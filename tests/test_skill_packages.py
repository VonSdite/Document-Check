import hashlib
import io
import json
import stat
import struct
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from docx import Document
from werkzeug.datastructures import FileStorage

from app.documents.skill_packages import load_skill_package, skill_archive_path
from app.persistence.check_item_pool import save_profile_check_items
from app.persistence.connection import get_db
from app.persistence.defaults import reset_default_check_item_prompt, seed_defaults
from app.persistence.schema import init_db
from app.tasks.activity import clear_activity
from app.tasks.runner import _run_check_items_concurrently
from app.tasks.selection import selected_check_items
from tests import test_routes as fixtures


def package_bytes(files=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            files
            or {
                "api-check/SKILL.md": "---\nname: 接口资料检查\ndescription: 按团队规则检查接口文档。\n---\n核对 references/规则.md 中的接口要求。",
                "api-check/references/规则.md": "请求示例必须与字段表一致。引用待检查文档的证据。",
            }
        ).items():
            archive.writestr(name, content)
    return output.getvalue()


def upload(data=None, name="接口检查.skill"):
    return FileStorage(stream=io.BytesIO(data or package_bytes()), filename=name)


class SkillPackageParserTest(unittest.TestCase):
    def test_instructions_references_and_metadata_are_read_from_one_root(self):
        package = load_skill_package(upload())
        self.assertEqual(package.metadata["name"], "接口资料检查")
        self.assertEqual(package.metadata["entrypoint"], "SKILL.md")
        self.assertEqual(
            [item["path"] for item in package.metadata["files"]],
            ["SKILL.md", "references/规则.md"],
        )
        self.assertIn("请求示例必须与字段表一致", package.prompt)
        self.assertIn("references/规则.md", package.prompt)
        self.assertNotIn("name: 接口资料检查", package.prompt)
        self.assertEqual(package.metadata["chars"], len(package.prompt))
        self.assertRegex(package.metadata["sha256"], r"^[0-9a-f]{64}$")

    def test_plain_markdown_and_utf8_bom_are_supported(self):
        package = load_skill_package(
            upload(
                package_bytes(
                    {"SKILL.md": b"\xef\xbb\xbfCheck documents.\r\nUse evidence."}
                )
            )
        )
        self.assertIn("Check documents.\nUse evidence.", package.prompt)
        self.assertEqual(package.metadata["name"], "")

    def test_word_reference_is_extracted_as_text(self):
        document = Document()
        document.add_paragraph("团队规范要求标注必填字段。")
        output = io.BytesIO()
        document.save(output)
        archive = package_bytes(
            {
                "SKILL.md": "依据团队规范检查。",
                "references/标准.docx": output.getvalue(),
            }
        )
        package = load_skill_package(upload(archive))
        self.assertIn("团队规范要求标注必填字段", package.prompt)
        with patch("app.documents.skill_packages.MAX_EXPANDED_BYTES", 100_000):
            with self.assertRaisesRegex(ValueError, "读取失败"):
                load_skill_package(upload(archive))

    def test_rejects_missing_ambiguous_unsafe_and_unreadable_packages(self):
        cases = [
            ({"readme.md": "说明"}, "唯一的 SKILL.md"),
            ({"SKILL.md": "检查", "other/SKILL.md": "检查"}, "唯一的 SKILL.md"),
            ({"a/b/SKILL.md": "检查"}, "顶层文件夹"),
            ({"a/SKILL.md": "检查", "outside.md": "规则"}, "所在目录"),
            ({"SKILL.md": "检查", "../outside.md": "规则"}, "无效路径"),
            ({"SKILL.md": "检查", "C:/outside.md": "规则"}, "无效路径"),
            ({"SKILL.md": "检查", "run.py": "print('run')"}, "不是支持的参考资料"),
            ({"SKILL.md": "检查", "rule.md": b"\xff"}, "UTF-8"),
            ({"SKILL.md": "检查", "rule.md": ""}, "未读取到文字"),
            ({"SKILL.md": "检查", "rule.md": "a\x00b"}, "有效的文本"),
            ({"SKILL.md": "检查", "rule.md": "a", "RULE.md": "b"}, "重名文件"),
            ({"SKILL.md": "---\nname: example\n"}, "正确结束"),
            ({"SKILL.md": "---\nname: [wrong]\n---\n检查"}, "name 需要是"),
            ({"SKILL.md": "---\nname: example\n---\n"}, "具体检查指令"),
        ]
        for files, message in cases:
            with (
                self.subTest(files=list(files)),
                self.assertRaisesRegex(ValueError, message),
            ):
                load_skill_package(upload(package_bytes(files)))
        for data, name in ((b"not a zip", "bad.zip"), (package_bytes(), "bad.exe")):
            with self.assertRaises(ValueError):
                load_skill_package(upload(data, name))
        archive = bytearray(package_bytes({"SKILL.md": "检查接口字段及示例。"}))
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entry = bundle.getinfo("SKILL.md")
        name_size, extra_size = struct.unpack_from(
            "<HH", archive, entry.header_offset + 26
        )
        data_offset = entry.header_offset + 30 + name_size + extra_size
        archive[data_offset] |= 0b110
        with self.assertRaisesRegex(ValueError, "无法读取"):
            load_skill_package(upload(bytes(archive)))

    def test_rejects_links_and_resource_limits(self):
        entry = zipfile.ZipInfo("link.md")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ValueError, "普通文件"):
            load_skill_package(
                upload(package_bytes({"SKILL.md": "检查", entry: "/outside"}))
            )
        for constant, limit in (
            ("MAX_ARCHIVE_BYTES", 5),
            ("MAX_EXPANDED_BYTES", 5),
            ("MAX_PACKAGE_FILES", 1),
            ("MAX_PROMPT_CHARS", 5),
        ):
            with (
                self.subTest(constant=constant),
                patch(f"app.documents.skill_packages.{constant}", limit),
                self.assertRaises(ValueError),
            ):
                load_skill_package(upload())


