import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from openpyxl import load_workbook

from app.checks.agent_policy import POLICY_KEY, check_agent_options, strategy_for
from app.contracts.agent import AgentOptions
from app.documents.index import DocumentIndex
from app.models.agent import AgentTurn
from app.models.client import LLMError
from app.persistence.checkpoints import (
    CheckpointOwnershipLost,
    CheckpointStore,
    MemoryCheckpointStore,
)
from app.persistence.connection import get_db
from app.persistence.defaults import seed_defaults
from app.persistence.schema import init_db
from app.persistence.settings import set_setting
from app.reporting.excel import build_report_workbook
from app.reporting.service import (
    _matching_report_suppression_rule,
    _prepare_task_results,
)
from app.tasks.runner import TaskRunner
from app.tasks.runtime.evidence_agent import EvidenceAgent, workflow_signature
from app.tasks.selection import _check_items_from_snapshot
from app.tasks.submission import _enabled_check_item_snapshots
from tests.test_agent_followup import followup
from tests.test_evidence_agent import (
    ScriptedModel,
    candidate,
    decision,
    discovery,
    empty_plan,
    input_context,
)


def round_model_turn(model, messages, *, schema, **kwargs):
    context = input_context(messages)
    title = schema["title"]
    if title == "CheckPlan":
        answer = empty_plan()
    elif title == "Followup":
        answer = followup(complete=True)
    elif title == "Discovery":
        refs = [
            {"source_id": source["id"], "quote": source["text"]}
            for source in context["sources"]
        ]
        answer = discovery(
            context,
            [candidate(refs), candidate(refs, description="需要核实适用条件")],
        )
    else:
        answer = {
            "actions": [],
            "decisions": [
                decision(
                    item["id"],
                    item["evidence"],
                    description=item["description"],
                    outcome="insufficient"
                    if "需要核实" in item["description"]
                    else "confirmed",
                )
                for item in context["candidates"]
            ],
        }
    model.before_request({"messages": messages})
    response = AgentTurn(
        json.dumps(answer, ensure_ascii=False),
        usage={"prompt_tokens": 100, "completion_tokens": 50},
        finish_reason="stop",
    )
    model.after_request({}, response, 0.01, None)
    return response


class EvidenceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(
            DATABASE=str(Path(self.root.name) / "test.sqlite3"),
            UPLOAD_FOLDER=str(Path(self.root.name) / "uploads"),
            IMAGE_FOLDER=str(Path(self.root.name) / "images"),
            NETWORK={"proxy_mode": "direct", "proxy": "", "ssl_verify": False},
        )
        Path(self.app.config["UPLOAD_FOLDER"]).mkdir()
        Path(self.app.config["IMAGE_FOLDER"]).mkdir()
        self.context = self.app.app_context()
        self.context.push()
        init_db()
        seed_defaults()

    def tearDown(self):
        self.context.pop()
        self.root.cleanup()

    def insert_task(self, snapshots, *, token="owner", text=None):
        db = get_db()
        cursor = db.execute(
            """INSERT INTO tasks(ip, original_filename, stored_filename, file_type, file_size,
            checks_json, checks_snapshot_json, model_name, api_base, status, claim_token, document_text, created_at, updated_at)
            VALUES ('127.0.0.1', 'a.txt', 'a.txt', 'txt', 10, '[]', ?, 'test', 'http://model/v1/chat/completions', 'running', ?, ?, '2026-01-01', '2026-01-01')""",
            (json.dumps(snapshots, ensure_ascii=False), token, text),
        )
        db.commit()
        return cursor.lastrowid

    def test_text_task_policies_use_built_in_defaults_and_ignore_public_overrides(
        self,
    ):
        for task_type in (
            "document_check",
            "consistency_check",
            "language_consistency_check",
        ):
            row = (
                get_db()
                .execute(
                    "SELECT * FROM check_items WHERE task_type = ? AND enabled=1 LIMIT 1",
                    (task_type,),
                )
                .fetchone()
            )
            if row is None:
                self.fail("默认检查项缺失")
            public_override = AgentOptions(
                strategy="compliance",
                max_calls=10,
                standards=[{"name": "规范", "version": "1", "text": "电压必须明确。"}],
            ).model_dump(mode="json")
            set_setting(POLICY_KEY + row["code"], public_override)
            snapshot = _enabled_check_item_snapshots(get_db(), [row["id"]], task_type)
            self.assertEqual(
                _check_items_from_snapshot(json.dumps(snapshot))[0]["agent_options"],
                AgentOptions().model_dump(mode="json"),
            )

    def test_checkpoint_write_requires_current_execution_owner(self):
        task_id = self.insert_task([])
        store = CheckpointStore(task_id, "check", "signature", "owner")
        store.save({"next": "discover"})
        self.assertEqual(store.load()["next"], "discover")
        get_db().execute(
            "UPDATE tasks SET claim_token='new-owner' WHERE id=?", (task_id,)
        )
        get_db().commit()
        with self.assertRaises(CheckpointOwnershipLost):
            store.save({"next": "done"})
        self.assertEqual(store.load()["next"], "discover")

    def test_checkpoint_from_different_thinking_workflow_is_not_reused(self):
        task_id = self.insert_task([])
        index = DocumentIndex([{"filename": "a.txt", "text": "设备电压220 V"}])
        item = {"code": "check", "prompt": "核实参数。"}
        connection = {"model_name": "deepseek", "reasoning_effort": "max"}
        options = AgentOptions.model_validate({"efficient_stages": True})
        with patch(
            "app.tasks.runtime.evidence_agent.WORKFLOW_VERSION", "evidence-review-v4"
        ):
            old_signature = workflow_signature(
                index, item, connection, options, "document_check"
            )
        CheckpointStore(task_id, "check", old_signature, "owner").save({"next": "done"})
        signature = workflow_signature(
            index, item, connection, options, "document_check"
        )
        compatible_signature = workflow_signature(
            index,
            item,
            connection,
            options,
            "document_check",
            include_execution_limits=True,
        )
        store = CheckpointStore(
            task_id,
            "check",
            signature,
            "owner",
            compatible_signatures=(compatible_signature,),
        )
        self.assertIsNone(store.load())

    def test_checkpoint_compatibility_keeps_task_item_and_execution_owner_boundaries(
        self,
    ):
        task_id = self.insert_task([])
        old_store = CheckpointStore(task_id, "check", "previous", "owner")
        old_store.save({"next": "discover"})
        store = CheckpointStore(
            task_id, "check", "current", "owner", compatible_signatures=("previous",)
        )
        self.assertEqual(store.load(), {"next": "discover"})
        self.assertIsNone(
            CheckpointStore(task_id, "check", "different", "owner").load()
        )
        self.assertIsNone(
            CheckpointStore(
                task_id,
                "other-check",
                "current",
                "owner",
                compatible_signatures=("previous",),
            ).load()
        )
        other_task = self.insert_task([])
        self.assertIsNone(
            CheckpointStore(
                other_task,
                "check",
                "current",
                "owner",
                compatible_signatures=("previous",),
            ).load()
        )
        store.save({"next": "review"})
        self.assertIsNone(old_store.load())
        self.assertEqual(store.load(), {"next": "review"})
        for assignment in (
            "cancel_requested=1",
            "cancel_requested=0, claim_token='other-owner'",
        ):
            get_db().execute(f"UPDATE tasks SET {assignment} WHERE id=?", (task_id,))
            get_db().commit()
            with self.assertRaises(CheckpointOwnershipLost):
                store.save({"next": "done"})
            self.assertEqual(store.load(), {"next": "review"})

    def test_worker_preserves_checkpoint_and_enforces_elapsed_budget(self):
        text = "\n".join(f"来源{n}：" + "设备参数。" * 120 for n in range(3))
        meta = {
            "source_documents": [
                {"filename": "a.txt", "text": text, "text_complete": True}
            ]
        }
        index = DocumentIndex.from_task(text, meta, "a.txt")
        old_options = AgentOptions(
            protocol="structured",
            chunk_chars=1000,
            max_calls=96,
            max_seconds=3600,
            full_document=False,
        )
        for missing_limits in (False, True):
            with self.subTest(missing_limits=missing_limits):
                snapshot_options = old_options.model_dump(mode="json")
                if missing_limits:
                    snapshot_options.pop("max_calls")
                    snapshot_options.pop("max_seconds")
                item = {
                    "code": "custom-check",
                    "name": "业务检查",
                    "prompt": "检查设备参数。",
                    "agent_options": snapshot_options,
                }
                task_id = self.insert_task([item], text=text)
                get_db().execute(
                    "UPDATE tasks SET document_meta_json=? WHERE id=?",
                    (json.dumps(meta, ensure_ascii=False), task_id),
                )
                get_db().commit()
                connection = dict(
                    get_db()
                    .execute("SELECT * FROM tasks WHERE id=?", (task_id,))
                    .fetchone()
                )
                legacy_signature = workflow_signature(
                    index,
                    item,
                    connection,
                    old_options,
                    "document_check",
                    include_execution_limits=True,
                )
                memory = MemoryCheckpointStore()
                discoveries = 0

                def handler(messages, title, tools, force):
                    nonlocal discoveries
                    if title == "CheckPlan":
                        return empty_plan()
                    discoveries += 1
                    return (
                        discovery(input_context(messages))
                        if discoveries == 1
                        else LLMError("503 服务暂时失败")
                    )

                first = EvidenceAgent(
                    index=index,
                    item=item,
                    task_type="document_check",
                    connection=connection,
                    options=old_options,
                    parallelism=1,
                    store=memory,
                    ensure_active=lambda: None,
                    model_factory=lambda connection, **kwargs: ScriptedModel(
                        handler, connection, **kwargs
                    ),
                ).run()
                self.assertIn("503", first["error"])
                state = memory.load()
                self.assertEqual(len(state["checked_ids"]), 1)
                state["elapsed_seconds"] = 3601.0
                state["requests"].extend(dict(state["requests"][0]) for _ in range(93))
                CheckpointStore(task_id, item["code"], legacy_signature, "owner").save(
                    state
                )
                known_input = sum(r["input_tokens"] or 0 for r in state["requests"])
                calls = []
                checked = []

                def turn(model, messages, *, schema, **kwargs):
                    calls.append(schema["title"])
                    self.fail("耗尽时间预算后不应发送模型请求")

                with patch("app.models.agent.AgentModel.turn", new=turn):
                    TaskRunner(self.app).run(task_id, "owner")
                task = (
                    get_db()
                    .execute("SELECT * FROM tasks WHERE id=?", (task_id,))
                    .fetchone()
                )
                self.assertEqual(task["status"], "failed")
                self.assertIn("执行时间预算", task["error"])
                result = json.loads(task["result_json"])[0]
                self.assertFalse(result["agent"]["coverage"]["all_checked"])
                self.assertEqual(calls, [])
                self.assertEqual(checked, [])
                self.assertFalse(set(checked) & set(state["checked_ids"]))
                self.assertEqual(result["agent"]["model_calls"], 96)
                self.assertEqual(result["agent"]["known_input_tokens"], known_input)
                self.assertGreaterEqual(result["agent"]["elapsed_seconds"], 3601.0)
                current_signature = workflow_signature(
                    index,
                    item,
                    connection,
                    old_options.model_copy(
                        update={"max_calls": None, "max_seconds": None}
                    ),
                    "document_check",
                )
                self.assertNotEqual(legacy_signature, current_signature)
                self.assertEqual(
                    CheckpointStore(
                        task_id, item["code"], current_signature, "owner"
                    ).load()["next"],
                    "discover",
                )
                self.assertIsNone(
                    CheckpointStore(
                        task_id, item["code"], legacy_signature, "owner"
                    ).load()
                )

    def test_end_to_end_worker_indexes_long_document_and_saves_valid_report(self):
        text = "\n".join("设备参数正常。" * 500 for _ in range(28))
        (Path(self.app.config["UPLOAD_FOLDER"]) / "a.txt").write_text(
            text, encoding="utf-8"
        )
        snapshots = [
            {
                "code": "custom-check",
                "name": "业务检查",
                "prompt": "检查设备参数。",
                "agent_options": {
                    **AgentOptions(protocol="structured").model_dump(mode="json"),
                    "max_output_tokens": 2048,
                    "max_tokens": 500000,
                },
            }
        ]
        get_db().execute("ALTER TABLE tasks ADD COLUMN max_output_tokens INTEGER")
        task_id = self.insert_task(snapshots)
        get_db().execute(
            "UPDATE tasks SET max_output_tokens=65536 WHERE id=?", (task_id,)
        )
        get_db().commit()
        calls = []

        def turn(model, messages, *, schema, **kwargs):
            calls.append(kwargs)
            model.before_request({"messages": messages})
            answer = (
                empty_plan()
                if schema["title"] == "CheckPlan"
                else followup(complete=True)
                if schema["title"] == "Followup"
                else discovery(input_context(messages))
            )
            response = AgentTurn(
                json.dumps(answer, ensure_ascii=False),
                usage={"prompt_tokens": 350000, "completion_tokens": 300000},
                finish_reason="stop",
            )
            model.after_request({}, response, 0.01, None)
            return response

        with patch("app.models.agent.AgentModel.turn", new=turn):
            TaskRunner(self.app).run(task_id, "owner")
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "completed", task["error"])
        self.assertTrue(calls)
        self.assertTrue(all("max_output_tokens" not in call for call in calls))
        result = json.loads(task["result_json"])[0]
        self.assertEqual(result["structured_report"]["items"], [])
        self.assertEqual(result["agent"]["coverage"]["checked_blocks"], 28)
        self.assertEqual(result["agent"]["known_input_tokens"], len(calls) * 350000)
        self.assertEqual(result["agent"]["known_output_tokens"], len(calls) * 300000)
        self.assertIsNotNone(
            get_db()
            .execute("SELECT 1 FROM task_checkpoints WHERE task_id=?", (task_id,))
            .fetchone()
        )
        meta = json.loads(task["document_meta_json"])
        self.assertTrue(meta["source_documents"][0]["text_complete"])
        output = build_report_workbook(task)
        self.assertGreater(len(output.getvalue()), 1000)
        workbook = load_workbook(output)
        self.assertEqual(workbook["执行用量"].cell(2, 3).value, len(calls) * 350000)
        self.assertEqual(workbook["执行用量"].cell(2, 4).value, len(calls) * 300000)

    def create_round_task(self, rounds=2):
        item = {
            "code": "custom-check",
            "name": "业务检查",
            "prompt": "检查设备参数。",
            "agent_options": AgentOptions(
                protocol="structured", strategy="local", full_document=False
            ).model_dump(mode="json"),
        }
        text = "设备A 电压220 V\n设备A 电压110 V"
        task_id = self.insert_task([item], text=text)
        get_db().execute(
            "UPDATE tasks SET document_meta_json=? WHERE id=?",
            (
                json.dumps(
                    {
                        "check_rounds": rounds,
                        "source_documents": [
                            {"filename": "a.txt", "text": text, "text_complete": True}
                        ],
                    },
                    ensure_ascii=False,
                ),
                task_id,
            ),
        )
        get_db().commit()
        return task_id

    def test_multiround_worker_keeps_independent_checkpoints_evidence_and_exports(self):
        task_id = self.create_round_task()
        agents = []

        def make_agent(**kwargs):
            agents.append(kwargs)
            return EvidenceAgent(**kwargs)

        with (
            patch("app.tasks.runner.EvidenceAgent", side_effect=make_agent),
            patch("app.models.agent.AgentModel.turn", new=round_model_turn),
        ):
            TaskRunner(self.app).run(task_id, "owner")
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "completed", task["error"])
        result = json.loads(task["result_json"])[0]
        self.assertEqual(
            result["round_summary"], {"planned": 2, "completed": 2, "failed": 0}
        )
        self.assertEqual(
            [agent["store"].code for agent in agents],
            ["custom-check", "custom-check:round:2"],
        )
        self.assertIsNot(agents[0]["shared_facts"], agents[1]["shared_facts"])
        self.assertIs(agents[0]["shared_work"], agents[1]["shared_work"])
        prepared = _prepare_task_results([result], suppression_rules={})[0]
        self.assertNotIn("error", prepared)
        self.assertEqual(prepared["report_counts"]["issue"], 1)
        self.assertEqual(prepared["report_counts"]["unverified"], 1)
        self.assertTrue(
            all(item["source_rounds"] == [1, 2] for item in prepared["report_items"])
        )
        self.assertTrue(all(item["text_evidence"] for item in prepared["report_items"]))
        self.assertEqual(result["agent"]["model_calls"], 7)
        self.assertEqual(result["agent"]["known_input_tokens"], 700)
        self.assertEqual(len(result["agent"]["followup_rounds"]), 2)
        self.assertEqual(result["agent"]["plans_reused"], 1)
        self.assertEqual(result["agent"]["review_reused"], 1)
        self.assertEqual(len(result["agent"]["decision_audit"]), 4)
        workbook = load_workbook(build_report_workbook(task))
        self.assertTrue(
            {"引用原文", "执行用量", "检查轮次"}.issubset(workbook.sheetnames)
        )
        self.assertEqual(workbook["检查轮次"].max_row, 3)

    def test_multiround_retry_resumes_failed_round_and_preserves_manual_review(self):
        task_id = self.create_round_task()
        discoveries = 0

        def interrupted_turn(model, messages, *, schema, **kwargs):
            nonlocal discoveries
            if schema["title"] == "Discovery":
                discoveries += 1
                if discoveries == 2:
                    model.before_request({"messages": messages})
                    model.after_request({}, None, 0.01, "503 服务暂时失败")
                    raise LLMError("503 服务暂时失败")
            return round_model_turn(model, messages, schema=schema, **kwargs)

        with patch("app.models.agent.AgentModel.turn", new=interrupted_turn):
            TaskRunner(self.app).run(task_id, "owner")
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "partial", task["error"])
        result = json.loads(task["result_json"])[0]
        self.assertEqual(result["round_summary"]["completed"], 1)
        previous_calls = result["agent"]["model_calls"]
        item_id = result["structured_report"]["items"][0]["id"]
        result["item_acceptances"] = {item_id: {"acceptance_status": "accepted"}}
        get_db().execute(
            "UPDATE tasks SET status='running', claim_token='owner', "
            "retry_check_codes_json='[\"custom-check\"]', result_json=? WHERE id=?",
            (json.dumps([result], ensure_ascii=False), task_id),
        )
        get_db().commit()
        stages = []

        def resumed_turn(model, messages, *, schema, **kwargs):
            stages.append(schema["title"])
            return round_model_turn(model, messages, schema=schema, **kwargs)

        with patch("app.models.agent.AgentModel.turn", new=resumed_turn):
            TaskRunner(self.app).run(task_id, "owner")
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "completed", task["error"])
        result = json.loads(task["result_json"])[0]
        self.assertEqual(stages, ["Discovery", "Review", "Followup"])
        self.assertEqual(result["agent"]["model_calls"], previous_calls + 3)
        self.assertEqual(result["structured_report"]["items"][0]["id"], item_id)
        self.assertEqual(
            result["item_acceptances"][item_id]["acceptance_status"], "accepted"
        )

    def test_worker_applies_call_budget_to_followup_and_stops_later_rounds(self):
        task_id = self.create_round_task()
        task = (
            get_db()
            .execute("SELECT checks_snapshot_json FROM tasks WHERE id=?", (task_id,))
            .fetchone()
        )
        snapshots = json.loads(task[0])
        snapshots[0]["agent_options"]["max_calls"] = 4
        get_db().execute(
            "UPDATE tasks SET checks_snapshot_json=? WHERE id=?",
            (json.dumps(snapshots), task_id),
        )
        get_db().commit()

        def turn(model, messages, *, schema, **kwargs):
            if schema["title"] != "Followup":
                return round_model_turn(model, messages, schema=schema, **kwargs)
            model.before_request({"messages": messages})
            response = AgentTurn(
                json.dumps(
                    followup(
                        actions=[
                            {
                                "name": "search_document",
                                "arguments": json.dumps({"query": "电压"}),
                            }
                        ]
                    )
                ),
                usage={"prompt_tokens": 100, "completion_tokens": 50},
                finish_reason="stop",
            )
            model.after_request({}, response, 0.01, None)
            return response

        with patch("app.models.agent.AgentModel.turn", new=turn):
            TaskRunner(self.app).run(task_id, "owner")
        task = (
            get_db()
            .execute("SELECT result_json FROM tasks WHERE id=?", (task_id,))
            .fetchone()
        )
        result = json.loads(task[0])[0]
        self.assertIn("调用次数预算", result["round_results"][0]["error"])
        self.assertEqual(result["agent"]["model_calls"], 4)
        self.assertEqual(len(result["round_results"]), 1)
        self.assertEqual(result["agent"]["followup_rounds"][0]["status"], "incomplete")

    def test_permanent_model_error_stops_later_evidence_rounds(self):
        task_id = self.create_round_task(rounds=5)
        with patch(
            "app.models.agent.AgentModel._request",
            side_effect=LLMError("余额不足，服务返回 500", retryable=False),
        ) as request:
            TaskRunner(self.app).run(task_id, "owner")
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "failed")
        request.assert_called_once()
        result = json.loads(task["result_json"])[0]
        self.assertEqual(
            result["round_summary"], {"planned": 5, "completed": 0, "failed": 1}
        )
        self.assertIn("余额不足", result["round_results"][0]["error"])

    def test_invalid_report_is_not_recovered_as_unvalidated_text(self):
        result = {
            "code": "check",
            "name": "检查",
            "agent": {"workflow_version": "v1"},
            "structured_report": {"items": "invalid"},
            "result": "1. 错误：凭空内容",
        }
        prepared = _prepare_task_results([result], suppression_rules={})[0]
        self.assertEqual(prepared["report_items"], [])
        self.assertIn("校验失败", prepared["error"])

    def test_feedback_suppression_requires_matching_evidence_context(self):
        item = {"description": "电压不一致", "evidence_fingerprint": "context-a"}
        rule = {
            "id": 1,
            "description": "电压不一致",
            "evidence_fingerprint": "context-b",
        }
        self.assertIsNone(_matching_report_suppression_rule(item, [rule])[0])
        rule["evidence_fingerprint"] = "context-a"
        self.assertEqual(_matching_report_suppression_rule(item, [rule])[0]["id"], 1)
        rule.pop("evidence_fingerprint")
        self.assertIsNone(_matching_report_suppression_rule(item, [rule])[0])

    def test_custom_checks_plan_scope_with_built_in_policies(self):
        options = check_agent_options("custom-business-check", "document_check")
        self.assertEqual(options, AgentOptions().model_dump(mode="json"))
        self.assertEqual(
            strategy_for("custom-business-check", "document_check", "auto"), "auto"
        )
        self.assertEqual(strategy_for("typo", "document_check", "auto"), "local")
        self.assertEqual(
            strategy_for("custom", "consistency_check", "auto"), "cross_document"
        )
        self.assertEqual(
            strategy_for("custom", "language_consistency_check", "auto"),
            "cross_language",
        )
        self.assertIsNone(check_agent_options("common-terms", "document_check"))


if __name__ == "__main__":
    unittest.main()
