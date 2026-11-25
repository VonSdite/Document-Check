import io
import json
import unittest

from openpyxl import load_workbook

from app.contracts.task_types import VIDEO_TASK_TYPE
from app.persistence.connection import get_db
from app.reporting.rounds import build_round_result
from tests import test_routes as fixtures


class ExcelExportTest(unittest.TestCase):
    setUp = fixtures.AdminSettingsRouteTest.setUp
    tearDown = fixtures.AdminSettingsRouteTest.tearDown
    _insert_task = fixtures.AdminSettingsRouteTest._insert_task
    _insert_report_task = fixtures.AdminSettingsRouteTest._insert_report_task

    def save_results(self, task_id, results):
        stored = json.dumps(results, ensure_ascii=False)
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET result_json = ? WHERE id = ?", (stored, task_id)
            )
            get_db().commit()
        return stored

    def export(self, task_id, prefix="/admin"):
        response = self.client.get(f"{prefix}/tasks/{task_id}/export.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        workbook = load_workbook(io.BytesIO(response.data))
        self.addCleanup(workbook.close)
        return workbook

    def test_report_controls_are_removed_and_offline_review_still_matches(self):
        controls = "".join(chr(code) for code in range(32) if code not in (9, 10, 13))
        readable = "后文\t制表\n换行\r回车 中文 😀"
        fields = {
            "category": "问题类型",
            "location": "位置",
            "excerpt": "原文/证据",
            "description": "问题描述",
            "impact": "影响",
            "suggestion": "修改建议",
        }
        report = {
            "items": [
                {
                    "status": "issue",
                    **{field: "前文" + controls + readable for field in fields},
                }
            ]
        }
        task_id = self._insert_task()
        self.save_results(
            task_id,
            [
                {
                    "code": "consistency",
                    "name": "全文\x00一致性检查",
                    "result": json.dumps(report, ensure_ascii=False),
                }
            ],
        )
        for prefix in ("/admin", ""):
            workbook = self.export(task_id, prefix)
            sheet = workbook["报告条目"]
            headers = {cell.value: cell.column for cell in sheet[1]}
            for label in fields.values():
                self.assertEqual(sheet.cell(2, headers[label]).value, "前文" + readable)
            self.assertEqual(sheet.cell(2, headers["检查项"]).value, "全文一致性检查")
            self.assertEqual(sheet.cell(2, headers["任务ID"]).value, task_id)
            self.assertEqual(sheet.cell(2, headers["任务ID"]).data_type, "n")
            self.assertEqual(workbook["初审与终审"].cell(2, 3).value, "前文" + readable)
            stats = dict(workbook["统计"].iter_rows(min_row=2, values_only=True))
            self.assertEqual(stats["问题"], 1)

        item_id = sheet.cell(2, headers["条目标识（请勿修改）"]).value
        sheet.cell(2, headers["是否认可 AI 结论"]).value = "认可"
        annotated = io.BytesIO()
        workbook.save(annotated)
        annotated.seek(0)
        response = self.client.post(
            f"/admin/tasks/{task_id}/import.xlsx",
            data={"report_excel": (annotated, "review.xlsx")},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            stored = json.loads(
                get_db()
                .execute("SELECT result_json FROM tasks WHERE id = ?", (task_id,))
                .fetchone()[0]
            )[0]
        self.assertEqual(json.loads(stored["result"]), report)
        self.assertEqual(stored["item_acceptances"][item_id], {"status": "accepted"})

    def test_export_cleans_document_names_and_profile_metadata(self):
        task_id = self._insert_report_task()
        for metadata, expected in (
            ({}, "文档名.txt"),
            (
                {
                    "groups": [
                        {
                            "files": [
                                {"original_filename": "主\x0b文档.txt"},
                                {"original_filename": "参考\x1b文档.txt"},
                            ]
                        }
                    ]
                },
                "主文档.txt\n参考文档.txt",
            ),
        ):
            with self.subTest(metadata=metadata):
                with self.app.app_context():
                    get_db().execute(
                        "UPDATE tasks SET original_filename = ?, document_meta_json = ?, "
                        "check_profile_snapshot_json = ? WHERE id = ?",
                        (
                            "文档\x0c名.txt",
                            json.dumps(metadata),
                            json.dumps({"name": "方案\x01名称", "revision": 7}),
                            task_id,
                        ),
                    )
                    get_db().commit()
                workbook = self.export(task_id)
                self.assertEqual(workbook["报告条目"].cell(2, 3).value, expected)
                self.assertEqual(workbook["检查方案"].cell(2, 2).value, "方案名称")
                self.assertEqual(workbook["检查方案"].cell(2, 3).value, 7)

    def test_export_cleans_round_summary_error_and_raw_reply(self):
        task_id = self._insert_report_task(status="partial")
        result = {
            "code": "consistency",
            "name": "一致性检查",
            **build_round_result(
                "consistency",
                2,
                [
                    {
                        "round": 1,
                        "status": "completed",
                        "report": {
                            "summary": "摘要\x00完成",
                            "items": [{"status": "issue", "description": "参数不一致"}],
                        },
                        "result": "原始\x0b回复\n第二行",
                    },
                    {
                        "round": 2,
                        "status": "failed",
                        "error": "错误\x1b原因",
                        "result": "部分\x0c回复",
                    },
                ],
            ),
        }
        stored = self.save_results(task_id, [result])
        workbook = self.export(task_id)
        rows = list(workbook["检查轮次"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(
            rows[0], ("一致性检查", 1, "完成", "摘要完成", None, "原始回复\n第二行")
        )
        self.assertEqual(
            rows[1], ("一致性检查", 2, "失败", None, "错误原因", "部分回复")
        )
        with self.app.app_context():
            self.assertEqual(
                get_db()
                .execute("SELECT result_json FROM tasks WHERE id = ?", (task_id,))
                .fetchone()[0],
                stored,
            )

    def test_export_cleans_appended_video_evidence(self):
        task_id = self._insert_report_task()
        self.save_results(
            task_id,
            [
                {
                    "code": "video-installation-sequence",
                    "name": "安装顺序检查",
                    "structured_report": {
                        "items": [
                            {
                                "status": "issue",
                                "description": "安装顺序有误",
                                "excerpt": "画面证据",
                                "evidence_refs": [
                                    {
                                        "position": "00:\x0b01.000",
                                        "filename": "帧\x0c1.png",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ],
        )
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET task_type = ? WHERE id = ?",
                (VIDEO_TASK_TYPE, task_id),
            )
            get_db().commit()
        sheet = self.export(task_id)["报告条目"]
        headers = {cell.value: cell.column for cell in sheet[1]}
        self.assertEqual(
            sheet.cell(2, headers["原文/证据"]).value,
            "画面证据\n关键帧：00:01.000（帧1.png）",
        )


if __name__ == "__main__":
    unittest.main()
