import unittest

from app.contracts.agent import AgentOptions
from app.documents.index import DocumentIndex
from app.models.client import LLMError
from app.persistence.checkpoints import MemoryCheckpointStore
from app.tasks.runtime.agent_reuse import SharedAgentWork
from tests import test_evidence_agent as scripted


class QuotationRecoveryTest(unittest.TestCase):
    def index(self, text):
        return DocumentIndex(
            [
                {
                    "filename": "sample.pdf",
                    "text_complete": True,
                    "blocks": [{"text": text, "kind": "paragraph"}],
                }
            ]
        )

    def test_pdf_line_wrap_resolves_to_exact_source_without_model_call(self):
        index = self.index("查看电池回路，闭合空开或更\n换熔丝。电压为220\nV。")
        source = index.blocks[0]
        for quote, original in [
            ("闭合空开或更换熔丝。", "闭合空开或更\n换熔丝。"),
            ("电压为220 V。", "电压为220\nV。"),
        ]:
            with self.subTest(quote=quote):
                resolved = index.resolve_reference(
                    {"source_id": source.id, "quote": quote}
                )
                self.assertEqual(resolved["quote"], original)
                self.assertTrue(index.verify(resolved))
        self.assertIsNone(
            index.resolve_reference(
                {"source_id": source.id, "quote": "更换熔丝"}, allowed=set()
            )
        )

    def test_changed_numbers_units_negation_and_word_boundaries_stay_invalid(self):
        index = self.index("电压220 V，不能接通。数值1 20。switch on。")
        for quote in [
            "电压110 V",
            "电压220 A",
            "电压220 V，能接通。",
            "数值120",
            "switchon",
        ]:
            with self.subTest(quote=quote):
                self.assertIsNone(
                    index.resolve_reference(
                        {
                            "source_id": index.blocks[0].id,
                            "quote": quote,
                        }
                    )
                )

    def test_ambiguous_whitespace_match_and_wrong_source_are_not_relocated(self):
        index = self.index("更\n换熔丝。更 换熔丝。")
        self.assertIsNone(
            index.resolve_reference(
                {
                    "source_id": index.blocks[0].id,
                    "quote": "更换熔丝。",
                }
            )
        )
        self.assertIsNone(
            index.resolve_reference(
                {
                    "source_id": "wrong-source",
                    "quote": "更\n换熔丝。",
                }
            )
        )


class ScopedAbsenceTest(unittest.TestCase):
    def make_agent(self):
        agent = scripted.EvidenceAgentTest().make_agent(
            lambda *args: self.fail("验证规则不调用模型"),
            code="understandability",
            documents=[
                {
                    "filename": "manual.txt",
                    "text_complete": True,
                    "text": (
                        "# 电池上电\n验证电流正常。\n# 更换空开\n将新空开置于ON。\n步骤结束。"
                    ),
                }
            ],
        )
        agent.state["requirements"] = [
            {
                "id": "r1",
                "text": "每个操作给出验证方法",
                "applicability": "操作步骤",
                "origin": "prompt",
                "source_id": "",
                "quote": "给出验证方法",
            }
        ]
        agent.state["checked_ids"] = [b.id for b in agent.index.blocks]
        agent.state["seen_sources"] = [b.id for b in agent.index.blocks]
        agent.state["observations"] = [
            {
                "requirement_id": "r1",
                "status": "covered",
                "rationale": "电池上电有验证方法",
                "evidence": [],
                "checked_source_ids": [agent.index.blocks[1].id],
            }
        ]
        return agent

    def accept(self, agent, **changes):
        source = agent.index.blocks[3]
        ref = {"source_id": source.id, "quote": source.text}
        agent._add_candidate(
            scripted.candidate(
                [ref],
                description="更换空开后未说明验证方法。",
                requirement_ids=["r1"],
                **changes,
            )
        )
        c = agent.state["candidates"][-1]
        agent._accept_decision(scripted.decision(c["id"], [ref]))
        return agent.state["decisions"][-1]

    def test_wording_does_not_turn_local_expression_into_document_absence(self):
        agent = self.make_agent()
        result = self.accept(agent)
        self.assertFalse(agent.state["candidates"][0]["absence_claim"])
        self.assertEqual(result["outcome"], "confirmed")

    def test_scoped_omission_is_not_vetoed_by_another_procedure(self):
        for scope in ["excerpt", "section"]:
            with self.subTest(scope=scope):
                agent = self.make_agent()
                result = self.accept(
                    agent,
                    absence_claim=True,
                    absence_scope=scope,
                    scope_source_id=agent.index.blocks[3].id,
                )
                self.assertEqual(result["outcome"], "confirmed")

    def test_section_absence_requires_the_entire_section_to_be_read(self):
        agent = self.make_agent()
        agent.state["seen_sources"].remove(agent.index.blocks[-1].id)
        result = self.accept(
            agent,
            absence_claim=True,
            absence_scope="section",
            scope_source_id=agent.index.blocks[3].id,
        )
        self.assertEqual(result["outcome"], "insufficient")
        self.assertTrue(result["evidence"])

    def test_document_absence_still_requires_unambiguous_complete_coverage(self):
        agent = self.make_agent()
        result = self.accept(agent, absence_claim=True, absence_scope="document")
        self.assertEqual(result["outcome"], "insufficient")

    def test_section_without_a_heading_cannot_claim_complete_scope(self):
        agent = scripted.EvidenceAgentTest().make_agent(lambda *args: None)
        self.assertEqual(
            agent.index.scope_sources(agent.index.blocks[0].id, "section"), []
        )


class TargetedCitationReviewTest(unittest.TestCase):
    def test_failed_reference_retry_preserves_completed_peer_and_resumes_only_pending(
        self,
    ):
        store = MemoryCheckpointStore()
        calls = []

        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(
                    ctx,
                    candidates=[
                        scripted.candidate(
                            [scripted.fact(s)["reference"]], description=f"问题{n}"
                        )
                        for n, s in enumerate(ctx["sources"])
                    ],
                )
            calls.append(len(ctx["candidates"]))
            if len(calls) == 2:
                return LLMError("503 临时失败")
            decisions = [
                scripted.decision(c["id"], c["evidence"]) for c in ctx["candidates"]
            ]
            if len(calls) == 1:
                decisions[0]["counterevidence"] = [
                    {
                        "source_id": ctx["sources"][0]["id"],
                        "quote": "无法定位的引用",
                    }
                ]
            return {"actions": [], "decisions": decisions}

        def run():
            return (
                scripted.EvidenceAgentTest()
                .make_agent(handler, store=store, code="typo")
                .run()
            )

        first = run()
        self.assertIn("503", first["error"])
        self.assertEqual(len(first["report"]["items"]), 1)
        final = run()
        self.assertEqual(final["error"], "")
        self.assertEqual(calls, [2, 1, 1])
        self.assertEqual(len(final["report"]["items"]), 2)

    def test_only_unresolved_candidate_is_retried_and_valid_peer_is_retained(self):
        batches = []

        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(
                    ctx,
                    candidates=[
                        scripted.candidate(
                            [scripted.fact(s)["reference"]], description=f"问题{n}"
                        )
                        for n, s in enumerate(ctx["sources"])
                    ],
                )
            batches.append([c["id"] for c in ctx["candidates"]])
            decisions = [
                scripted.decision(c["id"], c["evidence"]) for c in ctx["candidates"]
            ]
            if len(batches) == 1:
                decisions[0]["counterevidence"] = [
                    {
                        "source_id": ctx["sources"][0]["id"],
                        "quote": "原文概括而非引用",
                    }
                ]
            return {"actions": [], "decisions": decisions}

        result = scripted.EvidenceAgentTest().make_agent(handler, code="typo").run()
        self.assertEqual(result["error"], "")
        self.assertEqual(list(map(len, batches)), [2, 1])
        self.assertEqual(batches[1], batches[0][:1])
        self.assertEqual(len(result["report"]["items"]), 2)
        self.assertEqual(result["metadata"]["model_calls"], 4)

    def test_failed_retry_is_bounded_and_retains_verified_primary_evidence(self):
        reviews = []

        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(
                    ctx,
                    candidates=[
                        scripted.candidate(
                            [
                                scripted.fact(ctx["sources"][0])["reference"],
                            ]
                        )
                    ],
                )
            reviews.append(True)
            c = ctx["candidates"][0]
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(
                        c["id"],
                        c["evidence"],
                        counterevidence=[
                            {
                                "source_id": c["evidence"][0]["source_id"],
                                "quote": "编造的反证",
                            }
                        ],
                    )
                ],
            }

        agent = scripted.EvidenceAgentTest().make_agent(handler, code="typo")
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len(result["report"]["pending_items"]), 1)
        pending = result["report"]["pending_items"][0]
        self.assertTrue(pending["evidence"])
        self.assertEqual(pending["validation"]["model_outcome"], "confirmed")
        self.assertEqual(
            pending["validation"]["validation_reasons"], ["unresolved_reference"]
        )

    def test_one_round_budget_keeps_valid_evidence_without_retry(self):
        agent = scripted.EvidenceAgentTest().make_agent(
            lambda *args: None, options=AgentOptions(max_review_rounds=1)
        )
        ref = scripted.fact(agent.index.blocks[0].as_dict())["reference"]
        agent._add_candidate(scripted.candidate([ref]))
        agent.state["seen_sources"] = [ref["source_id"]]
        agent._accept_decision(
            scripted.decision(
                agent.state["candidates"][0]["id"],
                [ref, {"source_id": ref["source_id"], "quote": "编造的引用"}],
            )
        )
        self.assertEqual(agent.state["decisions"][0]["evidence"], [ref])


