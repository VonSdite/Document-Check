import json
import unittest
from unittest.mock import patch

from app.checks.evidence_tools import execute_tool
from app.contracts.agent import AgentOptions
from app.models.client import LLMError
from app.persistence.checkpoints import MemoryCheckpointStore
from tests import test_evidence_agent as scripted


def followup(*, actions=(), candidates=(), rechecks=(), complete=False):
    return {
        "status": "complete" if complete else "continue",
        "rationale": "必要范围已检查，保留证据不足的待核实项。"
        if complete
        else "根据现有疑点补充核查具体原文。",
        "actions": list(actions),
        "candidates": list(candidates),
        "rechecks": list(rechecks),
    }


def action(name, **arguments):
    return {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


def reference(source):
    return {"source_id": source["id"], "quote": source["text"]}


class AgentFollowupTest(unittest.TestCase):
    def make_agent(self, handler, **kwargs):
        return scripted.EvidenceAgentTest().make_agent(
            handler, followup=True, code="typo", **kwargs
        )

    def test_single_round_finishes_without_unnecessary_tools_or_extra_scan(self):
        calls = []

        def handler(messages, title, tools, force):
            calls.append(title)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(scripted.input_context(messages))
            self.assertEqual(title, "Followup")
            return followup(complete=True)

        result = self.make_agent(handler).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(calls, ["CheckPlan", "Discovery", "Followup"])
        self.assertEqual(result["metadata"]["followup"]["status"], "completed")
        self.assertEqual(result["metadata"]["tool_trace"], [])

    def test_missed_issue_is_found_by_model_selected_search_and_reviewed_in_same_round(
        self,
    ):
        calls, steps = [], 0

        def handler(messages, title, tools, force):
            nonlocal steps
            calls.append(title)
            context = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(context)
            if title == "Followup":
                steps += 1
                if steps == 1:
                    return followup(
                        actions=[action("search_document", query="电压", limit=8)]
                    )
                if steps == 2:
                    self.assertEqual(
                        context["tool_results"][0]["tool"], "search_document"
                    )
                    return followup(
                        candidates=[
                            scripted.candidate(
                                [reference(s) for s in context["sources"]]
                            )
                        ]
                    )
                self.assertEqual(
                    context["findings"][0]["decision"]["outcome"], "confirmed"
                )
                return followup(complete=True)
            self.assertEqual(title, "Review")
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(c["id"], c["evidence"])
                    for c in context["candidates"]
                ],
            }

        result = self.make_agent(handler).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertEqual(
            calls,
            ["CheckPlan", "Discovery", "Followup", "Followup", "Review", "Followup"],
        )
        self.assertEqual(result["metadata"]["model_calls"], 6)
        self.assertEqual(len(result["metadata"]["tool_trace"]), 1)
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])

    def test_new_evidence_can_reopen_pending_or_withdrawn_decision(self):
        for initial in ("insufficient", "explained"):
            with self.subTest(initial=initial):
                reviews = 0
                evidence = []

                def handler(messages, title, tools, force):
                    nonlocal reviews
                    ctx = scripted.input_context(messages)
                    if title == "CheckPlan":
                        return scripted.empty_plan()
                    if title == "Discovery":
                        evidence.extend(reference(s) for s in ctx["sources"])
                        return scripted.discovery(
                            ctx, [scripted.candidate(evidence[:1])]
                        )
                    if title == "Followup":
                        if reviews == 1:
                            return followup(
                                rechecks=[
                                    {
                                        "candidate_id": ctx["findings"][0]["id"],
                                        "evidence": evidence[1:],
                                    }
                                ]
                            )
                        return followup(complete=True)
                    reviews += 1
                    if reviews == 2:
                        self.assertEqual(
                            ctx["additional_evidence"][ctx["candidates"][0]["id"]],
                            evidence[1:],
                        )
                    return {
                        "actions": [],
                        "decisions": [
                            scripted.decision(
                                c["id"],
                                evidence[:1] if reviews == 1 else evidence,
                                outcome=initial if reviews == 1 else "confirmed",
                            )
                            for c in ctx["candidates"]
                        ],
                    }

                result = self.make_agent(handler).run()
                self.assertEqual(result["error"], "")
                self.assertEqual(reviews, 2)
                self.assertEqual(len(result["report"]["items"]), 1)
                self.assertFalse(result["report"]["pending_items"])
                self.assertFalse(result["report"]["withdrawn_items"])
                self.assertEqual(len(result["metadata"]["decision_audit"]), 1)

    def test_unread_or_invented_reference_is_rejected_and_valid_peer_is_reviewed(self):
        steps = 0

        def handler(messages, title, tools, force):
            nonlocal steps
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(ctx)
            if title == "Followup":
                steps += 1
                if steps == 1:
                    valid = reference(ctx["sources"][0])
                    unread = reference(agent.index.blocks[-1].as_dict())
                    self.assertNotIn(
                        unread["source_id"], {s["id"] for s in ctx["sources"]}
                    )
                    return followup(
                        candidates=[
                            scripted.candidate([valid], description="有效候选"),
                            scripted.candidate([unread], description="未经读取的引用"),
                            scripted.candidate(
                                [{**valid, "quote": "编造的原文"}],
                                description="原文不存在",
                            ),
                        ]
                    )
                self.assertTrue(ctx["feedback"])
                return followup(complete=True)
            self.assertEqual(len(ctx["candidates"]), 1)
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(c["id"], c["evidence"]) for c in ctx["candidates"]
                ],
            }

        agent = self.make_agent(
            handler,
            documents=[
                {
                    "filename": "a.txt",
                    "text_complete": True,
                    "blocks": [
                        {"text": f"设备{n}的电压参数", "kind": "paragraph"}
                        for n in range(20)
                    ],
                }
            ],
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(agent.state["candidates"]), 1)
        self.assertEqual(len(result["report"]["items"]), 1)

    def test_repeated_action_reuses_result_and_stops_without_false_completion(self):
        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(ctx)
            return followup(
                actions=[
                    action("read_context", source_id=ctx["sources"][0]["id"], radius=0)
                ]
            )

        result = self.make_agent(handler).run()
        self.assertIn("连续两步", result["error"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["metadata"]["model_calls"], 5)
        self.assertEqual(len(result["metadata"]["tool_trace"]), 1)
        self.assertEqual(result["metadata"]["followup"]["status"], "incomplete")

    def test_dynamic_state_tool_refreshes_after_new_candidate_is_reviewed(self):
        steps = 0

        def handler(messages, title, tools, force):
            nonlocal steps
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(ctx)
            if title == "Review":
                return {
                    "actions": [],
                    "decisions": [
                        scripted.decision(c["id"], c["evidence"])
                        for c in ctx["candidates"]
                    ],
                }
            steps += 1
            if steps in (1, 3):
                return followup(
                    actions=[
                        action("read_check_state", kind="candidates", offset=0, limit=8)
                    ]
                )
            if steps == 2:
                self.assertEqual(ctx["tool_results"][0]["total"], 0)
                return followup(
                    candidates=[scripted.candidate([reference(ctx["sources"][0])])]
                )
            self.assertEqual(ctx["tool_results"][0]["total"], 1)
            self.assertEqual(
                ctx["tool_results"][0]["entries"][0]["decision"]["outcome"], "confirmed"
            )
            return followup(complete=True)

        result = self.make_agent(handler).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["metadata"]["tool_trace"]), 2)

    def test_step_budget_keeps_reviewed_findings_and_marks_followup_incomplete(self):
        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(ctx)
            if title == "Followup":
                return followup(
                    candidates=[scripted.candidate([reference(ctx["sources"][0])])]
                )
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(c["id"], c["evidence"]) for c in ctx["candidates"]
                ],
            }

        result = self.make_agent(
            handler, options=AgentOptions(max_followup_steps=1)
        ).run()
        self.assertIn("补查决策次数上限", result["error"])
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertEqual(result["metadata"]["followup"]["status"], "incomplete")

    def test_request_budget_counts_followup_and_preserves_unreviewed_candidates(self):
        steps = 0

        def handler(messages, title, tools, force):
            nonlocal steps
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(ctx)
            self.assertEqual(title, "Followup")
            steps += 1
            if steps == 1:
                return followup(actions=[action("search_document", query="设备")])
            return followup(
                candidates=[scripted.candidate([reference(ctx["sources"][0])])]
            )

        result = self.make_agent(handler, options=AgentOptions(max_calls=4)).run()
        self.assertIn("调用次数预算", result["error"])
        self.assertEqual(result["metadata"]["model_calls"], 4)
        self.assertEqual(len(result["report"]["pending_items"]), 1)
        self.assertEqual(result["metadata"]["followup"]["status"], "incomplete")

    def test_interrupted_followup_resumes_after_saved_tool_without_repeating_scan(self):
        store = MemoryCheckpointStore()
        steps = 0

        def first(messages, title, tools, force):
            nonlocal steps
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(ctx)
            steps += 1
            if steps == 1:
                return followup(actions=[action("search_document", query="电压")])
            return LLMError("503 暂时不可用")

        failed = self.make_agent(first, store=store).run()
        self.assertIn("503", failed["error"])
        calls = []

        def resumed(messages, title, tools, force):
            calls.append(title)
            ctx = scripted.input_context(messages)
            if title == "Review":
                return {
                    "actions": [],
                    "decisions": [
                        scripted.decision(c["id"], c["evidence"])
                        for c in ctx["candidates"]
                    ],
                }
            self.assertEqual(title, "Followup")
            if not ctx["findings"]:
                self.assertEqual(ctx["tool_results"][0]["tool"], "search_document")
                return followup(
                    candidates=[scripted.candidate([reference(ctx["sources"][0])])]
                )
            return followup(complete=True)

        result = self.make_agent(resumed, store=store).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(calls, ["Followup", "Review", "Followup"])
        self.assertEqual(len(result["metadata"]["tool_trace"]), 1)
        self.assertEqual(
            result["metadata"]["model_calls"], failed["metadata"]["model_calls"] + 3
        )

    def test_interrupted_recheck_does_not_present_old_verdict_as_current(self):
        reviews, refs = 0, []

        def handler(messages, title, tools, force):
            nonlocal reviews
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                refs.extend(reference(s) for s in ctx["sources"])
                return scripted.discovery(ctx, [scripted.candidate(refs[:1])])
            if title == "Followup":
                return followup(
                    rechecks=[
                        {"candidate_id": ctx["findings"][0]["id"], "evidence": refs[1:]}
                    ]
                )
            reviews += 1
            if reviews == 2:
                return LLMError("503 暂时不可用")
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(c["id"], c["evidence"]) for c in ctx["candidates"]
                ],
            }

        result = self.make_agent(handler).run()
        self.assertIn("503", result["error"])
        self.assertFalse(result["report"]["items"])
        self.assertEqual(len(result["report"]["pending_items"]), 1)
        self.assertEqual(
            result["metadata"]["followup"]["steps"][0]["prior_decisions"][0]["outcome"],
            "confirmed",
        )

    def test_finish_cannot_bypass_required_coverage(self):
        agent = self.make_agent(lambda *args: followup(complete=True))
        agent.state["next"] = "followup"
        result = agent.run()
        self.assertIn("尚未完整检查", result["error"])
        self.assertFalse(result["metadata"]["coverage"]["all_checked"])

    def test_finish_requires_requirement_coverage_and_every_candidate_review(self):
        for missing in ("requirement", "decision"):
            with self.subTest(missing=missing):
                agent = self.make_agent(lambda *args: followup(complete=True))
                agent.state.update(
                    next="followup", checked_ids=[b.id for b in agent.index.blocks]
                )
                if missing == "requirement":
                    agent.strategy = "completeness"
                    agent.state["requirements"] = [
                        {
                            "id": "r1",
                            "text": "核对参数",
                            "applicability": "设备A",
                            "origin": "prompt",
                            "source_id": "",
                            "quote": "核对参数",
                        }
                    ]
                else:
                    agent._add_candidate(
                        scripted.candidate([reference(agent.index.blocks[0].as_dict())])
                    )
                result = agent.run()
                self.assertIn("自主补查未完成", result["error"])
                self.assertEqual(result["metadata"]["followup"]["status"], "incomplete")

    def test_recheck_without_new_evidence_keeps_verdict_and_stops_repetition(self):
        reviews = 0

        def handler(messages, title, tools, force):
            nonlocal reviews
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(
                    ctx, [scripted.candidate([reference(ctx["sources"][0])])]
                )
            if title == "Followup":
                item = ctx["findings"][0]
                return followup(
                    rechecks=[
                        {"candidate_id": item["id"], "evidence": item["evidence"]}
                    ]
                )
            reviews += 1
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(c["id"], c["evidence"]) for c in ctx["candidates"]
                ],
            }

        result = self.make_agent(handler).run()
        self.assertIn("连续两步", result["error"])
        self.assertEqual(reviews, 1)
        self.assertEqual(len(result["report"]["items"]), 1)

    def test_state_pagination_does_not_mark_sources_as_read_or_checked(self):
        agent = self.make_agent(lambda *args: None)
        page = execute_tool(
            "read_check_state",
            {"kind": "sources", "offset": 0, "limit": 1},
            agent.index,
            agent.state,
        )
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual(page["total"], 2)
        self.assertNotIn("text", page["entries"][0])
        self.assertEqual(page["sources"], [])
        self.assertEqual(agent.state["checked_ids"], [])
        self.assertEqual(agent.state["seen_sources"], [])

    def test_cancellation_is_checked_before_followup_tool_execution(self):
        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(ctx)
            return followup(actions=[action("search_document", query="电压")])

        agent = self.make_agent(handler)

        def active():
            if agent.state["next"] == "followup_tools":
                raise RuntimeError("用户取消")

        agent.external_ensure_active = active
        with patch("app.tasks.runtime.evidence_agent.execute_tool") as tool:
            with self.assertRaisesRegex(RuntimeError, "用户取消"):
                agent.run()
        tool.assert_not_called()
        self.assertEqual(agent.store.load()["next"], "followup")


if __name__ == "__main__":
    unittest.main()
