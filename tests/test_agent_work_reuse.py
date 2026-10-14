import copy
import unittest
from collections import Counter
from itertools import combinations, product

from app.contracts.agent import AgentOptions
from app.models.agent import IncompleteResponse
from app.models.client import LLMError
from app.persistence.checkpoints import MemoryCheckpointStore
from app.tasks.runtime.evidence_agent import SharedFacts
from tests import test_evidence_agent as scripted


def comparison_pairs(job):
    if "checks" in job:
        return [pair for check in job["checks"] for pair in comparison_pairs(check)]
    pairs = (
        combinations(job["facts"], 2)
        if "facts" in job
        else product(job["left_facts"], job["right_facts"])
    )
    return [tuple(sorted((a["value"], b["value"]))) for a, b in pairs]


class AgentWorkReuseTest(unittest.TestCase):
    def make_agent(self, handler, **kwargs):
        return scripted.EvidenceAgentTest().make_agent(handler, **kwargs)

    def test_repeated_alignment_inputs_keep_all_facts_and_comparisons(self):
        for task_type in (
            "document_check",
            "consistency_check",
            "language_consistency_check",
        ):
            with self.subTest(task_type=task_type):
                aligned, compared = [], []

                def handler(messages, title, tools, force):
                    context = scripted.input_context(messages)
                    if title == "CheckPlan":
                        return scripted.empty_plan()
                    if title == "Discovery":
                        return scripted.discovery(
                            context,
                            facts=[
                                scripted.fact(source, str(source["ordinal"]))
                                for source in context["sources"]
                            ],
                        )
                    if title == "Alignment":
                        aligned.extend(context["facts"])
                        return {
                            "keys": [
                                {
                                    "fact_id": fact["fact_id"],
                                    "attribute_key": "额定电压",
                                }
                                for fact in context["facts"]
                            ]
                        }
                    self.assertEqual(title, "Comparison")
                    compared.extend(comparison_pairs(context))
                    return {"candidates": []}

                agent = self.make_agent(
                    handler,
                    task_type=task_type,
                    documents=[
                        {
                            "filename": "parameters.txt",
                            "text": "\n".join(
                                f"设备 A 电压 {n} V。" for n in range(30)
                            ),
                            "text_complete": True,
                        }
                    ],
                )
                result = agent.run()
                self.assertEqual(result["error"], "")
                self.assertTrue(result["metadata"]["coverage"]["all_checked"])
                self.assertEqual(len(aligned), 1)
                self.assertEqual(aligned[0]["unit"], "V")
                self.assertEqual(len(agent.state["facts"]), 30)
                self.assertEqual(len(agent.state["fact_keys"]), 30)
                self.assertEqual(set(agent.state["fact_keys"].values()), {"额定电压"})
                self.assertEqual(
                    Counter(compared),
                    Counter(
                        tuple(sorted(pair))
                        for pair in combinations(map(str, range(30)), 2)
                    ),
                )

    def test_alignment_preserves_differences_in_object_unit_condition_and_version(self):
        agent = self.make_agent(lambda *args: None)
        base = scripted.fact(agent.index.blocks[0].as_dict())
        agent.state["facts"] = [
            base,
            {**base, "value": "110"},
            {**base, "subject": "设备 B"},
            {**base, "attribute": "电流"},
            {**base, "unit": "kV"},
            {**base, "conditions": "待机"},
            {**base, "version": "2"},
        ]
        jobs, members = agent._alignment_jobs()
        self.assertEqual(sum(map(len, jobs)), 6)
        self.assertEqual(sorted(map(len, members.values())), [1, 1, 1, 1, 1, 2])

    def test_alignment_reuse_survives_split_recovery_and_resume(self):
        store = MemoryCheckpointStore()
        aligned = []
        failed = False

        def handler(messages, title, tools, force):
            nonlocal failed
            context = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(
                    context,
                    facts=[
                        {
                            **scripted.fact(source, str(source["ordinal"])),
                            "attribute": "额定电压"
                            if source["ordinal"] % 2
                            else "工作电压",
                        }
                        for source in context["sources"]
                    ],
                )
            if title == "Alignment":
                if len(context["facts"]) > 1:
                    return IncompleteResponse("截断", finish_reason="length")
                if aligned and not failed:
                    failed = True
                    return LLMError("503 服务暂时失败")
                representative = context["facts"][0]["fact_id"]
                aligned.append(representative)
                return {"keys": [{"fact_id": representative, "attribute_key": "电压"}]}
            self.assertEqual(title, "Comparison")
            return {"candidates": []}

        def make():
            return self.make_agent(
                handler,
                store=store,
                documents=[
                    {
                        "filename": "a.txt",
                        "text": "\n".join(f"设备 A 电压 {n} V。" for n in range(12)),
                        "text_complete": True,
                    }
                ],
            )

        first = make().run()
        self.assertIn("503", first["error"])
        self.assertEqual(len(store.load()["fact_keys"]), 6)
        resumed = make()
        result = resumed.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(aligned), 2)
        self.assertEqual(len(set(aligned)), 2)
        self.assertEqual(len(resumed.state["fact_keys"]), 12)
        self.assertEqual(len(resumed.state["facts"]), 12)

    def test_large_comparisons_and_recovery_cover_each_pair_once(self):
        agent = self.make_agent(lambda *args: None)
        agent.max_input = 9000
        base = scripted.fact(agent.index.blocks[0].as_dict())
        agent.state["facts"] = [
            {**base, "value": str(n), "conditions": "运行条件。" * 100}
            for n in range(30)
        ]
        jobs = agent._comparison_jobs()
        self.assertTrue(any("left_facts" in job for job in jobs))
        expected = Counter(
            tuple(sorted(pair)) for pair in combinations(map(str, range(30)), 2)
        )
        self.assertEqual(
            Counter(pair for job in jobs for pair in comparison_pairs(job)), expected
        )
        pending, recovered = list(jobs), []
        while pending:
            job = pending.pop()
            if len(comparison_pairs(job)) > 1:
                smaller = agent._split_comparison_job(job)
                self.assertTrue(smaller)
                pending.extend(smaller)
            else:
                recovered.extend(comparison_pairs(job))
        self.assertEqual(Counter(recovered), expected)

    def test_repeated_candidates_use_one_review_and_keep_critical_severity(self):
        reviewed = []

        def handler(messages, title, tools, force):
            context = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                refs = [
                    {"source_id": source["id"], "quote": source["text"]}
                    for source in context["sources"]
                ]
                return scripted.discovery(
                    context,
                    candidates=[
                        scripted.candidate(refs, severity="low"),
                        scripted.candidate(
                            [*reversed(refs), refs[0]],
                            description=" 同一设备的电压不一致\n",
                            severity="critical",
                        ),
                        scripted.candidate(refs),
                    ],
                )
            self.assertEqual(title, "Review")
            reviewed.extend(context["candidates"])
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(item["id"], item["evidence"])
                    for item in context["candidates"]
                ],
            }

        result = self.make_agent(
            handler,
            code="typo",
            options=AgentOptions(max_candidates=1, review_batch_size=1),
        ).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(len(reviewed[0]["evidence"]), 2)
        self.assertEqual(result["metadata"]["model_calls"], 3)
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertEqual(result["report"]["items"][0]["severity"], "critical")

    def test_candidates_with_different_scope_or_kind_remain_separate(self):
        def handler(messages, title, tools, force):
            context = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                refs = [
                    scripted.fact(source)["reference"] for source in context["sources"]
                ]
                return scripted.discovery(
                    context,
                    candidates=[
                        scripted.candidate(refs, applicability="运行状态"),
                        scripted.candidate(refs, applicability="待机状态"),
                        scripted.candidate(
                            refs, applicability="运行状态", kind="suggestion"
                        ),
                    ],
                )
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(item["id"], item["evidence"])
                    for item in context["candidates"]
                ],
            }

        agent = self.make_agent(handler, code="typo")
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(agent.state["candidates"]), 3)
        self.assertEqual(
            Counter(item["status"] for item in result["report"]["items"]),
            {"issue": 2, "suggestion": 1},
        )

    def test_same_quotation_in_different_documents_retains_both_locations(self):
        def handler(messages, title, tools, force):
            context = scripted.input_context(messages)
            if title == "CheckPlan":
                return scripted.empty_plan()
            if title == "Discovery":
                return scripted.discovery(
                    context,
                    candidates=[
                        scripted.candidate([scripted.fact(source)["reference"]])
                        for source in context["sources"]
                    ],
                )
            return {
                "actions": [],
                "decisions": [
                    scripted.decision(item["id"], item["evidence"])
                    for item in context["candidates"]
                ],
            }

        result = self.make_agent(
            handler,
            code="typo",
            documents=[
                {"filename": name, "text": "同一段错误原文。", "text_complete": True}
                for name in ("a.txt", "b.txt")
            ],
        ).run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["report"]["items"]), 2)
        self.assertEqual(
            {
                ref["filename"]
                for item in result["report"]["items"]
                for ref in item["evidence_refs"]
            },
            {"a.txt", "b.txt"},
        )

    def test_shared_fact_cache_deduplicates_content_and_isolates_references(self):
        pool = SharedFacts()
        agent = self.make_agent(lambda *args: None)
        original = [scripted.fact(source.as_dict()) for source in agent.index.blocks]
        inserted = copy.deepcopy(original)
        pool.put("task-a", inserted)
        pool.put(
            "task-a",
            [
                {key: value for key, value in reversed(list(fact.items()))}
                for fact in original
            ],
        )
        inserted[0]["reference"]["quote"] = "缓存外修改"
        ids = {fact["reference"]["source_id"] for fact in original}
        received = pool.get("task-a", ids)
        self.assertEqual(received, original)
        received[0]["reference"]["quote"] = "返回值修改"
        self.assertEqual(pool.get("task-a", ids), original)
        self.assertEqual(pool.get("task-b", ids), [])
        self.assertEqual(
            pool.get("task-a", {original[1]["reference"]["source_id"]}), [original[1]]
        )


if __name__ == "__main__":
    unittest.main()
