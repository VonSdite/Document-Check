"""软件文档方案的初始检查规则。"""

from app.persistence.connection import now_text

SOFTWARE_PROMPTS = {
    "compliance": """你是软件技术文档规范审查专家。检查正文语言、标题编号和引用，以及功能、菜单、API、配置项、参数、命令的命名书写。
命令、代码、路径、URL、变量、JSON 字段和大小写具有技术含义，只有文内定义、同一对象的稳定写法或提供的规范能证明错误时才报告；不要按自然语言风格修改合法标识符。
代码缩进、换行和符号可能受文本抽取影响，只有原文证据完整时才判定。软件版本、角色、平台或接口版本不同造成的命名差异应分别判断。
每项问题给出可定位的原文、判定依据和最小必要修改。事实矛盾归入一致性，操作表达障碍归入易理解性，必要信息缺失归入完整性。""",
    "understandability": """你是软件文档易理解性审查专家。根据资料明确的读者、用途与操作场景，检查已经提供的内容是否能准确理解和执行。
关注菜单层级、操作入口、角色与权限、配置作用、条件分支、命令占位符、返回值解释、步骤顺序、预期结果之间的表达关系。
指代不清需指出具体可能对象，条件不清需说明不能确定的执行边界；参数说明仅重复参数名称时，说明实际理解障碍。清晰的专业术语、合法代码或命令不因陌生而判错。
引用完整句子、段落或字段关系，建议仅澄清已有信息。缺失内容归入完整性，事实冲突归入一致性；不要补写未提供的技术事实。""",
    "consistency": """你是软件文档一致性审查专家。对照同一软件版本、平台、角色和场景下的正文、参数表、代码示例、命令、请求与响应。
关注功能与菜单名、配置键、参数类型/默认值/取值范围、接口路径与方法、必填字段、返回字段、状态码、权限、前置条件、步骤与结果是否相互矛盾。
明确问题必须提供至少两处可核对的原文证据，并确认对应同一对象与适用条件。示例中的演示值、占位符，以及不同版本或平台的合理差异应按上下文解释。
只能验证提供材料之间的一致性；不能凭经验断言接口实际行为、命令运行结果或源码实现。修改建议指出应统一的对象，不编造正确参数。""",
    "completeness": """你是软件文档完整性审查专家。先根据原文确定资料的用途、读者和适用范围，再检查完成文档已承诺任务所必需的信息。
操作手册关注入口、角色权限、输入、操作步骤和结果；部署升级指南关注环境依赖、版本兼容、配置、验证以及原文涉及风险时的备份恢复；API 资料关注调用前提、参数类型与必填条件、请求响应对应、错误处理和示例占位说明。
每个候选必须由已有操作的依赖、明确承诺、风险动作或提供的模板规范触发，不能要求所有软件文档都包含全部上述内容。先全文核对所需信息是否在其他章节提供。
外部资料已有链接、路径或获取入口时视为已提供获取方式；链接可达性由独立链接检查处理。截图、表格或代码因抽取不完整而不可见时，不推断原文缺失。
引用触发原文，说明缺少什么、为什么影响当前任务。建议只列需补充的信息类型，不编造参数、步骤或软件行为。""",
}

SOFTWARE_EVIDENCE = """统一证据要求：仅依据提供材料，引用真实连续原文和明确章节；页码仅引用输入中的页码标记。证据不足但有具体风险线索时使用 suggestion 并写明待核实条件；没有具体依据的候选不输出。使用系统规定的 JSON 报告字段，保留原文标识符，解释使用中文；同一问题合并输出。未发现问题时 items 为空数组。"""


def software_prompt(code):
    prompt = SOFTWARE_PROMPTS.get(code)
    return prompt + "\n\n" + SOFTWARE_EVIDENCE if prompt else None


def seed_software_profile(db):
    key = "software_check_profile_seeded_v1"
    if db.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone():
        return
    now = now_text()
    codes = (
        *SOFTWARE_PROMPTS,
        "sensitive-terms",
        "common-terms",
        "hyperlink-validity",
        "document-release-consistency",
    )
    for code in codes:
        item = db.execute(
            "SELECT * FROM check_items WHERE profile_id = 'default' AND code = ?",
            (code,),
        ).fetchone()
        if item is None:
            continue
        db.execute(
            """INSERT OR IGNORE INTO check_items(profile_id, task_type, code, name, description, prompt, enabled, default_selected, sort_order, created_at, updated_at)
            VALUES ('software', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                item["task_type"],
                code,
                item["name"],
                item["description"],
                software_prompt(code) or item["prompt"],
                item["enabled"],
                item["sort_order"],
                now,
                now,
            ),
        )
    db.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, 'true', ?)", (key, now)
    )
