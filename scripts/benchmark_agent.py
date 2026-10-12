"""用固定模型延迟比较证据审查的串行和并行耗时。"""

import argparse
import json
import threading
import time

from app.checks.agent_policy import DEFAULT_AGENT_PARALLELISM
from app.contracts.agent import AgentOptions
from app.documents.index import DocumentIndex
from app.models.agent import AgentTurn
from app.persistence.checkpoints import MemoryCheckpointStore
from app.tasks.runtime.evidence_agent import EvidenceAgent


def measure(blocks, delay, parallelism):
    lock = threading.Lock()
    active = peak = 0

    class DelayedModel:
        def __init__(self, connection, *, before_request, after_request, **kwargs):
            self.before_request = before_request
            self.after_request = after_request

        def turn(self, messages, *, schema):
            nonlocal active, peak
            payload = {"messages": messages}
            self.before_request(payload)
            started = time.monotonic()
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(delay)
                if schema["title"] == "CheckPlan":
                    answer = {
                        "document_type": "技术说明",
                        "audience": "技术人员",
                        "scope": "参数",
                        "global_checks": [],
                        "requirements": [],
                    }
                elif schema["title"] == "Discovery":
                    context = json.loads(messages[-1]["content"].split("输入：", 1)[1])
                    answer = {
                        "covered_source_ids": context["expected_source_ids"],
                        "facts": [],
                        "candidates": [],
                        "observations": [],
                    }
                else:
                    raise AssertionError("本压测只包含规划和候选发现")
                turn = AgentTurn(
                    json.dumps(answer, ensure_ascii=False),
                    usage={"prompt_tokens": 100, "completion_tokens": 20},
                    finish_reason="stop",
                )
                self.after_request(payload, turn, time.monotonic() - started, None)
                return turn
            finally:
                with lock:
                    active -= 1

    index = DocumentIndex(
        [
            {
                "filename": "synthetic.txt",
                "text": "\n".join(
                    f"来源{n}：" + "设备参数正常。" * 85 for n in range(blocks)
                ),
                "text_complete": True,
            }
        ]
    )
    agent = EvidenceAgent(
        index=index,
        item={"code": "custom", "name": "参数检查", "prompt": "核实参数。"},
        task_type="document_check",
        connection={"api_base": "http://benchmark.invalid", "model_name": "delayed"},
        options=AgentOptions(protocol="structured", chunk_chars=1000),
        store=MemoryCheckpointStore(),
        ensure_active=lambda: None,
        model_factory=DelayedModel,
        parallelism=parallelism,
    )
    started = time.monotonic()
    result = agent.run()
    elapsed = time.monotonic() - started
    if result["error"] or not result["metadata"]["coverage"]["all_checked"]:
        raise AssertionError("压测检查必须成功并完整覆盖")
    metadata = result["metadata"]
    stats = {
        "parallelism": parallelism,
        "peak_requests": peak,
        "elapsed_seconds": round(elapsed, 3),
        "model_calls": metadata["model_calls"],
        "input_tokens": metadata["known_input_tokens"],
        "output_tokens": metadata["known_output_tokens"],
        "usage_complete": metadata["usage_complete"],
    }
    return result, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=24)
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()
    if args.blocks < 1 or args.delay < 0:
        parser.error("blocks 必须为正数，delay 必须为非负数")
    serial, serial_stats = measure(args.blocks, args.delay, 1)
    parallel, parallel_stats = measure(
        args.blocks, args.delay, DEFAULT_AGENT_PARALLELISM
    )
    equal_keys = (
        "coverage",
        "model_calls",
        "known_input_tokens",
        "known_output_tokens",
        "usage_complete",
    )
    if serial["report"] != parallel["report"] or any(
        serial["metadata"][key] != parallel["metadata"][key] for key in equal_keys
    ):
        raise AssertionError("串行与并行的报告、覆盖和用量应一致")
    print(
        json.dumps(
            {
                "blocks": args.blocks,
                "model_delay_seconds": args.delay,
                "serial": serial_stats,
                "parallel": parallel_stats,
                "speedup": round(
                    serial_stats["elapsed_seconds"] / parallel_stats["elapsed_seconds"],
                    2,
                ),
                "report_coverage_and_usage_equal": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
