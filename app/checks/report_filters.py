"""按条目结论过滤无需处理及越出规范性、一致性范围的补充说明建议。"""

import re
import unicodedata

REVIEW_SCOPES = frozenset({"compliance", "consistency"})
REPORT_FILTER_VERSION = "3-no-impact"
_DESCRIPTION = (
    "description",
    "issue",
    "problem",
    "finding",
    "疑似问题",
    "问题描述",
    "偏差说明",
    "差异说明",
    "冲突或缺失说明",
    "问题判断",
)
_SUGGESTION = (
    "suggestion",
    "recommendation",
    "fix",
    "修改建议",
    "建议修改",
    "建议处理方式",
    "需核对的依据",
    "建议",
)
_IMPACT = ("impact", "risk", "影响", "影响说明", "客户影响", "可能影响")
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
_CONTEXT = (
    r"(?:工况|适用(?:条件|范围|场景|版本|对象)|工作状态|运行条件|测量条件|选择依据|"
    r"取值依据|判断依据|条件边界|各范围|对应关系|"
    r"(?:环境|工作|输入|输出)?(?:温度|湿度|电压|电流|功率|频率)范围)"
)
_DETAIL = r"(?:具体(?:步骤|操作|方法)|详细(?:步骤|操作|说明)|操作(?:步骤|方法|细节)|如何(?:操作|配置|设置)|步骤细节|参数含义|术语解释|背景说明|验证方法|预期结果|异常处理|示例|必要说明|具体说明)"
_REFERENCE_DOCUMENT = re.compile(r"手册|指南|引用[^,;。\n]{0,15}(?:资料|文档|标准)")
_REFERENCE_METADATA = r"(?:版本号?|版本信息|发布日期|获取(?:方式|路径|入口)|下载(?:地址|链接|路径)|超链接|URL|文件路径|获取入口)"
_INCOMPLETE_CATEGORY = re.compile(
    r"(?:内容|信息|说明|步骤)(?:不完整|完整性|缺失|不足)|完整性"
)
_MISSING_PATTERN = re.compile(_MISSING)
_CONTEXT_PATTERN = re.compile(_CONTEXT)

# 引号内容作为一个整体参与分句，原文措辞不作为模型的判断。
_CLAUSE_BOUNDARY = re.compile(
    r"[,;。!?！？\n]+|(?<!\d)\.(?=\s|$)|但是|然而|而且|并且|同时|此外|另外|但|"
    r"(?:并|且)(?=仍|需|应|要|请|建议|补|添|改|更正|修正|纠正|替换|增加|删除|统一|将|把|第|同一|正文|名称|型号)"
)
_SUBJECT = r"(?:(?:此处|这里|该处|文中|原文|当前|该|两处|这两处|这些)(?:的)?)?(?:表述|表达|描述|写法|含义|规定|内容|格式|术语|词语|词|名称|用法|交叉引用|引用|条目|项)?"
_NO_ACTION_SUBJECT = re.compile(_SUBJECT)
_NO_ACTION_OBJECT = re.compile(r"(?:数值|电压值|参数值|参数|表述|内容|原文|格式|标点)?")
_POSITIVE_CONCLUSION = re.compile(
    rf"{_SUBJECT}(?:结合上下文)?(?:是|为|使用|属于)?(?:正常(?:简称|缩写|用法)|正确|准确|一致|等价|等效|可(?:以)?接受|符合(?:要求|规范)|适用于当前章节)|"
    r"(?:明确)?非问题|"
    r"(?:these|the|both|this)\s+(?:descriptions?|statements?|expressions?)\s+(?:are|is)\s+(?:equivalent|correct|consistent|acceptable)",
    re.I,
)
_NEUTRAL_CONCLUSION = re.compile(r"(?:本次)?检查(?:已)?完成")
_QUALIFIER = r"(?:各范围(?:对应的?)?|各个|各|对应的?|相关的?|具体的?|详细的?|必要的?|[\w\u4e00-\u9fff-]{1,16}时|并柜)?"
_DETAIL_TARGET = rf"{_QUALIFIER}(?:适用工况|{_CONTEXT}|{_DETAIL}|操作步骤|操作说明|步骤|说明)(?:的)?(?:说明|解释|信息|细节)?"
_REFERENCE_TARGET = (
    rf"(?:手册的?|指南的?|引用文档的?)?{_REFERENCE_METADATA}(?:信息|说明)?"
)
_TARGET_JOIN = r"(?:及|和|与|或|/|、|以及)"
_DETAIL_TARGETS = rf"{_DETAIL_TARGET}(?:{_TARGET_JOIN}{_DETAIL_TARGET})*"
_REFERENCE_TARGETS = rf"{_REFERENCE_TARGET}(?:{_TARGET_JOIN}{_REFERENCE_TARGET})*"
_SUPPLEMENT_SUBJECT = r"(?:参数说明|文档|文中|正文|本节|此处|该处)(?:中|的)?"
_PURE_MISSING_DETAIL = re.compile(
    rf"(?:{_SUPPLEMENT_SUBJECT})?(?:{_MISSING}{_DETAIL_TARGETS}|{_DETAIL_TARGETS}{_MISSING})"
)
_PURE_COARSE_STEP = re.compile(
    r"(?:操作步骤|步骤|操作流程|操作过程|操作说明|配置说明)(?:过于)?"
    r"(?:笼统|简略|粗略|概括|简单|不够详细|不够具体|细节不足)"
)
_REFERENCE_SUBJECT = r"(?:(?:文中|正文)?(?:引用|参考)(?:的)?(?:《[^》\n]+》|用户手册|手册|指南|文档|资料)|手册|指南)(?:中|的)?"
_PURE_MISSING_REFERENCE = re.compile(
    rf"(?:{_REFERENCE_SUBJECT})?(?:{_MISSING}{_REFERENCE_TARGETS}|{_REFERENCE_TARGETS}{_MISSING})"
)
_SUPPLEMENT_ACTION = r"(?:(?:建议|请|应当|应|需要|需)(?:进一步)?)?(?:补充|补写|增加|添加|提供|明确|解释|说明|细化|展开|完善|澄清)"
_PURE_DETAIL_SUGGESTION = re.compile(rf"{_SUPPLEMENT_ACTION}{_DETAIL_TARGETS}")
_PURE_REFERENCE_SUGGESTION = re.compile(rf"{_SUPPLEMENT_ACTION}{_REFERENCE_TARGETS}")
_GENERIC_INCOMPLETE = re.compile(
    r"[\w\u4e00-\u9fff-]{0,20}(?:说明|内容|信息|步骤)(?:不完整|不充分|不足)"
)
_UNSCOPED_VALUES = re.compile(
    r"(?:输入|输出|额定|工作)?(?:电压|电流|功率|温度|频率|数值|取值|范围)"
    r"(?:在)?(?:[一二三四五六七八九十两\d]+处|多处|前后)?(?:不同|不一致|存在差异)"
)
_RANGE_BACKGROUND = re.compile(
    r"(?:文中|正文|表中)?(?:给出|提供|列出)了?[一二三四五六七八九十两\d]+(?:个|种)?范围"
)
_NO_IMPACT_VALUE = r"(?:无|(?:无|没有|并无)(?:实际|实质性?)?影响|no\s+impact)"
_NO_IMPACT = re.compile(
    rf"{_NO_IMPACT_VALUE}(?:[。.!;,\s]+{_NO_IMPACT_VALUE})*[。.!;,\s]*", re.I
)
_EMPTY_SUGGESTION = re.compile(r"无(?:建议|修改建议)?|[-—–]+|N/?A|None|null", re.I)
_UNRESOLVED_DESCRIPTION = re.compile(
    r"错误|有误|不正确|不一致|不统一|不相同|不同|不符|不匹配|矛盾|冲突|差异|歧义|"
    r"写成|标成|写反|误用|颠倒|遗漏|漏掉|未找到|未发现|未见|不存在|"
    r"(?:存在|出现|有)[^,;。\n]{0,12}(?:问题|异常|风险|偏差)|"
    r"需|须|应当|必须|建议|请|待|核实|核对|核查|复核|验证|评估|"
    r"统一|补充|补齐|补全|补上|补写|补入|补单位|改用|改正|删除|删去|去掉|"
    r"修正|纠正|更正|替换|调整|增加|添加|完善|修改(?!记录|历史|日志)|"
    r"应(?:为|补|改|添|增|删|标|注明|统一|调整|使用)|"
    r"疑似|可能|尚未|仍未|不确定|不一定|或许|也许|无法|不能|不应|不宜|"
    r"并非|不是|不代表|难以|假如|仅当|另一处|却|"
    r"^(?:add|remove|change|correct|update|replace)\b|"
    r"\b(?:wrong|incorrect|inconsistent|inconsistency|conflict|mismatch|missing|"
    r"error|defect|unclear|uncertain|verify|check|review|confirm|should|must|"
    r"need|please|may|might|cannot)\b",
    re.I,
)


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
    """结合描述、影响和建议确认无需处理，标签仅作为原始信息保留。"""
    if _has_placeholder(item):
        return False
    claims = _claims(item)
    decisions = [_no_action_claim(claim) for claim in claims]
    if decisions and all(decisions) and "no_action" in decisions:
        return True
    return _no_impact_without_action(item)


