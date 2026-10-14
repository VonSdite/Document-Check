import unittest

from app.contracts.agent import AgentOptions, Discovery, Review
from app.tasks.runtime.evidence_agent import REVIEW_INSTRUCTIONS
from tests import test_evidence_agent as scripted


def table_document():
    return {
        "filename": "参数.docx",
        "text_complete": True,
        "blocks": [
            {"text": "设备A参数", "kind": "heading", "section": "设备A"},
            {"text": "仅适用于低温工况，电压以表中单位为准。", "section": "设备A"},
            *[
                {
                    "kind": "table",
                    "text": "型号 | 电压 / mV"
                    if i == 0
                    else f"设备A-{i} | 220000 " + "说明。" * 150,
                    "section": "设备A",
                    "location": f"表格1 > 第{i + 1}行",
                }
                for i in range(8)
            ],
            {"text": "注：高温工况不使用本表。", "section": "设备A"},
        ],
    }


class AgentContextTest(unittest.TestCase):
    def test_discovery_receives_scope_and_headers_without_rechecking_context(self):
        checked, later_context = [], []

        def handler(messages, title, tools, force):
            if title == "CheckPlan":
                return scripted.empty_plan()
            context = scripted.input_context(messages)
            targets = {s["id"] for s in context["sources"]}
            self.assertTrue(
                targets.isdisjoint(s["id"] for s in context["context_sources"])
            )
            checked.extend(targets)
            if any("设备A-7" in s["text"] for s in context["sources"]):
                later_context.extend(context["sources"] + context["context_sources"])
            return scripted.discovery(context)

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            documents=[table_document()],
            code="typo",
            options=AgentOptions(chunk_chars=1000),
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(checked), len(set(checked)))
        self.assertEqual(set(checked), set(agent.index.by_id))
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        context_text = "\n".join(s["text"] for s in later_context)
        self.assertIn("低温工况", context_text)
        self.assertIn("电压 / mV", context_text)
        self.assertIn("高温工况", context_text)

    def test_observation_can_quote_context_but_coverage_counts_only_targets(self):
        def handler(messages, title, tools, force):
            context = scripted.input_context(messages)
            header = next(
                s for s in context["context_sources"] if "电压 / mV" in s["text"]
            )
            return {
                **scripted.discovery(context),
                "observations": [
                    {
                        "requirement_id": "unit",
                        "status": "covered",
                        "rationale": "表头注明单位",
                        "evidence": [{"source_id": header["id"], "quote": "电压 / mV"}],
                    }
                ],
            }

        agent = scripted.EvidenceAgentTest().make_agent(
            handler, documents=[table_document()], code="completeness"
        )
        target = agent.index.blocks[-2]
        requirement = {
            "id": "unit",
            "text": "注明单位",
            "applicability": "参数表",
            "origin": "prompt",
            "source_id": "",
            "quote": "注明单位",
        }
        agent.state["requirements"] = [requirement]
        agent.state["discovery_mode"] = "chunked"
        agent._cached_jobs["discover"] = [([target], [requirement])]
        agent._discover()
        self.assertEqual(agent.state["observations"][0]["status"], "covered")
        self.assertEqual(agent.state["checked_ids"], [target.id])
        self.assertEqual(agent.state["requirement_coverage"]["unit"], [target.id])
        self.assertFalse(
            agent.index.coverage(agent.state["checked_ids"])["all_checked"]
        )

    def test_context_identifiers_cannot_be_reported_as_checked_sources(self):
        def handler(messages, title, tools, force):
            if title == "CheckPlan":
                return scripted.empty_plan()
            context = scripted.input_context(messages)
            output = scripted.discovery(context)
            output["covered_source_ids"] += [
                s["id"] for s in context["context_sources"]
            ]
            return output

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            documents=[table_document()],
            code="typo",
            options=AgentOptions(chunk_chars=1000),
        )
        result = agent.run()
        self.assertTrue(result["error"])
        self.assertFalse(result["metadata"]["coverage"]["all_checked"])

    def test_review_retains_distant_table_headers_and_scope(self):
        agent = scripted.EvidenceAgentTest().make_agent(
            lambda *args: None, documents=[table_document()]
        )
        target = agent.index.blocks[-2]
        batch = [
            {
                **scripted.candidate([{"source_id": target.id, "quote": "220000"}]),
                "id": "c1",
            }
        ]
        context = agent._review_context(batch)
        self.assertEqual(context["sources"][0]["id"], target.id)
        text = "\n".join(s["text"] for s in context["sources"])
        self.assertIn("电压 / mV", text)
        self.assertIn("低温工况", text)
        self.assertIn("高温工况", text)

    def test_context_budget_accounts_for_escaped_request_and_signals_omissions(self):
        document = table_document()
        document["blocks"][1]["text"] = '"\\\n' * 900
        agent = scripted.EvidenceAgentTest().make_agent(
            lambda *args: None, documents=[document]
        )
        target = agent.index.blocks[-2]
        job = {"sources": [target.as_dict()], "requirements": []}
        agent.max_input = 15000
        instruction, context = agent._discovery_request(job, whole_document=False)
        self.assertTrue(context["context_truncated"])
        self.assertLessEqual(
            agent._input_size(Discovery, instruction, context), agent.max_input
        )
        self.assertEqual(context["sources"], job["sources"])
        self.assertEqual(context["expected_source_ids"], [target.id])

    def test_review_context_keeps_direct_evidence_within_full_request_budget(self):
        document = table_document()
        document["blocks"][1]["text"] = '"\\\n' * 900
        agent = scripted.EvidenceAgentTest().make_agent(
            lambda *args: None, documents=[document]
        )
        agent.max_input = 15000
        target = agent.index.blocks[-2]
        batch = [
            {
                **scripted.candidate([{"source_id": target.id, "quote": "220000"}]),
                "id": "c1",
            }
        ]
        context = agent._review_context(batch)
        self.assertTrue(context["context_truncated"])
        self.assertEqual(context["sources"][0]["id"], target.id)
        self.assertLessEqual(
            agent._input_size(Review, REVIEW_INSTRUCTIONS, context), agent.max_input
        )

    def test_recovery_of_oversized_atomic_markup_always_reduces_source_size(self):
        agent = scripted.EvidenceAgentTest().make_agent(lambda *args: None)
        text = '<tr data-value="' + "甲" * 2000 + '"></tr>'
        job = {"sources": [{"kind": "table", "text": text}], "requirements": []}
        children = agent._split_discovery_job(job)
        self.assertEqual(len(children), 2)
        self.assertTrue(
            all(0 < len(child["sources"][0]["text"]) < len(text) for child in children)
        )


if __name__ == "__main__":
    unittest.main()
