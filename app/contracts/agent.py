"""文档检查各阶段的数据契约与执行预算。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

WORKFLOW_VERSION = "evidence-review-v1"
NonEmpty = Annotated[str, Field(min_length=1, max_length=12000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Standard(Contract):
    name: NonEmpty
    version: NonEmpty
    text: NonEmpty


class AgentOptions(Contract):
    enabled: bool = True
    strategy: Literal[
        "auto",
        "local",
        "consistency",
        "completeness",
        "compliance",
        "cross_document",
        "cross_language",
    ] = "auto"
    protocol: Literal["auto", "native", "structured"] = "auto"
    chunk_chars: int = Field(default=10000, ge=1000, le=50000)
    max_calls: int = Field(default=96, ge=4, le=1000)
    max_tokens: int = Field(default=500000, ge=10000, le=10000000)
    max_seconds: int = Field(default=3600, ge=30, le=14400)
    max_output_tokens: int = Field(default=8192, ge=512, le=32768)
    max_review_rounds: int = Field(default=3, ge=1, le=6)
    review_batch_size: int = Field(default=4, ge=1, le=12)
    max_candidates: int = Field(default=200, ge=1, le=1000)
    standards: list[Standard] = Field(default_factory=list, max_length=12)
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", max_length=12)


class EvidenceRef(Contract):
    source_id: NonEmpty
    quote: NonEmpty


class Requirement(Contract):
    id: NonEmpty
    text: NonEmpty
    applicability: str
    origin: Literal["prompt", "standard"]
    source_id: str
    quote: NonEmpty


class CheckPlan(Contract):
    document_type: str
    audience: str
    scope: str
    requirements: list[Requirement] = Field(max_length=80)


class Fact(Contract):
    subject: NonEmpty
    attribute: NonEmpty
    value: NonEmpty
    unit: str
    conditions: str
    version: str
    reference: EvidenceRef


class Candidate(Contract):
    category: NonEmpty
    description: NonEmpty
    impact: str
    suggestion: str
    severity: Literal["critical", "high", "medium", "low"]
    kind: Literal["issue", "suggestion"]
    absence_claim: bool
    applicability: str
    requirement_ids: list[str] = Field(max_length=80)
    evidence: list[EvidenceRef] = Field(max_length=20)


class RequirementObservation(Contract):
    requirement_id: NonEmpty
    status: Literal["covered", "not_found", "not_applicable", "uncertain"]
    rationale: str
    evidence: list[EvidenceRef] = Field(max_length=20)


class Discovery(Contract):
    covered_source_ids: list[str]
    facts: list[Fact] = Field(max_length=200)
    candidates: list[Candidate] = Field(max_length=100)
    observations: list[RequirementObservation] = Field(max_length=80)


class Comparison(Contract):
    candidates: list[Candidate] = Field(max_length=100)


class Decision(Contract):
    candidate_id: NonEmpty
    outcome: Literal["confirmed", "explained", "insufficient"]
    applicable: Literal["yes", "no", "unknown"]
    rationale: NonEmpty
    description: NonEmpty
    impact: str
    suggestion: str
    evidence: list[EvidenceRef] = Field(max_length=20)
    counterevidence: list[EvidenceRef] = Field(max_length=20)


class ToolAction(Contract):
    name: Literal[
        "search_document",
        "read_context",
        "read_table",
        "compare_numbers",
        "coverage_status",
    ]
    arguments: str


class Review(Contract):
    actions: list[ToolAction] = Field(max_length=6)
    decisions: list[Decision] = Field(max_length=12)


class ReportItem(Contract):
    id: NonEmpty
    status: Literal["issue", "suggestion"]
    severity: Literal["critical", "high", "medium", "low"]
    confidence: Literal["high", "medium", "low"]
    category: str
    location: str
    excerpt: str
    description: NonEmpty
    impact: str
    suggestion: str
    evidence_refs: list[dict]


class CheckedReport(Contract):
    summary: NonEmpty
    items: list[ReportItem]
    pending_items: list[dict]
    withdrawn_items: list[dict]


def strict_schema(contract: type[Contract]) -> dict:
    """所有对象字段显式提供，允许服务端执行严格结构化输出。"""
    schema = contract.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema
