import json
import unittest

from app.contracts.agent import AgentOptions
from app.documents.index import DocumentIndex
from app.models.agent import AgentTurn, IncompleteResponse, ToolsUnsupported
from app.models.client import LLMError
from app.persistence.checkpoints import MemoryCheckpointStore
from app.tasks.runtime.evidence_agent import EvidenceAgent, SharedFacts


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
        "global_checks": [],
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
        item=None,
        task_type="document_check",
        shared_facts=None,
    ):
        options = options or AgentOptions(protocol="structured")
        index = DocumentIndex(
            documents
            or [
                {
                    "filename": "a.txt",
                    "text": "设备A 电压220 V\n设备A 电压110 V",
                    "text_complete": True,
                }
            ]
        ).with_standards([s.model_dump(mode="json") for s in options.standards])
        return EvidenceAgent(
            index=index,
            item=item
            or {"code": code, "name": "一致性", "prompt": "检查同一型号参数一致性。"},
            task_type=task_type,
            connection={
                "api_base": "http://model/v1/chat/completions",
                "model_name": "test",
                "max_input_chars": 80000,
            },
            options=options,
            store=store or MemoryCheckpointStore(),
            shared_facts=shared_facts,
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

    def planning_handler(self, context):
        if "prompt_fragment" in context:
            requirements = [
                {
                    "id": str(number),
                    "text": line,
                    "applicability": "遵守完整提示词的共用条件",
                    "origin": "prompt",
                    "source_id": "",
                    "quote": line,
                }
                for number, line in enumerate(context["prompt_fragment"].splitlines())
                if line.startswith("要求")
            ]
            return {**empty_plan(), "requirements": requirements}
        return {
            **discovery(context),
            "observations": [
                {
                    "requirement_id": requirement["id"],
                    "status": "not_found",
                    "rationale": "本批没有涉及该要求",
                    "evidence": [],
                }
                for requirement in context["requirements"]
            ],
        }

    def test_long_prompt_planning_preserves_all_rules_and_global_conditions(self):
        lines = [
            f"要求{number}：" + "核实对象和操作条件。" * 28 for number in range(16)
        ]
        prompt = "共用条件：只核对同一型号、同一版本。\n" + "\n".join(lines)
        fragments = []

        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                self.assertIn(prompt, messages[1]["content"])
                fragments.append(context["prompt_fragment"])
            return self.planning_handler(context)

        agent = self.make_agent(
            handler,
            code="custom",
            item={"code": "custom", "name": "表达检查", "prompt": prompt},
            options=AgentOptions(protocol="structured", max_output_tokens=2048),
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertGreater(len(fragments), 1)
        self.assertEqual("".join(fragments), prompt)
        self.assertEqual({r["quote"] for r in agent.state["requirements"]}, set(lines))
        self.assertEqual(len(agent.state["plans"]), 1)
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        self.assertTrue(
            all(
                agent.state["requirement_coverage"][r["id"]]
                for r in agent.state["requirements"]
            )
        )

    def test_truncated_plan_splits_and_resumes_without_repeating_finished_rules(self):
        prompt = "\n".join(f"要求{n}：核实同一型号和条件下的参数。" for n in range(6))
        store = MemoryCheckpointStore()
        fragments = []
        failed = False

        def handler(messages, title, tools, force):
            nonlocal failed
            context = input_context(messages)
            if title == "CheckPlan":
                fragment = context["prompt_fragment"]
                fragments.append(fragment)
                if fragment == prompt:
                    return IncompleteResponse("输出被截断", finish_reason="length")
                if len(fragments) == 3 and not failed:
                    failed = True
                    return LLMError("503 服务暂时失败")
            return self.planning_handler(context)

        item = {"code": "custom", "name": "表达检查", "prompt": prompt}
        first = self.make_agent(handler, code="custom", item=item, store=store).run()
        self.assertIn("503", first["error"])
        agent = self.make_agent(handler, code="custom", item=item, store=store)
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(fragments.count(prompt), 1)
        self.assertEqual(fragments.count(fragments[1]), 1)
        self.assertEqual(
            {r["quote"] for r in agent.state["requirements"]}, set(prompt.splitlines())
        )
        self.assertEqual(result["metadata"]["model_calls"], len(fragments) + 1)
        self.assertTrue(any("缩小" in d for d in result["metadata"]["diagnostics"]))

    def test_unsplittable_plan_length_recovery_has_only_one_compact_retry(self):
        calls = []

        def handler(messages, title, tools, force):
            calls.append(messages)
            return IncompleteResponse("输出被截断", finish_reason="length")

        result = self.make_agent(handler).run()
        self.assertEqual(len(calls), 2)
        self.assertIn("每次回复 token 上限", result["error"])
        self.assertIn("思考", result["error"])
        self.assertEqual(result["report"]["items"], [])
        self.assertFalse(result["metadata"]["coverage"]["all_checked"])

    def test_filtered_plan_is_not_retried_as_length_truncation(self):
        calls = []

        def handler(messages, title, tools, force):
            calls.append(title)
            return IncompleteResponse("内容过滤", finish_reason="content_filter")

        result = self.make_agent(handler).run()
        self.assertEqual(calls, ["CheckPlan"])
        self.assertIn("内容过滤", result["error"])

    def test_length_recovery_obeys_accumulated_call_budget(self):
        prompt = "\n".join(f"要求{n}：核实同一型号和条件下的参数。" for n in range(16))
        result = self.make_agent(
            lambda *args: IncompleteResponse("输出被截断", finish_reason="length"),
            item={"code": "custom", "name": "表达检查", "prompt": prompt},
            options=AgentOptions(protocol="structured", max_calls=4),
        ).run()
        self.assertEqual(result["metadata"]["model_calls"], 4)
        self.assertIn("调用次数预算", result["error"])
        self.assertEqual(result["report"]["items"], [])

    def test_standard_plan_splitting_preserves_source_ids_and_exact_quotes(self):
        lines = ["紧固扭矩必须为2 N·m。", "安装前必须断开电源。"]
        options = AgentOptions(
            protocol="structured",
            standards=[
                {"name": "交付规范", "version": "2026.1", "text": "\n".join(lines)}
            ],
        )

        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title != "CheckPlan":
                return self.planning_handler(context)
            if not context["sources"]:
                return empty_plan()
            source = context["sources"][0]
            if source["text"] == "\n".join(lines):
                return IncompleteResponse("输出被截断", finish_reason="length")
            quote = source["text"].strip()
            return {
                **empty_plan(),
                "requirements": [
                    {
                        "id": "rule",
                        "text": quote,
                        "applicability": "交付说明",
                        "origin": "standard",
                        "source_id": source["id"],
                        "quote": quote,
                    }
                ],
            }

        agent = self.make_agent(handler, code="custom", options=options)
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual({r["quote"] for r in agent.state["requirements"]}, set(lines))
        self.assertTrue(all(agent.index.verify(r) for r in agent.state["requirements"]))

    def test_existing_plan_cursor_keeps_completed_prompt_and_standard_batches(self):
        options = AgentOptions(
            protocol="structured",
            standards=[{"name": "规范", "version": "1", "text": "电压必须明确。"}],
        )
        store = MemoryCheckpointStore()
        original = self.make_agent(
            lambda *args: empty_plan(), options=options, store=store
        )
        original.state["plan_cursor"] = 1
        original.state["plans"] = [
            {k: v for k, v in empty_plan().items() if k != "requirements"}
        ]
        original.persist()
        planned_sources = []

        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                planned_sources.append(context["sources"])
                return empty_plan()
            return discovery(context)

        result = self.make_agent(handler, options=options, store=store).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(planned_sources), 1)
        self.assertTrue(planned_sources[0])

    def test_global_comparison_finds_problem_absent_from_local_candidates(self):
        calls = []

        def handler(messages, title, tools, force):
            calls.append(title)
            context = input_context(messages)
            if title == "CheckPlan":
                return {**empty_plan(), "global_checks": ["consistency"]}
            if title == "Discovery":
                self.assertTrue(context["extract_facts"])
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

        for code in ("consistency", "custom-parameters", "understandability"):
            with self.subTest(code=code):
                calls.clear()
                result = self.make_agent(handler, code=code).run()
                self.assertEqual(result["error"], "")
                self.assertEqual(len(result["report"]["items"]), 1)
                self.assertEqual(len(result["report"]["items"][0]["evidence_refs"]), 2)
                self.assertEqual(result["metadata"]["global_checks"], ["consistency"])
                self.assertIn("Alignment", calls)
                self.assertIn("Comparison", calls)

    def test_custom_local_prompt_skips_global_work(self):
        calls = []

        def handler(messages, title, tools, force):
            calls.append(title)
            if title == "CheckPlan":
                return empty_plan()
            self.assertEqual(title, "Discovery")
            context = input_context(messages)
            self.assertFalse(context["extract_facts"])
            return discovery(context)

        result = self.make_agent(
            handler,
            item={
                "code": "custom-expression",
                "name": "表达",
                "prompt": "检查本段语句是否通顺。",
            },
        ).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(calls, ["CheckPlan", "Discovery"])
        self.assertEqual(result["metadata"]["global_checks"], [])

    def test_custom_prompt_combines_fact_comparison_and_requirement_coverage(self):
        prompt = "核对同一型号电压的一致性。安装指南必须说明接线前断电。"
        comparisons = []

        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return {
                    **empty_plan(),
                    "global_checks": ["consistency", "completeness"],
                    "requirements": [
                        {
                            "id": "safety",
                            "text": "接线前断电",
                            "applicability": "安装指南",
                            "origin": "prompt",
                            "source_id": "",
                            "quote": "安装指南必须说明接线前断电。",
                        }
                    ],
                }
            if title == "Discovery":
                self.assertTrue(context["extract_facts"])
                return {
                    **discovery(
                        context,
                        facts=[
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
                        ],
                    ),
                    "observations": [
                        {
                            "requirement_id": context["requirements"][0]["id"],
                            "status": "not_found",
                            "rationale": "所有已读来源均未说明断电要求",
                            "evidence": [],
                        }
                    ],
                }
            if title == "Alignment":
                return {
                    "keys": [
                        {"fact_id": f["fact_id"], "attribute_key": "电压"}
                        for f in context["facts"]
                    ]
                }
            if title == "Comparison":
                comparisons.append("facts" if "facts" in context else "requirement")
                if "facts" in context:
                    return {
                        "candidates": [
                            candidate([f["reference"] for f in context["facts"]])
                        ]
                    }
                self.assertTrue(context["coverage"]["all_checked"])
                return {
                    "candidates": [
                        candidate(
                            [],
                            description="未说明接线前断电",
                            absence_claim=True,
                            requirement_ids=[context["requirement"]["id"]],
                        )
                    ]
                }
            return {
                "actions": [],
                "decisions": [
                    decision(c["id"], c["evidence"], description=c["description"])
                    for c in context["candidates"]
                ],
            }

        result = self.make_agent(
            handler,
            item={"code": "custom-safety", "name": "设备安全", "prompt": prompt},
        ).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(comparisons, ["facts", "requirement"])
        self.assertEqual(len(result["report"]["items"]), 2)
        self.assertEqual(
            result["metadata"]["global_checks"], ["consistency", "completeness"]
        )

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
        refs = result["report"]["withdrawn_items"][0]["evidence_refs"]
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["filename"], "a")

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
        self.assertEqual(result["report"]["pending_items"][0]["evidence_refs"], [])

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

    def test_absence_requires_full_coverage_without_positive_or_uncertain_observations(
        self,
    ):
        prompt = "安装指南必须说明接线前断电。"
        for status, complete, expected in (
            ("not_found", True, True),
            ("not_found", False, False),
            ("covered", True, False),
            ("uncertain", True, False),
        ):
            with self.subTest(status=status, text_complete=complete):

                def handler(messages, title, tools, force):
                    context = input_context(messages)
                    if title == "CheckPlan":
                        return {
                            **empty_plan(),
                            "requirements": [
                                {
                                    "id": "safety",
                                    "text": "接线前断电",
                                    "applicability": "安装指南",
                                    "origin": "prompt",
                                    "source_id": "",
                                    "quote": prompt,
                                }
                            ],
                        }
                    if title == "Discovery":
                        req = context["requirements"][0]
                        source = context["sources"][0]
                        return {
                            **discovery(context),
                            "observations": [
                                {
                                    "requirement_id": req["id"],
                                    "status": status,
                                    "rationale": "本批检查结果",
                                    "evidence": [
                                        {
                                            "source_id": source["id"],
                                            "quote": source["text"],
                                        }
                                    ]
                                    if status == "covered"
                                    else [],
                                }
                            ],
                        }
                    if title == "Comparison":
                        req = context["requirement"]
                        return {
                            "candidates": [
                                candidate(
                                    [],
                                    description="未说明接线前断电",
                                    absence_claim=True,
                                    requirement_ids=[req["id"]],
                                )
                            ]
                        }
                    self.assertEqual(
                        context["requirement_observations"][0]["counts"][status], 1
                    )
                    c = context["candidates"][0]
                    return {
                        "actions": [],
                        "decisions": [
                            decision(c["id"], [], description=c["description"])
                        ],
                    }

                result = self.make_agent(
                    handler,
                    item={"code": "completeness", "name": "完整性", "prompt": prompt},
                    documents=[
                        {
                            "filename": "guide.txt",
                            "text": "断电后接线。"
                            if status == "covered"
                            else "连接电源后启动设备。",
                            "text_complete": complete,
                        }
                    ],
                ).run()
                self.assertEqual(bool(result["report"]["items"]), expected)
                if expected:
                    self.assertEqual(
                        result["report"]["items"][0]["evidence_refs"][0]["quote"],
                        prompt,
                    )
                else:
                    self.assertEqual(len(result["report"]["pending_items"]), 1)

    def test_versioned_standard_and_document_evidence_are_both_verified(self):
        standard = {
            "name": "交付规范",
            "version": "2026.1",
            "text": "紧固扭矩必须为2 N·m。",
        }

        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                requirements = []
                if context["sources"]:
                    source = context["sources"][0]
                    requirements = [
                        {
                            "id": "torque",
                            "text": standard["text"],
                            "applicability": "交付安装步骤",
                            "origin": "standard",
                            "source_id": source["id"],
                            "quote": standard["text"],
                        }
                    ]
                return {**empty_plan(), "requirements": requirements}
            if title == "Discovery":
                source = context["sources"][0]
                return {
                    **discovery(context),
                    "observations": [
                        {
                            "requirement_id": context["requirements"][0]["id"],
                            "status": "covered",
                            "rationale": "原文写明扭矩",
                            "evidence": [
                                {"source_id": source["id"], "quote": source["text"]}
                            ],
                        }
                    ],
                }
            if title == "Comparison":
                req = context["requirement"]
                refs = context["observations"][0]["evidence"] + [
                    {"source_id": req["source_id"], "quote": req["quote"]}
                ]
                return {
                    "candidates": [
                        candidate(
                            refs,
                            description="扭矩3 N·m与交付规范2 N·m不一致",
                            requirement_ids=[req["id"]],
                        )
                    ]
                }
            c = context["candidates"][0]
            return {
                "actions": [],
                "decisions": [
                    decision(c["id"], c["evidence"], description=c["description"])
                ],
            }

        result = self.make_agent(
            handler,
            code="compliance",
            documents=[
                {
                    "filename": "guide.txt",
                    "text": "紧固扭矩为3 N·m。",
                    "text_complete": True,
                }
            ],
            options=AgentOptions(protocol="structured", standards=[standard]),
        ).run()
        self.assertEqual(result["error"], "")
        refs = result["report"]["items"][0]["evidence_refs"]
        self.assertEqual({ref["role"] for ref in refs}, {"document", "standard"})
        self.assertTrue(any("2026.1" in ref["position"] for ref in refs))

    def test_cross_language_alignment_compares_opposite_operation_requirements(self):
        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                source = context["sources"][0]
                english = source["role"] == "document_a"
                return discovery(
                    context,
                    facts=[
                        {
                            "subject": "设备",
                            "attribute": "connection rule" if english else "接线规则",
                            "value": "prohibited" if english else "允许",
                            "unit": "",
                            "conditions": "设备通电",
                            "version": "1",
                            "reference": {
                                "source_id": source["id"],
                                "quote": source["text"],
                            },
                        }
                    ],
                )
            if title == "Alignment":
                return {
                    "keys": [
                        {"fact_id": f["fact_id"], "attribute_key": "通电接线要求"}
                        for f in context["facts"]
                    ]
                }
            if title == "Comparison":
                self.assertEqual(len(context["facts"]), 2)
                return {
                    "candidates": [
                        candidate(
                            [f["reference"] for f in context["facts"]],
                            description="禁止通电接线与通电接线的含义相反",
                            severity="critical",
                        )
                    ]
                }
            c = context["candidates"][0]
            return {
                "actions": [],
                "decisions": [
                    decision(c["id"], c["evidence"], description=c["description"])
                ],
            }

        result = self.make_agent(
            handler,
            task_type="language_consistency_check",
            documents=[
                {
                    "filename": "source.txt",
                    "role": "document_a",
                    "text": "Do not connect while powered on.",
                    "text_complete": True,
                },
                {
                    "filename": "译文.txt",
                    "role": "document_b",
                    "text": "设备通电时连接电缆。",
                    "text_complete": True,
                },
            ],
        ).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(result["report"]["items"][0]["severity"], "critical")
        self.assertEqual(
            {r["role"] for r in result["report"]["items"][0]["evidence_refs"]},
            {"document_a", "document_b"},
        )

    def test_shared_facts_are_reused_without_skipping_business_coverage(self):
        pool = SharedFacts()
        reused = []

        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                reused.append(len(context["shared_facts"]))
                facts = (
                    []
                    if context["shared_facts"]
                    else [
                        {
                            "subject": "设备A",
                            "attribute": "电压",
                            "value": source["text"],
                            "unit": "V",
                            "conditions": "运行",
                            "version": "1",
                            "reference": {
                                "source_id": source["id"],
                                "quote": source["text"],
                            },
                        }
                        for source in context["sources"]
                    ]
                )
                return discovery(context, facts=facts)
            if title == "Alignment":
                return {
                    "keys": [
                        {"fact_id": f["fact_id"], "attribute_key": "电压"}
                        for f in context["facts"]
                    ]
                }
            return {"candidates": []}

        first = self.make_agent(handler, shared_facts=pool).run()
        second = self.make_agent(
            handler,
            shared_facts=pool,
            code="custom",
            options=AgentOptions(protocol="structured", strategy="consistency"),
        ).run()
        self.assertEqual(reused, [0, 2])
        self.assertEqual(
            first["metadata"]["model_calls"], second["metadata"]["model_calls"]
        )
        self.assertTrue(second["metadata"]["coverage"]["all_checked"])

    def test_structure_repair_is_bounded_and_repeated_invalid_output_stays_incomplete(
        self,
    ):
        for repaired in (True, False):
            with self.subTest(repaired=repaired):
                calls = []

                def handler(messages, title, tools, force):
                    calls.append(title)
                    if title == "CheckPlan":
                        if len(calls) == 1 or not repaired:
                            return AgentTurn("{broken", finish_reason="stop")
                        return empty_plan()
                    return discovery(input_context(messages))

                result = self.make_agent(handler).run()
                self.assertEqual(calls.count("CheckPlan"), 2)
                self.assertEqual(bool(result["error"]), not repaired)
                if not repaired:
                    self.assertTrue(
                        result["report"]["summary"].startswith("检查未完成")
                    )
                    self.assertFalse(result["metadata"]["coverage"]["all_checked"])

    def test_last_review_round_does_not_confirm_decisions_with_unread_actions(self):
        for native_response in (False, True):
            with self.subTest(native_response=native_response):

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
                    self.assertIsNone(tools)
                    if native_response:
                        return AgentTurn(
                            tool_calls=[
                                {
                                    "id": "extra",
                                    "type": "function",
                                    "function": {
                                        "name": "coverage_status",
                                        "arguments": '{"requirement_id":""}',
                                    },
                                }
                            ],
                            finish_reason="tool_calls",
                        )
                    c = context["candidates"][0]
                    return {
                        "actions": [
                            {
                                "name": "coverage_status",
                                "arguments": '{"requirement_id":""}',
                            }
                        ],
                        "decisions": [decision(c["id"], c["evidence"])],
                    }

                result = self.make_agent(
                    handler, options=AgentOptions(protocol="auto", max_review_rounds=1)
                ).run()
                self.assertEqual(result["error"], "")
                self.assertEqual(result["report"]["items"], [])
                self.assertEqual(len(result["report"]["pending_items"]), 1)
                self.assertEqual(result["metadata"]["model_calls"], 3)

    def test_tool_message_rejection_falls_back_after_successful_native_read(self):
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
            c = context["candidates"][0]
            if force:
                return AgentTurn(
                    tool_calls=[
                        {
                            "id": "read",
                            "type": "function",
                            "function": {
                                "name": "read_context",
                                "arguments": json.dumps(
                                    {
                                        "source_id": c["evidence"][0]["source_id"],
                                        "radius": 1,
                                    }
                                ),
                            },
                        }
                    ],
                    finish_reason="tool_calls",
                )
            if any(m["role"] == "tool" for m in messages):
                return ToolsUnsupported("role tool 不支持")
            self.assertIsNone(tools)
            return {"actions": [], "decisions": [decision(c["id"], c["evidence"])]}

        result = self.make_agent(handler, options=AgentOptions(protocol="auto")).run()
        self.assertEqual(result["error"], "")
        self.assertFalse(result["metadata"]["capabilities"]["native_tools"])
        self.assertEqual(len(result["report"]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
