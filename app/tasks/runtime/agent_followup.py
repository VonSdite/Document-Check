"""单轮审查中的自主补查：模型选择行动，程序校验证据、覆盖与停止条件。"""

import hashlib
import json
import time
from copy import deepcopy

from app.checks.evidence_tools import TOOLS, check_state_entries, tool_definitions
from app.contracts.agent import Followup
from app.models.client import LLMError

FOLLOWUP_INSTRUCTIONS = (
    "你负责本轮检查的自主补查。首遍扫描和候选复核已经完成，按全部业务规则评估遗漏与未解决疑点，"
    "根据当前结果自行选择下一步，不机械重复整篇检查。"
    "可通过 actions 检索、读取原文、核对数值或分页读取检查状态；工具由程序执行，结果在下一步提供。"
    "发现新问题时用 candidates 提交有直接原文线索的候选，由独立会话复核。"
    "候选的引用必须来自实际提供的 sources，不能把目录、概括或候选描述当作原文。"
    "需要改判已有候选时，rechecks 指定 candidate_id 和新找到的直接原文；确认、待核实或撤回判断均可重查。"
    "检查跨章节关系时主动读取相关来源，核实对象、条件、版本和单位。"
    "缺项按 excerpt、section、document 指明范围，其他章节的覆盖不能替代当前步骤或章节的要求。"
    "来源目录、要求和候选列表可能只提供部分，可用 read_check_state 按 offset 分页读取；"
    "搜索无结果不能证明全文缺失，工具读取也不等于完成业务检查。"
    "每步简要说明行动目的或结束依据，不输出推理链。利用已完成的工具结果，避免重复相同行动。"
    "必要覆盖完成、候选均已复核，且没有值得继续执行的具体补查时返回 status=complete，"
    "actions、candidates、rechecks 均为空；证据不足的判断可以保留待核实并说明限制。"
    "继续行动返回 status=continue；预算有限，优先处理可能影响正确操作的疑点，并为独立复核和结束留出预算。"
)


