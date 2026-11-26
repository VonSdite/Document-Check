"""按条目结论过滤无需处理及越出规范性、一致性范围的补充说明建议。"""

import re
import unicodedata

REVIEW_SCOPES = frozenset({"compliance", "consistency"})
_DESCRIPTION = ("description", "issue", "problem", "finding", "问题描述", "问题判断")
_SUGGESTION = ("suggestion", "recommendation", "fix", "修改建议", "建议")
_EXCERPT = ("excerpt", "quote", "original", "evidence", "原文摘录", "原文", "证据")
_QUOTED = re.compile(r'“[^”\n]*”|「[^」\n]*」|"[^"\n]*"|`[^`\n]*`')
_ERROR_NOUN = (
    r"(?:(?:标点|拼写|参数|单位|编号)(?:标注)?)?"
    r"(?:问题|错误|错别字|错字|异常|不一致|冲突)"
)
_NO_ACTION = re.compile(
    r"(?:无需|无须|不需|不需要)(?:进行)?(?:修改|改动|调整|处理|修正|更正|纠正)|"
    r"保持(?:原文|现状|不变|现有表述)|"
    r"(?:不构成|不属于|不是|并非)(?:实质|实际|明确)?(?:问题|错误|错别字|错字|矛盾|冲突)|"
    r"(?:未发现|未见|没有|并无|不存在|无)(?:明显|实质|实际|明确)?"
    rf"{_ERROR_NOUN}(?:(?:以及|或|和|及|、){_ERROR_NOUN})*"
    r"(?=$|[\s,;。.!！？?:：]|但|且|并且)|"
    r"\b(?:no\s+(?:change|changes|action|modification)\s+(?:is\s+|are\s+)?(?:required|needed)|"
    r"no\s+need\s+to\s+(?:modify|change))\b",
    re.I,
)
_CONDITIONAL = re.compile(
    r"如果|(?:^|[,;。\s])若|如(?:确认|核实|果)|除非|待(?:确认|核实)|"
    r"(?:确认|核实)后|在[^,;。\n]{0,20}(?:前提|情况下)|\b(?:if|unless|provided)\b",
    re.I,
)
_NEGATED_NO_ACTION = re.compile(r"(?:并非|不是|不能|不应|不可|不得)[^,;。\n]{0,12}$")
_CHANGE = re.compile(
    r"(?:修改|改为|改成|更正|修正|纠正|替换|补充|补写|补全|补上|补齐|增加|新增|"
    r"删除|去掉|统一|调整|澄清|核实|核对|确认|明确|细化|展开|完善)|"
    r"\b(?:change|correct|replace|add|remove|update|verify|clarify)\b",
    re.I,
)
_DEFECT = re.compile(
    r"错别字|错字|误写|错写|误标|漏字|多字|错引|错指|跳号|编号重复|重复编号|"
    r"(?:标注|拼写|计算|接线|极性|符号|公式|数值|参数|型号|名称|用词|语法|标点|"
    r"页码|术语|单位|编号|引用|图号|表号)[^,;。\n]{0,10}(?:错误|有误|写错)|"
    r"(?:缺少|缺失|漏写|漏标|未标注)[^,;。\n]{0,10}(?:单位|编号|闭合符号)|"
    r"(?:单位|编号|括号|引号)[^,;。\n]{0,10}(?:缺少|缺失|遗漏|错误|有误)|"
    r"(?:图|表|第)\s*[A-Za-z0-9.-]+[^,;。\n]{0,8}(?:不存在|缺失)|"
    r"直接(?:冲突|矛盾)|相互矛盾|不能同时成立|"
    r"(?:一处|前文|正文)[^,;。\n]{0,20}允许[^。\n]{0,40}(?:另一处|后文|表格)[^。\n]{0,15}禁止|"
    r"\b(?:typo|misspelled|wrong\s+(?:unit|reference))\b",
    re.I,
)
_CORRECTION = re.compile(r"(?:改|更正|修正|替换|统一)(?:写)?为|改成|应写作|应使用")
_PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|XXX)\b|待插入|此处插入|占位符", re.I)
_MISSING = r"(?:未(?:明确|详细|充分)?(?:说明|解释|交代|给出|提供|注明|界定|标明|区分|定义)|未明确|没有(?:说明|解释|给出|提供)|缺少|缺乏|缺失|不明确|不清晰|不清楚|不充分|不完整|不足)"
_CONTEXT = r"(?:工况|适用(?:条件|范围|场景|版本|对象)|工作状态|运行条件|测量条件|选择依据|取值依据|判断依据|条件边界|各范围|对应关系)"
_DETAIL = r"(?:具体(?:步骤|操作|方法)|详细(?:步骤|操作|说明)|操作(?:步骤|方法|细节)|如何(?:操作|配置|设置)|步骤细节|参数含义|术语解释|背景说明|验证方法|预期结果|异常处理|示例|必要说明|具体说明)"
_SUPPLEMENT_MISSING = re.compile(
    rf"{_MISSING}[^,;。\n]{{0,45}}(?:{_CONTEXT}|{_DETAIL})|"
    rf"(?:{_CONTEXT}|{_DETAIL})[^,;。\n]{{0,30}}{_MISSING}"
)
_COARSE_STEP = re.compile(
    r"(?:步骤|操作流程|操作过程|操作说明|配置说明)[^,;。\n]{0,18}"
    r"(?:笼统|简略|粗略|概括|过于简单|不够详细|不够具体|细节不足)|"
    r"(?:笼统|简略|粗略)[^,;。\n]{0,12}(?:步骤|操作)"
)
_REFERENCE_DOCUMENT = re.compile(r"手册|指南|引用[^,;。\n]{0,15}(?:资料|文档|标准)")
_REFERENCE_METADATA = r"(?:版本号?|版本信息|发布日期|获取(?:方式|路径|入口)|下载(?:地址|链接|路径)|超链接|URL|文件路径|获取入口)"
_REFERENCE_MISSING = re.compile(
    rf"{_MISSING}[^,;。\n]{{0,40}}{_REFERENCE_METADATA}|"
    rf"{_REFERENCE_METADATA}[^,;。\n]{{0,20}}{_MISSING}",
    re.I,
)
_SUPPLEMENT = re.compile(
    rf"(?:补充|补写|增加|完善|细化|展开|"
    rf"(?:建议|应当|应|需要|需|请)(?:进一步)?(?:明确|解释|说明))[^,;。\n]{{0,25}}"
    rf"(?:{_CONTEXT}|{_DETAIL}|步骤|说明|{_REFERENCE_METADATA})",
    re.I,
)
_INCOMPLETE_CATEGORY = re.compile(
    r"(?:内容|信息|说明|步骤)(?:不完整|完整性|缺失|不足)|完整性"
)
_COMPARISON = re.compile(r"不一致|不同|差异|分别为|冲突|矛盾")
_SAME_CONTEXT = re.compile(r"(?:同一|相同)(?:的)?(?:工况|条件|状态|阶段|测量点|端口)")
_UNCONFIRMED = re.compile(r"是否|未说明|未确认|尚未|不能确认|无法确认|待核实|需确认")


