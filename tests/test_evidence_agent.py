import json
import unittest

from app.contracts.agent import AgentOptions
from app.documents.index import DocumentIndex
from app.models.agent import AgentTurn, ToolsUnsupported
from app.models.client import LLMError
from app.persistence.checkpoints import MemoryCheckpointStore
from app.tasks.runtime.evidence_agent import EvidenceAgent


def candidate(refs, **changes):
    return {
        "category": "参数一致性",
        "description": "同一设备的电压不一致",
        "impact": "可能导致错误操作",
        "suggestion": "核实并统一",
        "severity": "high",
        "kind": "issue",
        "absence_claim": False,
        "applicability": "同一型号和条件",
        "requirement_ids": [],
        "evidence": refs,
        **changes,
    }


def decision(candidate_id, evidence, **changes):
    return {
        "candidate_id": candidate_id,
        "outcome": "confirmed",
        "applicable": "yes",
        "rationale": "原文同一条件存在冲突",
        "description": "同一设备的电压不一致",
        "impact": "可能导致错误操作",
        "suggestion": "核实并统一",
        "evidence": evidence,
        "counterevidence": [],
        **changes,
    }


class ScriptedModel:
    def __init__(self, handler, connection, **kwargs):
        self.handler = handler
        self.before = kwargs["before_request"]
        self.after = kwargs["after_request"]
        self.capabilities = kwargs["capabilities"]

    def turn(
        self, messages, *, schema, tools=None, force_tool=None, max_output_tokens=8192
    ):
        payload = {"messages": messages, "max_completion_tokens": max_output_tokens}
        self.before(payload)
        answer = self.handler(messages, schema.get("title"), tools, force_tool)
        if isinstance(answer, Exception):
            self.after(payload, None, 0.01, str(answer))
            if isinstance(answer, ToolsUnsupported):
                self.capabilities["native_tools"] = False
            raise answer
        turn = (
            answer
            if isinstance(answer, AgentTurn)
            else AgentTurn(
                json.dumps(answer, ensure_ascii=False),
                usage={"prompt_tokens": 100, "completion_tokens": 50},
                finish_reason="stop",
            )
        )
        self.after(payload, turn, 0.01, None)
        return turn


def input_context(messages):
    for message in messages[::-1]:
        if "输入：" in str(message.get("content")):
            return json.loads(message["content"].split("输入：", 1)[1])
    return {}


def empty_plan():
    return {
        "document_type": "技术说明",
        "audience": "技术人员",
        "scope": "设备参数",
        "requirements": [],
    }


def discovery(context, candidates=None, facts=None):
    return {
        "covered_source_ids": [s["id"] for s in context["sources"]],
        "facts": facts or [],
        "candidates": candidates or [],
        "observations": [],
    }