class FollowupLoop:
    def __init__(self, agent):
        self.agent = agent
        self.state = agent.state
        self.loop = self.state.setdefault(
            "followup",
            {
                "status": "running",
                "steps": [],
                "seen_sources": [],
                "tool_cache": {},
                "tool_results": [],
                "feedback": [],
                "pending_actions": [],
                "review_ids": [],
                "no_progress": 0,
            },
        )

    def _stop(self, reason):
        self.loop.update(status="incomplete", stop_reason=reason)
        raise LLMError(f"自主补查未完成：{reason}；已保留检查结果。", retryable=False)

    def _context(self):
        agent = self.agent
        # 报告校验同时更新视觉、表格证据限制，供补查判断使用。
        agent.build_report()
        findings = check_state_entries("candidates", agent.index, self.state)
        findings.sort(
            key=lambda item: item["validation"].get("final_outcome") != "insufficient"
        )
        results = deepcopy(self.loop["tool_results"])
        priority_ids = list(
            dict.fromkeys(
                [s["id"] for result in results for s in result.get("sources", [])]
                + self.loop["seen_sources"][-12:]
                + [r["source_id"] for c in findings[:8] for r in c["evidence"]]
                + [b.id for b in agent.index.blocks[:12]]
            )
        )
        context = {
            "coverage": agent.index.coverage(self.state["checked_ids"]),
            "counts": {
                "sources": len(agent.index.blocks),
                "requirements": len(self.state["requirements"]),
                "candidates": len(findings),
            },
            "source_catalog": check_state_entries("sources", agent.index, self.state)[
                :8
            ],
            "requirements": self.state["requirements"][:8],
            "findings": findings[:8],
            "sources": [
                agent.index.by_id[s].as_dict()
                for s in priority_ids[:12]
                if s in agent.index.by_id
            ],
            "context_truncated": any(
                len(values) > limit
                for values, limit in (
                    (priority_ids, 12),
                    (findings, 8),
                    (self.state["requirements"], 8),
                    (agent.index.blocks, 8),
                )
            ),
            "tool_results": [
                {k: v for k, v in result.items() if k != "sources"}
                for result in results
            ],
            "recent_steps": self.loop["steps"][-3:],
            "feedback": self.loop["feedback"][-6:],
            "remaining_steps": agent.options.max_followup_steps
            - len(self.loop["steps"]),
            "remaining_calls": (
                max(0, agent.options.max_calls - len(self.state["requests"]))
                if agent.options.max_calls is not None
                else None
            ),
            "remaining_seconds": (
                max(
                    0,
                    agent.options.max_seconds
                    - agent.base_elapsed
                    - (time.monotonic() - agent.started),
                )
                if agent.options.max_seconds is not None
                else None
            ),
            "tools": tool_definitions(),
        }
        # 保留完整的原文块；目录与状态可按页继续读取。
        for field in (
            "recent_steps",
            "findings",
            "requirements",
            "source_catalog",
            "tool_results",
            "sources",
        ):
            while (
                context[field]
                and agent._input_size(Followup, FOLLOWUP_INSTRUCTIONS, context)
                > agent.max_input
            ):
                context[field].pop()
                context["context_truncated"] = True
        self.loop["seen_sources"] = list(
            dict.fromkeys(
                self.loop["seen_sources"] + [s["id"] for s in context["sources"]]
            )
        )
        return context

    def step(self):
        self.loop["status"] = "running"
        if len(self.loop["steps"]) >= self.agent.options.max_followup_steps:
            self._stop("已达到补查决策次数上限")
        if self.loop["no_progress"] >= 2:
            self._stop("连续两步没有新的取证行动或可复核内容")
        response = self.agent._ask(Followup, FOLLOWUP_INSTRUCTIONS, self._context())
        data = response.model_dump(mode="json")
        self.loop["steps"].append(
            {
                "step": len(self.loop["steps"]) + 1,
                "status": data["status"],
                "rationale": data["rationale"],
                "actions": data["actions"],
                "new_candidate_ids": [],
                "recheck_ids": [],
            }
        )
        if data["status"] == "complete":
            coverage = self.agent.index.coverage(self.state["checked_ids"])
            if not coverage["all_checked"] or not coverage["text_complete"]:
                self._stop("必要原文尚未完整检查或解析不完整")
            if self.agent._tracks_observations() and any(
                not self.agent.index.coverage(
                    self.state["requirement_coverage"].get(r["id"], [])
                )["all_checked"]
                for r in self.state["requirements"]
            ):
                self._stop("业务要求尚未覆盖全部必要来源")
            decided = {d["candidate_id"] for d in self.state["decisions"]}
            if any(c["id"] not in decided for c in self.state["candidates"]):
                self._stop("仍有候选尚未完成独立复核")
            self.loop.update(status="completed", stop_reason=data["rationale"])
            self.state["next"] = "finalize"
            return
        self.loop["review_ids"] = self._candidates(data["candidates"])
        self.loop["review_ids"].extend(self._rechecks(data["rechecks"]))
        self.loop["pending_actions"] = data["actions"]
        self.loop["tool_results"] = []
        self.loop["no_progress"] = (
            0 if self.loop["review_ids"] else self.loop["no_progress"] + 1
        )
        if data["actions"]:
            self.state["next"] = "followup_tools"
        else:
            self._route_reviews()

    def _references(self, references):
        resolved = [
            self.agent.index.resolve_reference(
                ref, allowed=set(self.loop["seen_sources"])
            )
            for ref in references
        ]
        if any(ref is None for ref in resolved):
            return None
        return resolved

    def _candidates(self, candidates):
        added = []
        requirements = {r["id"] for r in self.state["requirements"]}
        for candidate in candidates:
            references = self._references(candidate["evidence"])
            if (
                references is None
                or not set(candidate["requirement_ids"]) <= requirements
                or (
                    not references
                    and not (
                        candidate["absence_claim"] and candidate["requirement_ids"]
                    )
                )
            ):
                self.loop["feedback"].append(
                    "新候选的要求或引用尚未核实，请先读取准确原文再提交。"
                )
                continue
            before = len(self.state["candidates"])
            self.agent._add_candidate({**candidate, "evidence": references})
            if len(self.state["candidates"]) > before:
                added.append(self.state["candidates"][-1]["id"])
            else:
                self.loop["feedback"].append(
                    "已保留相同候选；改变已有判断请提供新证据并使用 rechecks。"
                )
        self.loop["steps"][-1]["new_candidate_ids"] = added
        return added

    def _rechecks(self, rechecks):
        candidates = {c["id"]: c for c in self.state["candidates"]}
        decisions = {d["candidate_id"]: d for d in self.state["decisions"]}
        extra = self.state.setdefault("supplemental_evidence", {})
        scheduled = []
        for recheck in rechecks:
            cid = recheck["candidate_id"]
            refs = self._references(recheck["evidence"])
            if cid not in decisions or refs is None:
                self.loop["feedback"].append(
                    "重查需要已复核的候选标识和实际读取过的新证据。"
                )
                continue
            known = {
                (r["source_id"], r["quote"])
                for r in candidates[cid]["evidence"]
                + decisions[cid]["evidence"]
                + decisions[cid]["counterevidence"]
                + extra.get(cid, [])
            }
            fresh = [r for r in refs if (r["source_id"], r["quote"]) not in known]
            if not fresh:
                self.loop["feedback"].append(f"候选 {cid} 没有新证据，保留已有判断。")
                continue
            extra.setdefault(cid, []).extend(fresh)
            self.agent.shared_work.discard_decision(self.agent.work_signature, cid)
            scheduled.append(cid)
        self.loop["steps"][-1]["recheck_ids"] = scheduled
        return scheduled

    def tools(self):
        while self.loop["pending_actions"]:
            self.agent.ensure_active()
            action = self.loop["pending_actions"][0]
            try:
                args = TOOLS[action["name"]][0].model_validate(
                    json.loads(action["arguments"])
                )
                version = None
                if action["name"] == "read_check_state":
                    version = check_state_entries(
                        args.kind, self.agent.index, self.state
                    )
                elif action["name"] == "coverage_status":
                    version = [
                        self.state["checked_ids"],
                        self.state["requirement_coverage"],
                        self.state["observations"],
                    ]
                key = hashlib.sha256(
                    json.dumps(
                        [action["name"], args.model_dump(mode="json"), version],
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                cached = self.loop["tool_cache"].get(key)
                if cached is None:
                    result = self.agent._tool_result(action)
                    if not result.get("error"):
                        self.loop["no_progress"] = 0
                        self.loop["tool_cache"][key] = deepcopy(result)
                else:
                    result = deepcopy(cached)
                    result["reused"] = True
                    self.loop["feedback"].append(
                        "相同工具参数已执行，已复用结果；请选择新的具体行动或结束。"
                    )
            except ValueError as exc:
                result = {"error": str(exc)[:1000], "sources": []}
            self.loop["tool_results"].append({"tool": action["name"], **result})
            self.loop["pending_actions"].pop(0)
            self.agent.persist()
        self._route_reviews()

    def _route_reviews(self):
        ids = set(self.loop["review_ids"])
        if ids:
            prior = [d for d in self.state["decisions"] if d["candidate_id"] in ids]
            if prior:
                self.loop["steps"][-1]["prior_decisions"] = deepcopy(prior)
                self.state["decisions"] = [
                    d for d in self.state["decisions"] if d["candidate_id"] not in ids
                ]
                for cid in ids:
                    self.state.get("decision_audit", {}).pop(cid, None)
            settled = [c for c in self.state["candidates"] if c["id"] not in ids]
            pending = [c for c in self.state["candidates"] if c["id"] in ids]
            self.state.update(
                candidates=settled + pending,
                review_cursor=len(settled),
                review_batch=[],
                review_round=0,
                review_prepared=True,
                messages=[],
                pending_actions=[],
                next="review",
            )
            self.state.get("batch_outputs", {}).pop("review", None)
            self.agent._clear_reference_retry()
        else:
            self.state["next"] = "followup"
        self.loop["review_ids"] = []
