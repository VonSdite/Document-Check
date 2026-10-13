import json
import threading
import unittest
from collections import Counter

from app.contracts.agent import AgentOptions
from app.documents.index import DocumentIndex, text_blocks
from app.models.agent import IncompleteResponse
from app.models.client import LLMError
from app.persistence.checkpoints import MemoryCheckpointStore
from tests import test_evidence_agent as scripted
from tests.test_evidence_agent import (
    candidate,
    decision,
    discovery,
    empty_plan,
    fact,
    input_context,
)


class AgentPerformanceTest(unittest.TestCase):
    def test_full_document_checks_all_text_task_types_without_intermediate_facts(self):
        for task_type in (
            "document_check",
            "consistency_check",
            "language_consistency_check",
        ):
            with self.subTest(task_type=task_type):
                calls = []

                def handler(messages, title, tools, force):
                    calls.append(title)
                    context = input_context(messages)
                    if title == "CheckPlan":
                        return empty_plan()
                    if title == "FullDocumentDiscovery":
                        self.assertTrue(context["whole_document"])
                        self.assertFalse(context["extract_facts"])
                        self.assertEqual(
                            {s["role"] for s in context["sources"]},
                            {"master", "related"},
                        )
                        return {
                            "all_sources_checked": True,
                            "observations": [],
                            "candidates": [
                                candidate(
                                    [
                                        {"source_id": s["id"], "quote": s["text"]}
                                        for s in context["sources"]
                                    ]
                                )
                            ],
                        }
                    self.assertEqual(title, "Review")
                    item = context["candidates"][0]
                    return {
                        "actions": [],
                        "decisions": [decision(item["id"], item["evidence"])],
                    }

                agent = scripted.EvidenceAgentTest().make_agent(
                    handler,
                    task_type=task_type,
                    documents=[
                        {
                            "filename": "a.txt",
                            "role": "master",
                            "text": "设备A电压220 V",
                            "text_complete": True,
                        },
                        {
                            "filename": "b.txt",
                            "role": "related",
                            "text": "Device A voltage 110 V",
                            "text_complete": True,
                        },
                    ],
                )
                agent.options = agent.options.model_copy(update={"full_document": True})
                result = agent.run()
                self.assertEqual(result["error"], "")
                self.assertTrue(result["metadata"]["coverage"]["all_checked"])
                self.assertEqual(result["metadata"]["discovery_mode"], "whole_document")
                self.assertEqual(
                    calls, ["CheckPlan", "FullDocumentDiscovery", "Review"]
                )
                self.assertEqual(len(result["report"]["items"][0]["evidence_refs"]), 2)

    def test_full_document_fallback_rechecks_every_source_and_resumes_chunks(self):
        for reason in ("length", "incomplete", "observations", "schema", "context"):
            with self.subTest(reason=reason):
                store = MemoryCheckpointStore()
                calls = []
                fail = True

                def handler(messages, title, tools, force):
                    nonlocal fail
                    calls.append(title)
                    context = input_context(messages)
                    if title == "CheckPlan":
                        return empty_plan()
                    if title == "FullDocumentDiscovery":
                        if reason == "schema":
                            return {"wrong": "structure"}
                        if reason == "context":
                            return LLMError("maximum context length exceeded")
                        if reason == "length":
                            return IncompleteResponse(
                                "输出截断", finish_reason="length"
                            )
                        return {
                            "all_sources_checked": reason != "incomplete",
                            "candidates": [],
                            "observations": [
                                {
                                    "requirement_id": "unknown",
                                    "status": "not_found",
                                    "rationale": "未知要求",
                                    "evidence": [],
                                }
                            ]
                            if reason == "observations"
                            else [],
                        }
                    self.assertEqual(title, "Discovery")
                    if fail:
                        fail = False
                        return LLMError("503 服务暂时失败")
                    return discovery(context)

                def make():
                    agent = scripted.EvidenceAgentTest().make_agent(
                        handler, store=store
                    )
                    agent.options = agent.options.model_copy(
                        update={"full_document": True}
                    )
                    return agent

                result = make().run()
                self.assertIn("503", result["error"])
                self.assertEqual(result["metadata"]["coverage"]["checked_blocks"], 0)
                result = make().run()
                self.assertEqual(result["error"], "")
                self.assertTrue(result["metadata"]["coverage"]["all_checked"])
                self.assertEqual(result["metadata"]["discovery_mode"], "chunked")
                self.assertEqual(
                    calls.count("FullDocumentDiscovery"), 2 if reason == "schema" else 1
                )
                self.assertEqual(calls.count("CheckPlan"), 1)

    def test_full_document_eligibility_includes_prompt_and_schema_budget(self):
        agent = scripted.EvidenceAgentTest().make_agent(
            lambda *args: None, connection={"max_input_chars": 80000}
        )
        agent.options = agent.options.model_copy(update={"full_document": True})
        self.assertEqual(len(agent._discovery_jobs()), 1)
        self.assertEqual(agent.state["discovery_mode"], "whole_document")
        agent._cached_jobs.clear()
        agent.item = {**agent.item, "prompt": "长规则。" * 20000}
        agent._discovery_jobs()
        self.assertEqual(agent.state["discovery_mode"], "chunked")

    def test_full_document_requirement_coverage_ignores_chunk_pack_count(self):
        prompt = "要求" + "核实安全操作。" * 70

        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return {
                    **empty_plan(),
                    "requirements": [
                        {
                            "id": str(i),
                            "text": f"第{i}项：{prompt}",
                            "applicability": "全部正文",
                            "origin": "prompt",
                            "source_id": "",
                            "quote": prompt,
                        }
                        for i in range(4)
                    ],
                }
            self.assertEqual(title, "FullDocumentDiscovery")
            return {
                "all_sources_checked": True,
                "candidates": [],
                "observations": [
                    {
                        "requirement_id": r["id"],
                        "status": "not_found",
                        "rationale": "未找到",
                        "evidence": [],
                    }
                    for r in context["requirements"]
                ],
            }

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            item={"code": "completeness", "name": "完整性", "prompt": prompt},
            options=AgentOptions(chunk_chars=1000),
        )
        agent.options = agent.options.model_copy(update={"full_document": True})
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        self.assertEqual(len(agent.state["requirement_coverage"]), 4)
        for sources in agent.state["requirement_coverage"].values():
            self.assertTrue(agent.index.coverage(sources)["all_checked"])

    def test_page_compaction_preserves_every_line_and_section_table_boundaries(self):
        text = "\n".join(
            f"[第{page}页]\n# 章节{page}\n"
            + "\n".join(f"第{page}页内容{line:03d}，参数正常。" for line in range(80))
            + f"\n[PDF结构化表格 T{page} 开始；x]\n<table>220 V</table>\n[PDF结构化表格 T{page} 结束]"
            for page in range(1, 11)
        )
        raw = text_blocks(text)
        index = DocumentIndex(
            [{"filename": "guide.pdf", "blocks": raw, "text_complete": True}]
        )
        self.assertLess(len(index.blocks), len(raw) // 10)
        for original in raw:
            matches = [
                b
                for b in index.blocks
                if original["text"] in b.text and b.section == original["section"]
            ]
            self.assertEqual(len(matches), 1)
            source = matches[0]
            self.assertEqual(source.section, original["section"])
            self.assertEqual(
                source.location.split(" / ")[0], original["location"].split(" / ")[0]
            )
            self.assertTrue(
                index.verify({"source_id": source.id, "quote": original["text"]})
            )
            if original["kind"] == "table":
                self.assertEqual(source.text, original["text"])
        self.assertEqual(
            index.signature,
            DocumentIndex(
                [{"filename": "guide.pdf", "blocks": raw, "text_complete": True}]
            ).signature,
        )
        self.assertTrue(index.coverage([b.id for b in index.blocks])["all_checked"])

    def test_same_filename_different_roles_keep_discovery_batches_separate(self):
        index = DocumentIndex(
            [
                {"filename": "guide.pdf", "role": role, "text": "参数220 V"}
                for role in ("master", "related")
            ]
        )
        chunks = index.chunks(20000)
        self.assertEqual(len(chunks), 2)
        self.assertNotEqual(chunks[0][0].document_id, chunks[1][0].document_id)

    def test_local_rules_skip_observations_but_global_scope_keeps_them(self):
        for code, global_checks, expected in [
            ("understandability", [], False),
            ("typo", [], False),
            ("consistency", [], False),
            ("understandability", ["completeness"], True),
            ("compliance", [], True),
        ]:
            with self.subTest(code=code, global_checks=global_checks):

                def handler(messages, title, tools, force):
                    context = input_context(messages)
                    if title == "CheckPlan":
                        return {
                            **empty_plan(),
                            "global_checks": global_checks,
                            "requirements": [
                                {
                                    "id": "r1",
                                    "text": "核实要求",
                                    "applicability": "本文件",
                                    "origin": "prompt",
                                    "source_id": "",
                                    "quote": "核实要求",
                                }
                            ],
                        }
                    if title == "Comparison":
                        return {"candidates": []}
                    self.assertEqual(
                        bool(context["observation_requirement_ids"]), expected
                    )
                    self.assertEqual(len(context["requirements"]), 1)
                    return {
                        **discovery(context),
                        "observations": [
                            {
                                "requirement_id": rid,
                                "status": "not_found",
                                "rationale": "本批未找到",
                                "evidence": [],
                            }
                            for rid in context["observation_requirement_ids"]
                        ],
                    }

                agent = scripted.EvidenceAgentTest().make_agent(
                    handler, item={"code": code, "name": "检查", "prompt": "核实要求"}
                )
                self.assertEqual(agent.run()["error"], "")
                self.assertEqual(bool(agent.state["observations"]), expected)
                self.assertEqual(bool(agent.state["requirement_coverage"]), expected)

    def test_auto_review_uses_supplied_evidence_without_capability_probe(self):
        def handler(messages, title, tools, force):
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                source = context["sources"][0]
                return discovery(
                    context,
                    candidates=[
                        candidate(
                            [{"source_id": source["id"], "quote": source["text"]}]
                        )
                    ],
                )
            self.assertIsNone(tools)
            self.assertIsNone(force)
            item = context["candidates"][0]
            return {
                "actions": [],
                "decisions": [decision(item["id"], item["evidence"])],
            }

        result = (
            scripted.EvidenceAgentTest()
            .make_agent(handler, options=AgentOptions(protocol="auto"))
            .run()
        )
        self.assertEqual(result["error"], "")
        self.assertEqual(result["metadata"]["model_calls"], 3)
        self.assertEqual(len(result["report"]["items"]), 1)

    def test_parallel_review_resumes_successful_later_batches_and_isolates_tools(self):
        store = MemoryCheckpointStore()
        barrier = threading.Barrier(3)
        calls = Counter()
        lock = threading.Lock()
        fail = True

        def handler(messages, title, tools, force):
            nonlocal fail
            context = input_context(messages)
            if title == "CheckPlan":
                return empty_plan()
            if title == "Discovery":
                return discovery(
                    context,
                    candidates=[
                        candidate([{"source_id": s["id"], "quote": s["text"]}])
                        for s in context["sources"]
                    ],
                )
            items = context["candidates"]
            key = items[0]["evidence"][0]["quote"]
            with lock:
                calls[key] += 1
                initial = calls[key] == 1
            if initial:
                barrier.wait(timeout=3)
                if key == "来源0" and fail:
                    fail = False
                    return LLMError("503 服务暂时失败")
                return {
                    "actions": [
                        {
                            "name": "read_context",
                            "arguments": json.dumps(
                                {
                                    "source_id": items[0]["evidence"][0]["source_id"],
                                    "radius": 1,
                                }
                            ),
                        }
                    ],
                    "decisions": [],
                }
            return {
                "actions": [],
                "decisions": [decision(c["id"], c["evidence"]) for c in items],
            }

        def make():
            return scripted.EvidenceAgentTest().make_agent(
                handler,
                documents=[
                    {
                        "filename": "a.txt",
                        "text": "\n".join(f"来源{i}" for i in range(6)),
                        "text_complete": True,
                    }
                ],
                options=AgentOptions(protocol="auto", review_batch_size=2),
                parallelism=3,
                store=store,
            )

        first = make().run()
        self.assertIn("503", first["error"])
        self.assertEqual(set(store.load()["batch_outputs"]["review"]), {"2", "4"})
        result = make().run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["report"]["items"]), 6)
        self.assertEqual(dict(calls), {"来源0": 2, "来源2": 2, "来源4": 2})
        self.assertEqual(len(result["metadata"]["tool_trace"]), 2)
        self.assertEqual(result["metadata"]["model_calls"], 8)

    def test_packed_comparisons_keep_all_groups_when_split(self):
        agent = scripted.EvidenceAgentTest().make_agent(lambda *args: None)
        agent.state["facts"] = [
            {**fact(source.as_dict()), "attribute": f"参数{number}"}
            for number in range(40)
            for source in agent.index.blocks
        ]
        jobs = agent._comparison_jobs()
        self.assertLess(len(jobs), 10)
        groups = []
        pending = list(jobs)
        while pending:
            job = pending.pop(0)
            if "checks" in job:
                pending.extend(agent._split_comparison_job(job))
            else:
                groups.append(job)
        self.assertEqual(len(groups), 40)
        self.assertEqual(
            {group["facts"][0]["attribute"] for group in groups},
            {f"参数{i}" for i in range(40)},
        )
        for group in groups:
            self.assertEqual(len(group["facts"]), 2)
            self.assertEqual(len({f["attribute"] for f in group["facts"]}), 1)


if __name__ == "__main__":
    unittest.main()
