"""只读取当前任务来源的审查工具。"""

import re
from decimal import Decimal

from pydantic import Field

from app.contracts.agent import Contract, EvidenceRef, NonEmpty, strict_schema


class SearchArguments(Contract):
    query: NonEmpty
    limit: int = Field(default=8, ge=1, le=20)


class ContextArguments(Contract):
    source_id: NonEmpty
    radius: int = Field(default=1, ge=0, le=3)


class TableArguments(Contract):
    source_id: NonEmpty


class NumberArguments(Contract):
    left: EvidenceRef
    right: EvidenceRef


class CoverageArguments(Contract):
    requirement_id: str


TOOLS = {
    "search_document": (
        SearchArguments,
        "检索当前任务文档及已配置的版本化标准；无命中不能证明内容缺失。",
    ),
    "read_context": (
        ContextArguments,
        "读取来源及同一文件的相邻内容，核实定义、型号、版本和例外。",
    ),
    "read_table": (TableArguments, "读取来源所在表格，保留表头、行列和合并信息。"),
    "compare_numbers": (
        NumberArguments,
        "核对两条原文引用中的数字和单位；计算结果不能证明对象和条件一致。",
    ),
    "coverage_status": (
        CoverageArguments,
        "查看解析和检查覆盖、逐项要求的观察记录，核实缺失判断的适用性。",
    ),
}


def tool_definitions():
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": strict_schema(contract),
                "strict": True,
            },
        }
        for name, (contract, description) in TOOLS.items()
    ]


def execute_tool(name: str, arguments: dict, index, state: dict) -> dict:
    if name not in TOOLS:
        raise ValueError("工具名称不在允许范围内")
    args = TOOLS[name][0].model_validate(arguments)
    if name == "search_document":
        return {
            "sources": index.search(args.query, args.limit),
            "absence_proven": False,
        }
    if name == "read_context":
        return {"sources": index.context(args.source_id, args.radius)}
    if name == "read_table":
        source = index.by_id.get(args.source_id)
        if source is None or source.kind != "table":
            raise ValueError("来源不是可读取的结构化表格")
        table = re.search(r"表格\s*([^ /；>]+)", source.location)
        sources = [source.as_dict()]
        if table:
            sources = [
                b.as_dict()
                for b in index.blocks
                if b.filename == source.filename
                and b.role == source.role
                and b.kind == "table"
                and re.search(r"表格\s*([^ /；>]+)", b.location)
                and re.search(r"表格\s*([^ /；>]+)", b.location).group(1)
                == table.group(1)
            ]
        return {"sources": sources[:20], "truncated": len(sources) > 20}
    if name == "coverage_status":
        observations = state.get("observations", [])
        return {
            **index.coverage(state.get("checked_ids", [])),
            "observations": [
                o
                for o in observations
                if not args.requirement_id or o["requirement_id"] == args.requirement_id
            ],
            "sources": [],
        }
    left, right = args.left.model_dump(), args.right.model_dump()
    if not index.verify(left) or not index.verify(right):
        raise ValueError("数值引用无法在原文中核实")
    left_value, left_unit = _number(left["quote"])
    right_value, right_unit = _number(right["quote"])
    compatible = left_unit == right_unit
    return {
        "left": str(left_value),
        "right": str(right_value),
        "unit": left_unit,
        "compatible_units": compatible,
        "equal": left_value == right_value if compatible else None,
        "difference": str(left_value - right_value) if compatible else None,
        "scope_verified": False,
        "sources": [index.by_id[r["source_id"]].as_dict() for r in (left, right)],
    }


def _number(quote: str):
    matches = list(
        re.finditer(r"(?<![\w.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?", quote)
    )
    if len(matches) != 1:
        raise ValueError("数值引用需要恰好包含一个数字")
    match = matches[0]
    value = Decimal(match.group())
    unit = quote[match.end() :].strip().lower()
    units = {
        "v": ("V", 1),
        "mv": ("V", "0.001"),
        "kv": ("V", 1000),
        "a": ("A", 1),
        "ma": ("A", "0.001"),
        "w": ("W", 1),
        "kw": ("W", 1000),
        "s": ("s", 1),
        "秒": ("s", 1),
        "ms": ("s", "0.001"),
        "min": ("s", 60),
        "分钟": ("s", 60),
        "h": ("s", 3600),
        "%": ("ratio", "0.01"),
        "": ("", 1),
    }
    dimension, factor = units.get(unit, (unit, 1))
    return value * Decimal(str(factor)), dimension