class ComplementaryRoundsTest(unittest.TestCase):
    def handler(self, messages, title, tools, force):
        ctx = scripted.input_context(messages)
        if title == "CheckPlan":
            return scripted.empty_plan()
        if title == "Discovery":
            return scripted.discovery(
                ctx,
                candidates=[
                    scripted.candidate(
                        [
                            scripted.fact(ctx["sources"][0])["reference"],
                        ]
                    )
                ],
            )
        return {
            "actions": [],
            "decisions": [
                scripted.decision(c["id"], c["evidence"]) for c in ctx["candidates"]
            ],
        }

    def test_five_rounds_keep_every_detection_pass_with_seven_instead_of_fifteen_calls(
        self,
    ):
        pool = SharedAgentWork()
        discovery_instructions = []

        def handler(messages, title, tools, force):
            if title == "Discovery":
                discovery_instructions.append(
                    messages[-1]["content"].split("输出 JSON schema")[0]
                )
            return self.handler(messages, title, tools, force)

        results = [
            scripted.EvidenceAgentTest()
            .make_agent(
                handler,
                code="typo",
                shared_work=pool,
                round_number=n,
                round_count=5,
            )
            .run()
            for n in range(1, 6)
        ]
        self.assertTrue(all(r["error"] == "" for r in results))
        self.assertEqual(sum(r["metadata"]["model_calls"] for r in results), 7)
        self.assertEqual(sum(r["metadata"]["plan_reused"] for r in results), 4)
        self.assertEqual(sum(r["metadata"]["review_reused"] for r in results), 4)
        self.assertEqual(len({r["metadata"]["round_focus"] for r in results}), 5)
        self.assertEqual(len(set(discovery_instructions)), 5)
        self.assertTrue(all(len(r["report"]["items"]) == 1 for r in results))
        self.assertEqual(
            sum(
                q["stage"] == "discover"
                for r in results
                for q in r["metadata"]["requests"]
            ),
            5,
        )

    def test_changed_prompt_document_or_model_cannot_reuse_prior_conclusions(self):
        for changed in [
            {"item": {"code": "typo", "name": "文字", "prompt": "另一套检查要求"}},
            {
                "documents": [
                    {
                        "filename": "a.txt",
                        "text": "另一设备电压220V",
                        "text_complete": True,
                    }
                ]
            },
            {"connection": {"model_name": "another-model"}},
        ]:
            with self.subTest(changed=changed):
                pool = SharedAgentWork()
                scripted.EvidenceAgentTest().make_agent(
                    self.handler, code="typo", shared_work=pool
                ).run()
                result = (
                    scripted.EvidenceAgentTest()
                    .make_agent(
                        self.handler,
                        code="typo",
                        shared_work=pool,
                        round_number=2,
                        **changed,
                    )
                    .run()
                )
                self.assertEqual(result["error"], "")
                self.assertEqual(result["metadata"]["model_calls"], 3)
                self.assertFalse(result["metadata"]["plan_reused"])
                self.assertEqual(result["metadata"]["review_reused"], 0)

    def test_uncertain_decision_is_reviewed_again(self):
        pool = SharedAgentWork()

        def handler(messages, title, tools, force):
            answer = self.handler(messages, title, tools, force)
            if title == "Review":
                answer["decisions"][0]["outcome"] = "insufficient"
            return answer

        for number in [1, 2]:
            result = (
                scripted.EvidenceAgentTest()
                .make_agent(
                    handler,
                    code="typo",
                    shared_work=pool,
                    round_number=number,
                )
                .run()
            )
            self.assertEqual(result["error"], "")
            self.assertEqual(result["metadata"]["review_reused"], 0)
            self.assertEqual(result["metadata"]["model_calls"], 3 if number == 1 else 2)

    def test_new_evidence_candidate_is_reviewed_while_existing_candidate_is_reused(
        self,
    ):
        pool = SharedAgentWork()
        reviewed, passes = [], []

        def handler(messages, title, tools, force):
            answer = self.handler(messages, title, tools, force)
            ctx = scripted.input_context(messages)
            if title == "Discovery":
                passes.append(True)
                if len(passes) == 2:
                    answer["candidates"].append(
                        scripted.candidate(
                            [scripted.fact(ctx["sources"][-1])["reference"]],
                            description="另一处独立问题",
                        )
                    )
            if title == "Review":
                reviewed.append([c["description"] for c in ctx["candidates"]])
            return answer

        scripted.EvidenceAgentTest().make_agent(
            handler, code="typo", shared_work=pool
        ).run()
        final = (
            scripted.EvidenceAgentTest()
            .make_agent(
                handler,
                code="typo",
                shared_work=pool,
                round_number=2,
            )
            .run()
        )
        self.assertEqual(final["error"], "")
        self.assertEqual(len(final["report"]["items"]), 2)
        self.assertEqual(final["metadata"]["review_reused"], 1)
        self.assertEqual(reviewed[1], ["另一处独立问题"])

    def test_visual_guardrail_pending_result_is_not_reused_as_confirmed(self):
        pool = SharedAgentWork()

        def handler(messages, title, tools, force):
            answer = self.handler(messages, title, tools, force)
            if title == "Discovery":
                answer["candidates"][0]["description"] = (
                    "文档缺少图1，应提供系统架构图。"
                )
            if title == "Review":
                answer["decisions"][0]["description"] = (
                    "文档缺少图1，应提供系统架构图。"
                )
            return answer

        for number in [1, 2]:
            result = (
                scripted.EvidenceAgentTest()
                .make_agent(
                    handler,
                    code="typo",
                    shared_work=pool,
                    round_number=number,
                )
                .run()
            )
            self.assertEqual(result["error"], "")
            self.assertEqual(len(result["report"]["pending_items"]), 1)
            self.assertEqual(result["metadata"]["review_reused"], 0)
            audit = next(iter(result["metadata"]["decision_audit"].values()))
            self.assertEqual(audit["final_outcome"], "insufficient")


