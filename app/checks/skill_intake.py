"""根据原文与确认理由整理专项草稿和案例。"""

import json

from app.checks.skills import (
    CASE_FIELDS,
    MAX_EXAMPLES_PER_KIND,
    create_skill,
    get_skill,
    get_skill_case,
    save_case,
    save_skill,
    select_skill_examples,
    selected_examples,
    skill_cases,
    skill_definition,
)


def _text(data, key):
    return str(data.get(key) or "").strip()


def quick_case_values(data, *, positive_only=False):
    evidence = str(data.get("evidence") or "").strip()
    rationale = str(data.get("rationale") or "").strip()
    corrected = str(data.get("corrected_text") or "").strip()
    if not evidence:
        raise ValueError("请粘贴有问题的原文和必要上下文。")
    if not rationale:
        raise ValueError("请用自己的话说明为什么有问题。")
    if len(rationale) > 3000 or len(corrected) > 3000:
        raise ValueError("问题说明和正确写法各最多填写 3000 字。")
    kind = "positive" if positive_only else data.get("case_kind", "positive")
    expected = (
        f"应识别并报告下述问题：\n{rationale}"
        if kind == "positive"
        else f"应放行这段原文，理由：\n{rationale}"
    )
    if corrected and kind == "positive":
        expected += f"\n确认后的正确写法：\n{corrected}"
    return {
        "case_kind": kind,
        "reviewed": "",
        "title": _text(data, "title") or rationale.splitlines()[0][:60],
        "reference": _text(data, "reference")
        or "用户提供的原文与说明，适用条件以原文为准",
        "location": _text(data, "location") or "提供的原文片段",
        "evidence": evidence,
        "rationale": rationale,
        "expected_result": _text(data, "expected_result") or expected,
        "corrected_text": corrected,
        "document_text": _text(data, "document_text") or evidence,
    }


def _draft_rule(case):
    return {
        "target": f"发现与以下已知问题相同的错误模式：\n{case['rationale']}",
        "applicability": (
            "核对对象、产品版本、运行模式和适用条件。条件明确且与已知问题相符时执行判断。\n"
            f"案例场景：{str(case['reference'])[:2000]}"
        ),
        "steps": (
            "1. 找到与已知问题相关的表述，读取必要上下文。\n"
            "2. 核对对象、条件、单位和相关引用，按检查目标判断。\n"
            "3. 结合已确认的真实问题和正确反例核对适用边界。\n"
            "4. 证据充分时报告具体问题并引用原文。"
        ),
        "evidence_requirements": (
            "证据来自待检查文档，包含原文和可定位的位置。一致性问题引用相关的两处表述。"
            "缺失类问题核对全文及引用内容。业务判断使用已提供的权威依据，证据充分时报告问题。"
        ),
        "exclusions": (
            "对象、版本或运行条件不同，单位换算后等价，以及已确认应放行的情形。"
            "必要证据或权威依据不足时保留待核对结论。"
        ),
        "suggestion_principles": (
            "依据原文和已确认的案例给出修改方向。正确写法以用户确认的内容为准；"
            "需要业务核实时说明待核对的内容。"
        ),
    }


def _save_intake_case(db, item, values, data, source):
    case_id = save_case(db, item, values, source=source)
    case = get_skill_case(db, item["id"], case_id)
    case["data"]["intake"] = "quick"
    case["data"]["context_scope"] = (
        "provided_text" if str(data.get("document_text") or "").strip() else "excerpt"
    )
    db.execute(
        "UPDATE skill_cases SET case_json=? WHERE id=?",
        (json.dumps(case["data"], ensure_ascii=False), case_id),
    )
    return case_id


def create_quick_skill(db, data, source=None):
    """一次保存规则草稿和待确认的真实问题，正式启用由用户确认。"""
    values = quick_case_values(data, positive_only=True)
    name = _text(data, "name") or values["title"][:60]
    skill_id = create_skill(db, name)
    save_skill(
        db,
        get_skill(db, skill_id),
        {"name": name, "description": values["rationale"][:200], **_draft_rule(values)},
    )
    case_id = _save_intake_case(db, get_skill(db, skill_id), values, data, source)
    return skill_id, case_id


def add_quick_case(db, item, data, source=None):
    values = quick_case_values(data)
    if not skill_definition(item):
        if values["case_kind"] != "positive":
            raise ValueError("请先用一条真实问题建立专项，再补充应当放行的反例。")
        save_skill(
            db,
            item,
            {
                "name": item["name"],
                "description": item["description"],
                **_draft_rule(values),
            },
        )
    return _save_intake_case(db, get_skill(db, item["id"]), values, data, source)


def confirm_intake_case(db, item, case_id, *, enable=False):
    """人工确认案例，有示例空位时引用该案例，并按用户选择启用专项。"""
    case = get_skill_case(db, item["id"], case_id)
    if enable and case["data"].get("kind") != "positive":
        raise ValueError("启用专项需要先确认一条真实问题。")
    if not case["data"].get("reviewed"):
        values = {key: case["data"].get(key, "") for key, _ in CASE_FIELDS}
        values.update(case_kind=case["data"]["kind"], reviewed="on")
        save_case(db, item, values, case_id)
        updated = get_skill_case(db, item["id"], case_id)
        for key in ("intake", "context_scope"):
            if key in case["data"]:
                updated["data"][key] = case["data"][key]
        db.execute(
            "UPDATE skill_cases SET case_json=? WHERE id=?",
            (json.dumps(updated["data"], ensure_ascii=False), case_id),
        )
    examples = selected_examples(skill_cases(db, item["id"]))
    selected_ids = [example["id"] for example in examples]
    if (
        case_id not in selected_ids
        and sum(example["data"]["kind"] == case["data"]["kind"] for example in examples)
        < MAX_EXAMPLES_PER_KIND
    ):
        selected_ids.append(case_id)
        select_skill_examples(db, get_skill(db, item["id"]), selected_ids)
    if enable:
        current = get_skill(db, item["id"])
        save_skill(
            db,
            current,
            {
                "name": current["name"],
                "description": current["description"],
                "enabled": "on",
                **skill_definition(current),
            },
        )
    return case_id in selected_ids