class SkillCheckRouteTest(unittest.TestCase):
    setUp = fixtures.AdminSettingsRouteTest.setUp
    tearDown = fixtures.AdminSettingsRouteTest.tearDown
    _configure_provider = fixtures.AdminSettingsRouteTest._configure_provider

    def create(self):
        response = self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "create_check_item",
                "task_type": "document_check",
                "check_method": "skill",
                "enabled": "on",
                "skill_package": (io.BytesIO(package_bytes()), "接口检查.skill"),
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["item_id"]

    def item(self, item_id):
        with self.app.app_context():
            return dict(
                get_db()
                .execute("SELECT * FROM check_items WHERE id = ?", (item_id,))
                .fetchone()
            )

    def test_create_preview_download_and_metadata_save(self):
        item_id = self.create()
        item = self.item(item_id)
        self.assertEqual(item["check_method"], "skill")
        self.assertEqual(item["name"], "接口资料检查")
        self.assertEqual(item["description"], "按团队规则检查接口文档。")
        package = json.loads(item["skill_package_json"])
        with self.app.app_context():
            self.assertTrue(skill_archive_path(package["sha256"]).is_file())
        page = BeautifulSoup(
            self.client.get("/admin/rules?tab=items").data, "html.parser"
        )
        editor = page.select_one(f"#check-item-detail-{item_id}")
        self.assertIn("references/规则.md", editor.get_text())
        self.assertIn(
            "请求示例必须与字段表一致", editor.select_one("[data-skill-preview]").text
        )
        self.assertTrue(editor.select_one('[name="prompt"]').has_attr("disabled"))
        response = self.client.get(f"/admin/rules?download_skill={item_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.data).hexdigest(), package["sha256"])
        response.close()
        self.assertEqual(
            self.app.test_client().get(f"/rules?download_skill={item_id}").status_code,
            302,
        )
        self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "prompt",
                "item_id": item_id,
                "name": "新的名称",
                "description": "新的说明",
                "check_method": "skill",
                "prompt": "篡改包内容",
                "enabled": "on",
            },
        )
        saved = self.item(item_id)
        self.assertEqual(saved["prompt"], item["prompt"])
        self.assertEqual(saved["skill_package_json"], item["skill_package_json"])
        self.assertEqual(saved["name"], "新的名称")

    def test_invalid_replacement_preserves_current_package_and_prompt(self):
        item_id = self.create()
        before = self.item(item_id)
        self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "prompt",
                "item_id": item_id,
                "name": "不应保存",
                "check_method": "skill",
                "skill_package": (io.BytesIO(b"broken"), "broken.zip"),
            },
        )
        self.assertEqual(self.item(item_id), before)

    def test_only_document_model_checks_accept_skill_packages(self):
        with self.app.app_context():
            local = (
                get_db()
                .execute(
                    "SELECT id FROM check_items WHERE code = 'sensitive-terms' LIMIT 1"
                )
                .fetchone()[0]
            )
        before = self.item(local)
        self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "prompt",
                "item_id": local,
                "name": "本地词表",
                "check_method": "skill",
                "skill_package": (io.BytesIO(package_bytes()), "test.zip"),
            },
        )
        self.assertEqual(self.item(local), before)
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "create_check_item",
                "task_type": "image_check",
                "name": "错误范围",
                "check_method": "skill",
                "skill_package": (io.BytesIO(package_bytes()), "test.zip"),
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("单文档", response.json["error"])

    def test_builtin_can_use_skill_and_reset_restores_prompt_method(self):
        with self.app.app_context():
            item_id = (
                get_db()
                .execute("SELECT id FROM check_items WHERE code = 'compliance'")
                .fetchone()[0]
            )
        original = self.item(item_id)
        self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "prompt",
                "item_id": item_id,
                "name": "技能规范检查",
                "check_method": "skill",
                "skill_package": (
                    io.BytesIO(
                        package_bytes(
                            {"SKILL.md": original["prompt"] + "\n使用团队补充要求。"}
                        )
                    ),
                    "test.zip",
                ),
                "enabled": "on",
            },
        )
        item = self.item(item_id)
        self.assertEqual(item["check_method"], "skill")
        with self.app.app_context(), patch.object(self.app, "teardown_appcontext"):
            init_db()
            seed_defaults()
        self.assertEqual(self.item(item_id), item)
        with self.app.app_context():
            self.assertTrue(reset_default_check_item_prompt(item_id))
        saved = self.item(item_id)
        self.assertEqual(saved["prompt"], original["prompt"])
        self.assertEqual(saved["check_method"], "prompt")
        self.assertEqual(saved["skill_package_json"], "{}")

    def test_upload_preflight_counts_skill_instructions_and_reference_text(self):
        item_id = self.create()
        item = self.item(item_id)
        model_id = self._configure_provider()
        text = "接口说明"
        input_chars = len(item["prompt"]) + len("file: doc.txt\n\n" + text)
        with self.app.app_context():
            save_profile_check_items("default", [item_id])
            get_db().execute(
                "UPDATE user_model_providers SET max_input_chars = ?",
                (input_chars - 1,),
            )
            get_db().commit()
        response = self.client.post(
            "/",
            data={
                "model_id": model_id,
                "checks": [str(item_id)],
                "document": (io.BytesIO(text.encode()), "doc.txt"),
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f"Skill 指令、参考资料与文档合计 {input_chars} 字",
            response.get_data(as_text=True),
        )
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
            )
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir()), [])

    def test_tasks_execute_frozen_skill_after_replacement_and_deletion(self):
        item_id = self.create()
        original = self.item(item_id)
        model_id = self._configure_provider()
        with self.app.app_context():
            save_profile_check_items("default", [item_id])
            get_db().commit()
        self.client.post(
            "/",
            data={
                "model_id": model_id,
                "profile_id": "default",
                "checks": [str(item_id)],
                "document": (
                    io.BytesIO("接口 userId 为必填字段。".encode()),
                    "接口.txt",
                ),
            },
        )
        with self.app.app_context():
            task = dict(get_db().execute("SELECT * FROM tasks").fetchone())
            frozen = json.loads(task["checks_snapshot_json"])[0]
            self.assertEqual(frozen["check_method"], "skill")
            self.assertEqual(frozen["prompt"], original["prompt"])
            self.assertEqual(
                frozen["skill_package"], json.loads(original["skill_package_json"])
            )
        self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "prompt",
                "item_id": item_id,
                "name": "接口资料检查",
                "check_method": "skill",
                "skill_package": (
                    io.BytesIO(package_bytes({"SKILL.md": "第二版检查标准"})),
                    "new.zip",
                ),
                "enabled": "on",
            },
        )
        self.assertNotEqual(
            self.item(item_id)["skill_package_json"], original["skill_package_json"]
        )
        self.client.post(
            "/admin/rules?tab=items",
            data={"action": "delete_check_item", "item_id": item_id},
        )
        with self.app.app_context():
            db = get_db()
            task["retry_check_codes_json"] = json.dumps([frozen["code"]])
            selected = selected_check_items(db, task)
            self.assertEqual(selected[0]["prompt"], original["prompt"])
            db.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?", (task["id"],)
            )
            db.commit()
            task["status"] = "running"
            with patch(
                "app.tasks.runner.run_check",
                return_value='{"summary":"检查完成","items":[]}',
            ) as model:
                results = _run_check_items_concurrently(
                    self.app,
                    task,
                    selected,
                    "接口 userId 为必填字段。",
                    max_workers=1,
                    stream_trace_enabled=False,
                )
            self.assertEqual(model.call_args.kwargs["prompt"], original["prompt"])
            self.assertIn("请求示例必须与字段表一致", model.call_args.kwargs["prompt"])
            self.assertEqual(results[0]["skill_package"], frozen["skill_package"])
            clear_activity(task["id"], None)
            task["max_input_chars"] = 20
            with patch("app.tasks.runner.run_check") as model:
                results = _run_check_items_concurrently(
                    self.app,
                    task,
                    selected,
                    "接口说明",
                    max_workers=1,
                    stream_trace_enabled=False,
                )
            model.assert_not_called()
            self.assertIn("Skill 指令、参考资料与文档合计", results[0]["error"])

    def test_prompt_method_keeps_ordinary_item_behavior(self):
        item_id = self.create()
        self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "prompt",
                "item_id": item_id,
                "name": "提示词检查",
                "check_method": "prompt",
                "prompt": "核对章节标题",
                "enabled": "on",
            },
        )
        item = self.item(item_id)
        self.assertEqual(item["check_method"], "prompt")
        self.assertEqual(item["prompt"], "核对章节标题")
        self.assertEqual(item["skill_package_json"], "{}")
        self.assertEqual(
            self.client.get(f"/admin/rules?download_skill={item_id}").status_code, 404
        )
