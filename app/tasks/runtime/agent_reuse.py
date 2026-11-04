"""在同一任务中复用相同输入的规划与已验证判断，发现过程逐轮执行。"""

import threading
from copy import deepcopy

ROUND_FOCUSES = (
    "完整核查全部业务要求，建立问题清单。",
    "沿读者的操作路径核查主体、动作、前提、分支、结果与验证方法。",
    "重点核查数值、单位、范围、边界条件以及正文与表格的对应关系。",
    "重点对照跨章节的同一对象、术语、引用、配置、版本和适用条件。",
    "从例外情况和反向条件补查容易遗漏的问题，同时核实已有解释。",
)


def round_focus(number):
    return ROUND_FOCUSES[min(max(number - 1, 0), len(ROUND_FOCUSES) - 1)]


class SharedAgentWork:
    def __init__(self):
        self.lock = threading.Lock()
        self.plans = {}
        self.decisions = {}

    def get_plan(self, signature):
        with self.lock:
            return deepcopy(self.plans.get(signature))

    def put_plan(self, signature, state):
        with self.lock:
            self.plans[signature] = deepcopy(
                {
                    key: state[key]
                    for key in ("plans", "requirements", "global_checks", "plan_cursor")
                }
            )

    def get_decision(self, signature, candidate_id):
        with self.lock:
            return deepcopy(self.decisions.get((signature, candidate_id)))

    def discard_decision(self, signature, candidate_id):
        with self.lock:
            self.decisions.pop((signature, candidate_id), None)

    def put_decision(self, signature, candidate, decision, source_ids, round_number):
        if decision["outcome"] not in {"confirmed", "explained"}:
            return
        # 全文缺失依赖本轮逐项覆盖观察，逐轮重新验证。
        if (
            candidate["absence_claim"]
            and candidate.get("absence_scope", "document") == "document"
        ):
            return
        with self.lock:
            self.decisions[(signature, candidate["id"])] = deepcopy(
                {
                    "decision": decision,
                    "source_ids": list(source_ids),
                    "round": round_number,
                }
            )
