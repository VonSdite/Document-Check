import json
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.task_types import (
    CONSISTENCY_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    document_groups_from_meta,
    task_type_label,
)
from app.tasks import _extract_consistency_document_text


class TaskTypesTest(unittest.TestCase):
    def test_document_groups_from_meta_ignores_invalid_entries(self):
        groups = document_groups_from_meta(
            json.dumps(
                {
                    "groups": [
                        {
                            "role": "master",
                            "label": "素材文档",
                            "files": [{"original_filename": "a.txt", "stored_filename": "a.txt"}],
                        },
                        {"role": "related", "label": "空组", "files": []},
                        "bad",
                    ]
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["label"], "素材文档")
        self.assertEqual(groups[0]["files"][0]["original_filename"], "a.txt")

    def test_task_type_label_for_consistency(self):
        self.assertEqual(task_type_label(CONSISTENCY_TASK_TYPE), "多文档对照检查")

    def test_task_type_label_for_language_consistency(self):
        self.assertEqual(task_type_label(LANGUAGE_CONSISTENCY_TASK_TYPE), "跨语种文档一致性对比")

    def test_task_type_label_for_image_check(self):
        self.assertEqual(task_type_label(IMAGE_TASK_TYPE), "图片检查")

    def test_task_type_label_for_video_check(self):
        self.assertEqual(task_type_label(VIDEO_TASK_TYPE), "视频检查")

    def test_extracts_grouped_consistency_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_dir = Path(temp_dir)
            (upload_dir / "master.txt").write_text("素材参数为 10A", encoding="utf-8")
            (upload_dir / "related.txt").write_text("资料参数为 12A", encoding="utf-8")
            app = Flask(__name__)
            app.config["UPLOAD_FOLDER"] = str(upload_dir)
            task = {
                "document_meta_json": json.dumps(
                    {
                        "groups": [
                            {
                                "role": "master",
                                "label": "素材文档",
                                "files": [
                                    {
                                        "original_filename": "master.txt",
                                        "stored_filename": "master.txt",
                                        "file_type": "txt",
                                        "file_size": 1,
                                    }
                                ],
                            },
                            {
                                "role": "related",
                                "label": "资料",
                                "files": [
                                    {
                                        "original_filename": "related.txt",
                                        "stored_filename": "related.txt",
                                        "file_type": "txt",
                                        "file_size": 1,
                                    }
                                ],
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            }

            text = _extract_consistency_document_text(app, task)

        self.assertIn("# 素材文档", text)
        self.assertIn("## 素材文档1：master.txt", text)
        self.assertIn("素材参数为 10A", text)
        self.assertIn("## 资料1：related.txt", text)
        self.assertIn("资料参数为 12A", text)


if __name__ == "__main__":
    unittest.main()
