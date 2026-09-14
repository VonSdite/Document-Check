import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from flask import Flask

from app.models import client as llm
from app.tasks.files import _task_upload_paths
from app.tasks.model_output import (
    ModelOutputRecorder,
    model_output_path,
    read_model_output,
)
from app.tasks.runtime.artifacts import _remove_task_artifacts, _task_artifact_usage
from app.tasks.runtime.multimodal_protocol import (
    _run_combined_multimodal_check_with_repair,
)
from tests.test_llm import FakeResponse, FakeSession


class ModelOutputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Flask(__name__)
        self.app.config["UPLOAD_FOLDER"] = str(Path(self.temp.name) / "uploads")
        self.app.config["IMAGE_FOLDER"] = str(Path(self.temp.name) / "images")
        self.recorder = ModelOutputRecorder(self.app, 1)
        llm._reset_http_session_pools()

    def record(self, stream, codes, text):
        output = self.recorder.for_checks(codes)
        for event in [
            {"kind": "start"},
            {"kind": "thinking", "text": text},
            {"kind": "content", "text": "正文"},
            {"kind": "end"},
        ]:
            output(dict(event, stream=stream, attempt=1))

    def drain(self):
        events, cursor, pages = [], 0, 0
        while True:
            result = read_model_output(self.app, 1, cursor)
            events.extend(result["events"])
            pages += 1
            if not result["more"]:
                return events, result["cursor"], pages
            self.assertGreater(result["cursor"], cursor)
            cursor = result["cursor"]

    def test_concurrent_unicode_output_is_complete_and_bounded(self):
        text = "思考中文🙂\n" * 5000
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(
                executor.map(
                    lambda i: self.record(str(i), [str(i), "shared"], text), range(4)
                )
            )
        events, cursor, pages = self.drain()
        self.assertGreater(pages, 1)
        for index in range(4):
            received = [event for event in events if event["stream"] == str(index)]
            self.assertEqual(received[0]["kind"], "start")
            self.assertEqual(received[-1]["kind"], "end")
            self.assertEqual(
                "".join(e["text"] for e in received if e["kind"] == "thinking"), text
            )
            self.assertTrue(all(e["codes"] == [str(index), "shared"] for e in received))
        self.assertEqual(read_model_output(self.app, 1, cursor)["events"], [])
        self.assertEqual(cursor, model_output_path(self.app, 1).stat().st_size)

    def test_incomplete_tail_waits_and_then_reads_complete_unicode_record(self):
        path = model_output_path(self.app, 1)
        path.parent.mkdir()
        line = json.dumps(
            {"kind": "thinking", "text": "中文🙂"}, ensure_ascii=False
        ).encode()
        path.write_bytes(line[:-3])
        self.assertEqual(read_model_output(self.app, 1)["cursor"], 0)
        with path.open("ab") as output:
            output.write(line[-3:] + b"\n")
        result = read_model_output(self.app, 1)
        self.assertEqual(result["events"][0]["text"], "中文🙂")
        self.assertTrue(read_model_output(self.app, 1, 9999)["reset"])

    def test_output_records_execution_per_check_for_late_response_isolation(self):
        record = self.recorder.for_checks(["a", "b"], executions={"a": 2, "b": 4})
        record({"kind": "start", "stream": "combined", "attempt": 1})
        record(
            {
                "kind": "content",
                "stream": "combined",
                "attempt": 1,
                "codes": ["b"],
                "text": "补偿结果",
            }
        )
        record({"kind": "end", "stream": "combined", "attempt": 1})
        events, _, _ = self.drain()
        self.assertEqual(events[0]["executions"], {"a": 2, "b": 4})
        self.assertEqual(events[1]["executions"], {"b": 4})

    def test_output_is_counted_and_removed_with_task_artifacts(self):
        self.record("one", ["check"], "分析")
        path = model_output_path(self.app, 1)
        task = {"id": 1, "stored_filename": "source.txt", "document_meta_json": "{}"}
        with self.app.app_context():
            self.assertIn(path, _task_upload_paths(task))
        self.assertEqual(_task_artifact_usage(self.app, task), (path.stat().st_size, 1))
        _remove_task_artifacts(self.app, task)
        self.assertFalse(path.exists())

    def test_sparse_output_flushes_while_upstream_is_idle(self):
        flushed = Event()
        original_flush = self.recorder._flush

        def flush():
            original_flush()
            flushed.set()

        record = self.recorder.for_checks(["check"])
        record({"kind": "start", "stream": "one", "attempt": 1})
        with patch.object(self.recorder, "_flush", side_effect=flush):
            record(
                {
                    "kind": "thinking",
                    "text": "第一段思考",
                    "stream": "one",
                    "attempt": 1,
                }
            )
            self.assertTrue(flushed.wait(2), "上游停顿时缓冲内容应按时写出")
        events, _, _ = self.drain()
        self.assertEqual(events[-1]["text"], "第一段思考")
        record({"kind": "end", "stream": "one", "attempt": 1})
        self.assertIsNone(self.recorder.timer)

    def test_output_write_failure_does_not_abort_check(self):
        with (
            patch.object(Path, "open", side_effect=PermissionError("locked")),
            self.assertLogs("app.tasks.model_output", level="WARNING"),
        ):
            self.record("one", ["check"], "分析")
        self.assertTrue(self.recorder.disabled)

    def run_model(self, response, *, cancel_event=None, model_name="custom-model"):
        llm._reset_http_session_pools()
        session = FakeSession(response)
        with (
            patch.object(llm.requests, "Session", return_value=session),
            patch.object(llm.time, "sleep"),
        ):
            return llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key=None,
                model_name=model_name,
                check_name="检查",
                prompt="检查",
                document_text="材料",
                on_output=self.recorder.for_checks(["check"]),
                cancel_event=cancel_event,
            )

    def test_output_display_is_independent_of_model_name_and_alias(self):
        for model_name in (
            "glm-5.3-flash",
            "deepseek-v4",
            "qwen3",
            "kimi-k2.6",
            "private-proxy-alias",
            "unknown-future-model",
        ):
            for field in (
                "reasoning_content",
                "reasoning",
                "reasoning_text",
                "reasoning_details",
            ):
                for envelope in ("delta", "message"):
                    with self.subTest(model=model_name, field=field, envelope=envelope):
                        cursor = self.drain()[1]
                        reasoning = (
                            [{"text": "通用思考"}]
                            if field == "reasoning_details"
                            else "通用思考"
                        )
                        frames = [
                            json.dumps({"choices": [{envelope: {field: reasoning}}]}),
                            json.dumps(
                                {"choices": [{envelope: {"content": "通用正文"}}]}
                            ),
                            "data: [DONE]",
                        ]
                        self.assertEqual(
                            self.run_model(
                                [FakeResponse(lines=frames)], model_name=model_name
                            ),
                            "通用正文",
                        )
                        events = read_model_output(self.app, 1, cursor)["events"]
                        self.assertEqual(
                            [event["kind"] for event in events],
                            ["start", "thinking", "content", "end"],
                        )
                        self.assertEqual(
                            "".join(
                                event["text"]
                                for event in events
                                if event["kind"] == "thinking"
                            ),
                            "通用思考",
                        )
                        self.assertEqual(
                            "".join(
                                event["text"]
                                for event in events
                                if event["kind"] == "content"
                            ),
                            "通用正文",
                        )

    def test_model_without_reasoning_displays_body_only(self):
        self.assertEqual(
            self.run_model(
                [
                    FakeResponse(
                        lines=[
                            'data: {"choices":[{"delta":{"content":"普通模型正文"}}]}',
                            "data: [DONE]",
                        ]
                    )
                ]
            ),
            "普通模型正文",
        )
        events, _, _ = self.drain()
        self.assertEqual(
            [event["kind"] for event in events], ["start", "content", "end"]
        )
        self.assertEqual(events[1]["text"], "普通模型正文")

    def test_retry_preserves_thinking_and_separates_attempts(self):
        responses = [
            FakeResponse(
                lines=[
                    'data: {"choices":[{"delta":{"reasoning_content":"分析第一轮"}}]}',
                    "data: [DONE]",
                ]
            ),
            FakeResponse(
                lines=[
                    'data: {"choices":[{"delta":{"content":"正文第二轮"}}]}',
                    "data: [DONE]",
                ]
            ),
        ]
        self.assertEqual(self.run_model(responses), "正文第二轮")
        events, _, _ = self.drain()
        self.assertEqual([e["attempt"] for e in events if e["kind"] == "end"], [1, 2])
        first = [e for e in events if e["attempt"] == 1]
        second = [e for e in events if e["attempt"] == 2]
        self.assertNotEqual(first[0]["stream"], second[0]["stream"])
        self.assertEqual("".join(e["text"] for e in first), "分析第一轮")
        self.assertEqual("".join(e["text"] for e in second), "正文第二轮")

    def test_cancel_flushes_received_thinking_without_retry(self):
        cancel = Event()

        def lines():
            yield 'data: {"choices":[{"delta":{"reasoning_content":"已收到的思考"}}]}'
            cancel.set()
            yield 'data: {"choices":[{"delta":{"content":"取消后的内容"}}]}'

        with self.assertRaises(llm.LLMError):
            self.run_model([FakeResponse(lines=lines())], cancel_event=cancel)
        events, _, _ = self.drain()
        self.assertEqual("".join(e["text"] for e in events), "已收到的思考")
        self.assertEqual([e["kind"] for e in events], ["start", "thinking", "end"])

    def test_full_message_and_reasoning_details_are_not_truncated(self):
        reasoning = "分析" * 2000
        response = FakeResponse(
            lines=[
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "reasoning_details": [{"text": reasoning}],
                                    "content": "正文",
                                }
                            }
                        ]
                    }
                )
            ]
        )
        self.assertEqual(self.run_model([response]), "正文")
        events, _, _ = self.drain()
        self.assertEqual(
            "".join(e["text"] for e in events if e["kind"] == "thinking"), reasoning
        )

    def test_multimodal_repair_output_belongs_only_to_missing_checks(self):
        checks = [{"code": "a", "name": "A"}, {"code": "b", "name": "B"}]
        count = 0

        def run(**kwargs):
            nonlocal count
            count += 1
            code = "a" if count == 1 else "b"
            output = kwargs["on_output"]
            for event in [
                {"kind": "start"},
                {"kind": "content", "text": f"正文{code}"},
                {"kind": "end"},
            ]:
                output(dict(event, stream=str(count), attempt=1))
            return json.dumps(
                {"results": [{"code": code, "summary": "未发现问题", "items": []}]}
            )

        with patch(
            "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
            side_effect=run,
        ):
            _run_combined_multimodal_check_with_repair(
                self.app,
                check_items=checks,
                prompt_builder=lambda items: "检查",
                check_name="图片",
                error_label="图片",
                run_kwargs={
                    "on_output": self.recorder.for_checks(["a", "b"], "批次 1/2")
                },
            )
        events, _, _ = self.drain()
        initial, repair = [e for e in events if e["kind"] == "content"]
        self.assertEqual(initial["codes"], ["a", "b"])
        self.assertEqual(repair["codes"], ["b"])
        self.assertEqual(repair["label"], "批次 1/2 · 缺项补偿")

    def test_reasoning_chunk_limit_is_sixty_four_thousand(self):
        consumed = 0

        def lines():
            nonlocal consumed
            for _ in range(64001):
                consumed += 1
                yield 'data: {"choices":[{"delta":{"reasoning_content":"a"}}]}'

        with self.assertRaises(llm._ReasoningOnlyResponseError):
            llm._read_stream_lines(lines(), None)
        self.assertEqual(consumed, 64000)