class FactReferenceRecoveryTest(unittest.TestCase):
    def test_line_wrapped_fact_reaches_comparison_and_keeps_original_source_quote(self):
        stages = []

        def handler(messages, title, tools, force):
            stages.append(title)
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                facts = [scripted.fact(s, str(n)) for n, s in enumerate(ctx["sources"])]
                for fact in facts:
                    fact["reference"]["quote"] = fact["reference"]["quote"].replace(
                        "\n", ""
                    )
                return scripted.discovery(ctx, facts=facts)
            if title == "Alignment":
                return {
                    "keys": [
                        {"fact_id": fact["fact_id"], "attribute_key": "电压"}
                        for fact in ctx["facts"]
                    ]
                }
            self.assertEqual(title, "Comparison")
            return {"candidates": []}

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            documents=[
                {
                    "filename": "manual.pdf",
                    "text_complete": True,
                    "blocks": [
                        {"text": "设备A的额定电\n压为220 V。", "kind": "paragraph"},
                        {"text": "设备A的额定电\n压为110 V。", "kind": "paragraph"},
                    ],
                }
            ],
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertIn("Comparison", stages)
        self.assertEqual(len(agent.state["facts"]), 2)
        self.assertTrue(
            all("\n" in fact["reference"]["quote"] for fact in agent.state["facts"])
        )
        view = {**agent.index.blocks[0].as_dict(), "text": "设备A的额定电"}
        self.assertIsNone(
            agent._resolve_batch_reference(agent.state["facts"][0]["reference"], [view])
        )