def is_supplement_only_finding(item: dict) -> bool:
    """逐个主张确认补充说明范围，独立错误和未识别的主张继续保留。"""
    if _has_placeholder(item) or not _text(item, _DESCRIPTION):
        return False
    claims = _claims(item)
    reference = bool(_REFERENCE_DOCUMENT.search("\n".join(claims)))
    incomplete_category = bool(
        _INCOMPLETE_CATEGORY.search(
            _text(item, ("category", "issue_type", "problem_type", "问题类型"))
        )
    )
    decisions = [
        _supplement_claim(claim, reference, incomplete_category) for claim in claims
    ]
    # 数值差异只作为缺少工况说明的背景，具体修正或未知主张仍需逐条保留。
    missing_context = any(
        decision == "supplement"
        and _CONTEXT_PATTERN.search(claim)
        and _MISSING_PATTERN.search(claim)
        for claim, decision in zip(claims, decisions)
    )
    for claim, decision in zip(claims, decisions):
        if decision:
            continue
        if missing_context and (
            _UNSCOPED_VALUES.fullmatch(claim) or _RANGE_BACKGROUND.fullmatch(claim)
        ):
            continue
        return False
    return "supplement" in decisions


def _has_placeholder(item: dict) -> bool:
    return bool(_PLACEHOLDER.search(_text(item, _DESCRIPTION + _EXCERPT)))


def _claims(
    item: dict, fields: tuple[str, ...] = _DESCRIPTION + _SUGGESTION
) -> list[str]:
    text = _QUOTED.sub("\ufff0", _text(item, fields))
    return [
        cleaned
        for part in _CLAUSE_BOUNDARY.split(text)
        if (cleaned := re.sub(r"^(?:且|仍|还|则)\s*", "", part.strip()).strip())
    ]


def _no_impact_without_action(item: dict) -> bool:
    if not _NO_IMPACT.fullmatch(_text(item, _IMPACT)):
        return False
    if "?" in _QUOTED.sub("", _text(item, _DESCRIPTION + _SUGGESTION)):
        return False
    if any(
        not _EMPTY_SUGGESTION.fullmatch(claim)
        and _no_action_claim(claim) != "no_action"
        for claim in _claims(item, _SUGGESTION)
    ):
        return False
    claims = _claims(item, _DESCRIPTION)
    # 明确无影响且无有效建议时允许背景叙述，独立错误和待核实事项保留。
    return bool(claims) and all(
        _no_action_claim(claim)
        or not any(
            pattern.search(claim)
            for pattern in (
                _CONDITIONAL,
                _DEFECT,
                _CORRECTION,
                _MISSING_PATTERN,
                _UNRESOLVED_DESCRIPTION,
            )
        )
        for claim in claims
    )


def _no_action_claim(claim: str) -> str:
    if _CONDITIONAL.search(claim):
        return ""
    if _POSITIVE_CONCLUSION.fullmatch(claim):
        return "no_action"
    if _NEUTRAL_CONCLUSION.fullmatch(claim):
        return "neutral"
    match = _NO_ACTION.search(claim)
    if match and not _NEGATED_NO_ACTION.search(claim[: match.start()]):
        if _NO_ACTION_SUBJECT.fullmatch(
            claim[: match.start()].strip()
        ) and _NO_ACTION_OBJECT.fullmatch(claim[match.end() :].strip()):
            return "no_action"
    return ""


def _supplement_claim(claim: str, reference: bool, incomplete_category: bool) -> str:
    no_action = _no_action_claim(claim)
    if no_action:
        return no_action
    if _DEFECT.search(claim) or _CORRECTION.search(claim) or _CONDITIONAL.search(claim):
        return ""
    if (
        _PURE_MISSING_DETAIL.fullmatch(claim)
        or _PURE_COARSE_STEP.fullmatch(claim)
        or _PURE_DETAIL_SUGGESTION.fullmatch(claim)
        or reference
        and (
            _PURE_MISSING_REFERENCE.fullmatch(claim)
            or _PURE_REFERENCE_SUGGESTION.fullmatch(claim)
        )
        or incomplete_category
        and _GENERIC_INCOMPLETE.fullmatch(claim)
    ):
        return "supplement"
    return ""


def report_filter_audit(items: list, *, review_scope: str = "") -> dict:
    """保存过滤时的条目序号、范围及原因；展示按当前规则重新计算。"""
    return {
        "version": REPORT_FILTER_VERSION,
        "scope": review_scope,
        "items": [
            {"index": index, "reason": reason}
            for index, item in enumerate(items, start=1)
            if (reason := report_filter_reason(item, review_scope=review_scope))
        ],
    }


def report_filter_reason(item: dict, *, review_scope: str = "") -> str:
    if not isinstance(item, dict):
        return ""
    if is_no_action_finding(item):
        return "no_action"
    if review_scope in REVIEW_SCOPES and is_supplement_only_finding(item):
        return "supplement_only"
    return ""