class EvidenceAgentTest(unittest.TestCase):
    def make_agent(
        self,
        handler,
        *,
        documents=None,
        options=None,
        store=None,
        ensure=None,
        code="consistency",
    ):
        index = DocumentIndex(
            documents
            or [
                {
                    "filename": "a.txt",
                    "text": "设备A 电压220 V\n设备A 电压110 V",
                    "text_complete": True,
                }
            ]
        )
        return EvidenceAgent(
            index=index,
            item={"code": code, "name": "一致性", "prompt": "检查同一型号参数一致性。"},
            task_type="document_check",
            connection={
                "api_base": "http://model/v1/chat/completions",
                "model_name": "test",
                "max_input_chars": 80000,
            },
            options=options or AgentOptions(protocol="structured"),
            store=store or MemoryCheckpointStore(),
            ensure_active=ensure or (lambda: None),
            model_factory=lambda connection, **kwargs: ScriptedModel(
                handler, connection, **kwargs
            ),
        )

    def test_no_candidates_skips_review_and_records_actual_usage(self):
        calls = []

        def handler(messages, title, tools, force):
            calls.append(title)
            return (
                empty_plan()
                if title == "CheckPlan"
                else discovery(input_context(messages))
            )

        result = self.make_agent(handler).run()
        self.assertEqual(calls, ["CheckPlan", "Discovery"])
        self.assertEqual(result["report"]["items"], [])
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        self.assertEqual(result["metadata"]["known_input_tokens"], 200)
        self.assertEqual(result["error"], "")

    def test_global_comparison_finds_problem_absent_from_local_candidates(self):
        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                facts = [
                    {
                        "subject": "设备A",
                        "attribute": "电压",
                        "value": value,
                        "unit": "V",
                        "conditions": "运行",
                        "version": "1",
                        "reference": {
                            "source_id": source["id"],
                            "quote": source["text"],
                        },
                    }
                    for source, value in zip(context["sources"], ["220", "110"])
                ]
                return discovery(context, facts=facts)
            if title == "Comparison":
                return {
                    "candidates": [
                        candidate([f["reference"] for f in context["facts"]])
                    ]
                }
            if title == "Alignment":
                return {
                    "keys": [
                        {"fact_id": f["fact_id"], "attribute_key": "电压"}
                        for f in context["facts"]
                    ]
                }
            c = context["candidates"][0]
            return {"actions": [], "decisions": [decision(c["id"], c["evidence"])]}

        result = self.make_agent(handler).run()
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertEqual(len(result["report"]["items"][0]["evidence_refs"]), 2)

    def test_review_withdraws_difference_between_models(self):
        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                return discovery(
                    context,
                    candidates=[
                        candidate(
                            [
                                {"source_id": s["id"], "quote": s["text"]}
                                for s in context["sources"]
                            ]
                        )
                    ],
                )
            c = context["candidates"][0]
            return {
                "actions": [],
                "decisions": [
                    decision(
                        c["id"],
                        c["evidence"],
                        outcome="explained",
                        applicable="no",
                        rationale="A 与 B 是不同型号",
                    )
                ],
            }

        result = self.make_agent(
            handler,
            documents=[
                {
                    "filename": "a",
                    "text": "A型号220 V\nB型号110 V",
                    "text_complete": True,
                }
            ],
        ).run()
        self.assertEqual(result["report"]["items"], [])
        self.assertEqual(len(result["report"]["withdrawn_items"]), 1)

    def test_fabricated_quote_cannot_enter_report(self):
        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                return discovery(
                    context,
                    candidates=[
                        candidate(
                            [
                                {
                                    "source_id": context["sources"][0]["id"],
                                    "quote": "不存在的500 V",
                                }
                            ]
                        )
                    ],
                )
            c = context["candidates"][0]
            return {"actions": [], "decisions": [decision(c["id"], c["evidence"])]}

        result = self.make_agent(handler).run()
        self.assertEqual(result["report"]["items"], [])
        self.assertEqual(len(result["report"]["pending_items"]), 1)

    def test_missing_content_requires_requirement_and_full_coverage(self):
        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                return discovery(
                    context, candidates=[candidate([], absence_claim=True)]
                )
            c = context["candidates"][0]
            return {"actions": [], "decisions": [decision(c["id"], [])]}

        result = self.make_agent(handler).run()
        self.assertEqual(result["report"]["items"], [])
        self.assertIn("覆盖", result["report"]["pending_items"][0]["rationale"])

    def test_native_tool_capability_falls_back_to_structured_actions(self):
        count = 0

        def handler(messages, title, tools, force):
            nonlocal count
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                s = context["sources"][0]
                return discovery(
                    context,
                    candidates=[
                        candidate([{"source_id": s["id"], "quote": s["text"]}])
                    ],
                )
            if tools:
                return ToolsUnsupported("tools 不支持")
            count += 1
            c = context["candidates"][0]
            if count == 1:
                return {
                    "actions": [
                        {
                            "name": "read_context",
                            "arguments": json.dumps(
                                {
                                    "source_id": c["evidence"][0]["source_id"],
                                    "radius": 1,
                                }
                            ),
                        }
                    ],
                    "decisions": [],
                }
            return {"actions": [], "decisions": [decision(c["id"], c["evidence"])]}

        result = self.make_agent(handler, options=AgentOptions(protocol="auto")).run()
        self.assertFalse(result["metadata"]["capabilities"]["native_tools"])
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertEqual(count, 2)

    def test_resume_reuses_completed_nodes_without_repeating_plan(self):
        store = MemoryCheckpointStore()
        failed = False
        calls = []

        def handler(messages, title, tools, force):
            nonlocal failed
            calls.append(title)
            if title == "CheckPlan":
                return empty_plan()
            if not failed:
                failed = True
                return LLMError("503 服务暂时失败")
            return discovery(input_context(messages))

        result = self.make_agent(handler, store=store).run()
        self.assertIn("503", result["error"])
        result = self.make_agent(handler, store=store).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(calls.count("CheckPlan"), 1)
        self.assertEqual(calls.count("Discovery"), 2)
        completed_calls = len(calls)
        self.make_agent(handler, store=store).run()
        self.assertEqual(len(calls), completed_calls)

    def test_incomplete_source_is_not_reported_as_clean_complete_document(self):
        def handler(messages, title, tools, force):
            return (
                empty_plan()
                if title == "CheckPlan"
                else discovery(input_context(messages))
            )

        result = self.make_agent(
            handler,
            documents=[
                {"filename": "a.pdf", "text": "读取的第一页", "text_complete": False}
            ],
        ).run()
        self.assertIn("未可靠解析", result["error"])
        self.assertIn("仅覆盖", result["report"]["summary"])

    def test_long_documents_cover_every_block_in_bounded_requests(self):
        sizes = []

        def handler(messages, title, tools, force):
            sizes.append(len(json.dumps(messages, ensure_ascii=False)))
            return (
                empty_plan()
                if title == "CheckPlan"
                else discovery(input_context(messages))
            )

        result = self.make_agent(
            handler,
            documents=[
                {
                    "filename": "long.txt",
                    "text": "\n".join("文本" * 1600 for _ in range(30)),
                    "text_complete": True,
                }
            ],
        ).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(result["metadata"]["coverage"]["checked_blocks"], 30)
        self.assertLess(max(sizes), 80000)

    def test_budget_failure_retains_candidates_without_confirming_them(self):
        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                s = context["sources"][0]
                return discovery(
                    context,
                    candidates=[
                        candidate([{"source_id": s["id"], "quote": s["text"]}])
                    ],
                )
            return LLMError("服务错误")

        result = self.make_agent(
            handler, options=AgentOptions(protocol="structured", max_calls=4)
        ).run()
        self.assertTrue(result["error"])
        self.assertEqual(result["report"]["items"], [])
        self.assertEqual(len(result["report"]["pending_items"]), 1)

    def test_cancel_stops_before_model_call(self):
        def ensure():
            raise RuntimeError("已取消")

        agent = self.make_agent(lambda *args: self.fail("不能调用模型"), ensure=ensure)
        with self.assertRaisesRegex(RuntimeError, "已取消"):
            agent.run()


if __name__ == "__main__":
    unittest.main()