def _text(item: dict, fields: tuple[str, ...]) -> str:
    return unicodedata.normalize(
        "NFKC", "\n".join(str(item.get(field) or "") for field in fields)
    ).strip()


def review_filter_scope(check: dict) -> str:
    """优先使用快照模板标识，兼容固定编码和明确的历史检查项名称。"""
    template = str(check.get("template_code") or "")
    if template:
        return template if template in REVIEW_SCOPES else ""
    code = str(check.get("code") or "")
    if code in REVIEW_SCOPES:
        return code
    name = str(check.get("name") or "").strip()
    if re.search(r"(?:文档)?规范性检查$", name):
        return "compliance"
    if re.search(r"(?:全文|文档)一致性检查$", name):
        return "consistency"
    return ""


def is_no_action_finding(item: dict) -> bool:
    """只将明确且无附加修改要求的结论视为无需处理，摘录不参与判定。"""
    status = (
        str(item.get("status") or item.get("type") or item.get("状态") or "")
        .strip()
        .lower()
    )
    if status in {"non_issue", "nonissue", "not_issue", "非问题", "无需修改", "无问题"}:
        return True
    description = _QUOTED.sub("", _text(item, _DESCRIPTION))
    suggestion = _QUOTED.sub("", _text(item, _SUGGESTION))
    text = description + "\n" + suggestion
    matches = list(_NO_ACTION.finditer(text))
    if not matches or _DEFECT.search(_NO_ACTION.sub("", description)):
        return False
    if _CONDITIONAL.search(text) or any(
        _NEGATED_NO_ACTION.search(text[max(0, match.start() - 18) : match.start()])
        for match in matches
    ):
        return False
    # “无需修改数值，但需补单位”保留；“保持原文，无需修改”可过滤。
    remainder = _NO_ACTION.sub("", suggestion)
    remaining_description = _NO_ACTION.sub("", description)
    if _CHANGE.search(remainder) or re.search(
        rf"(?:但|仍|还|另|必须|需要|建议|请)[^,;。\n]{{0,12}}(?:{_CHANGE.pattern})",
        remaining_description,
        re.I,
    ):
        return False
    return True


def is_supplement_only_finding(item: dict) -> bool:
    """识别缺少说明或细节的主张，同时保留可独立成立的错误和冲突。"""
    description = _text(item, _DESCRIPTION)
    suggestion = _text(item, _SUGGESTION)
    category = _text(item, ("category", "issue_type", "problem_type", "问题类型"))
    claim = description + "\n" + suggestion
    if (
        _DEFECT.search(claim)
        or _CORRECTION.search(claim)
        or _PLACEHOLDER.search(description + "\n" + _text(item, _EXCERPT))
    ):
        return False
    if _COMPARISON.search(description) and any(
        not _UNCONFIRMED.search(description[max(0, match.start() - 16) : match.start()])
        for match in _SAME_CONTEXT.finditer(description)
    ):
        return False
    return bool(
        _SUPPLEMENT_MISSING.search(description)
        or _COARSE_STEP.search(description)
        or _REFERENCE_DOCUMENT.search(description)
        and _REFERENCE_MISSING.search(description)
        or _SUPPLEMENT.search(description)
        and not _COMPARISON.search(description)
        or _INCOMPLETE_CATEGORY.search(category)
        and _SUPPLEMENT.search(suggestion)
    )


def report_filter_reason(item: dict, *, review_scope: str = "") -> str:
    if not isinstance(item, dict):
        return ""
    if is_no_action_finding(item):
        return "no_action"
    if review_scope in REVIEW_SCOPES and is_supplement_only_finding(item):
        return "supplement_only"
    return ""