class SaturatedDiscoveryTest(unittest.TestCase):
    def test_fact_limit_splits_sources_without_discarding_verified_facts(self):
        discoveries = []

        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                discoveries.append(len(ctx["sources"]))
                facts = []
                for source in ctx["sources"]:
                    for line in source["text"].splitlines():
                        attribute, value = line.split("=")
                        facts.append(
                            {
                                **scripted.fact(source, value),
                                "attribute": attribute,
                                "reference": {"source_id": source["id"], "quote": line},
                            }
                        )
                return scripted.discovery(ctx, facts=facts)
            self.assertEqual(title, "Alignment")
            return {
                "keys": [
                    {"fact_id": fact["fact_id"], "attribute_key": fact["attribute"]}
                    for fact in ctx["facts"]
                ]
            }

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            documents=[
                {
                    "filename": "parameters.txt",
                    "text_complete": True,
                    "blocks": [
                        {
                            "text": "\n".join(
                                f"属性{n}={n}" for n in range(start, start + 100)
                            ),
                            "kind": "paragraph",
                        }
                        for start in [0, 100]
                    ],
                }
            ],
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(discoveries, [2, 1, 1])
        self.assertEqual(len(agent.state["facts"]), 200)
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        self.assertEqual(result["metadata"]["saturated_batches"], 1)

    def test_global_comparison_limit_splits_checks_and_keeps_additional_issue(self):
        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "Comparison":
                if "checks" in ctx:
                    ref = ctx["checks"][0]["facts"][0]["reference"]
                    return {
                        "candidates": [
                            scripted.candidate([ref], description=f"候选{n}")
                            for n in range(100)
                        ]
                    }
                return {
                    "candidates": [
                        scripted.candidate(
                            [ctx["facts"][0]["reference"]],
                            description="独立对照组的新问题",
                        )
                    ]
                    if ctx["facts"][0]["value"] == "2"
                    else []
                }
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(
                        candidate["id"],
                        candidate["evidence"],
                        description=candidate["description"],
                    )
                    for candidate in ctx["candidates"]
                ],
            }

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            documents=[
                {
                    "filename": "parameters.txt",
                    "text_complete": True,
                    "text": "\n".join(f"设备参数{n}值{n}" for n in range(4)),
                }
            ],
        )
        facts = [
            scripted.fact(source.as_dict(), str(n))
            for n, source in enumerate(agent.index.blocks)
        ]
        agent.state.update(
            next="compare", facts=facts, checked_ids=[b.id for b in agent.index.blocks]
        )
        agent._cached_jobs["compare"] = [
            {"checks": [{"facts": facts[:2]}, {"facts": facts[2:]}]}
        ]
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(result["metadata"]["saturated_batches"], 1)
        self.assertEqual(len(result["report"]["items"]), 101)
        self.assertTrue(
            any(
                item["description"] == "独立对照组的新问题"
                for item in result["report"]["items"]
            )
        )

    def test_full_document_candidate_limit_continues_scanning_and_keeps_new_issue(self):
        def handler(messages, title, tools, force):
            ctx = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "FullDocumentDiscovery":
                ref = scripted.fact(ctx["sources"][0])["reference"]
                return {
                    "all_sources_checked": True,
                    "observations": [],
                    "candidates": [
                        scripted.candidate([ref], description=f"候选{n}")
                        for n in range(100)
                    ],
                }
            if title == "Discovery":
                return scripted.discovery(
                    ctx,
                    candidates=[
                        scripted.candidate(
                            [scripted.fact(ctx["sources"][-1])["reference"]],
                            description="补查发现的新问题",
                        )
                    ]
                    if ctx["sources"][-1]["ordinal"] == 1
                    else [],
                )
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(
                        c["id"], c["evidence"], description=c["description"]
                    )
                    for c in ctx["candidates"]
                ],
            }

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            code="typo",
            options=AgentOptions(chunk_chars=1000),
            documents=[
                {
                    "filename": "a.txt",
                    "text_complete": True,
                    "text": "甲" * 450 + "\n" + "乙" * 450,
                }
            ],
        )
        agent.options = agent.options.model_copy(update={"full_document": True})
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(result["metadata"]["saturated_batches"], 1)
        self.assertEqual(result["metadata"]["discovery_mode"], "chunked")
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        self.assertEqual(len(result["report"]["items"]), 101)
        self.assertTrue(
            any(
                i["description"] == "补查发现的新问题"
                for i in result["report"]["items"]
            )
        )


if __name__ == "__main__":
    unittest.main()
