import json
import sqlite3
from datetime import datetime

from flask import current_app, g

from .task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE, IMAGE_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE, VIDEO_TASK_TYPE


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g.db = db
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS check_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL DEFAULT 'document_check',
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            prompt TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL DEFAULT 'document_check',
            ip TEXT NOT NULL,
            username_snapshot TEXT,
            owner_subject TEXT,
            owner_name_snapshot TEXT,
            owner_source TEXT,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            document_text TEXT,
            document_meta_json TEXT,
            checks_json TEXT NOT NULL,
            checks_snapshot_json TEXT,
            provider_name TEXT,
            model_name TEXT NOT NULL,
            api_base TEXT NOT NULL,
            api_key TEXT,
            request_timeout INTEGER NOT NULL DEFAULT 3600,
            max_input_chars INTEGER NOT NULL DEFAULT 80000,
            force_disable_thinking INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            result_json TEXT,
            summary TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS user_model_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_subject TEXT NOT NULL,
            name TEXT NOT NULL,
            api_base TEXT NOT NULL,
            api_key TEXT,
            request_timeout INTEGER NOT NULL DEFAULT 3600,
            max_input_chars INTEGER NOT NULL DEFAULT 80000,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_model_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            force_disable_thinking INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(provider_id) REFERENCES user_model_providers(id) ON DELETE CASCADE,
            UNIQUE(provider_id, model_name, force_disable_thinking)
        );

        CREATE TABLE IF NOT EXISTS ip_usernames (
            ip TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_ip_created ON tasks(ip, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_user_model_providers_owner ON user_model_providers(owner_subject, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_model_configs_provider ON user_model_configs(provider_id, sort_order ASC, id ASC);
        """
    )
    _ensure_column(db, "check_items", "task_type", f"TEXT NOT NULL DEFAULT '{DOCUMENT_TASK_TYPE}'")
    _ensure_column(db, "tasks", "task_type", f"TEXT NOT NULL DEFAULT '{DOCUMENT_TASK_TYPE}'")
    _ensure_column(db, "tasks", "document_text", "TEXT")
    _ensure_column(db, "tasks", "document_meta_json", "TEXT")
    _ensure_column(db, "tasks", "checks_snapshot_json", "TEXT")
    _ensure_column(db, "tasks", "force_disable_thinking", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "tasks", "owner_subject", "TEXT")
    _ensure_column(db, "tasks", "owner_name_snapshot", "TEXT")
    _ensure_column(db, "tasks", "owner_source", "TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_created ON tasks(owner_subject, created_at DESC)")
    _migrate_task_owners(db)
    current_app.teardown_appcontext(close_db)
    db.commit()


def _ensure_column(db, table: str, column: str, definition: str):
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_task_owners(db):
    db.execute(
        """
        UPDATE tasks
        SET owner_subject = 'ip:' || ip
        WHERE owner_subject IS NULL OR owner_subject = ''
        """
    )
    db.execute(
        """
        UPDATE tasks
        SET owner_name_snapshot = username_snapshot
        WHERE (owner_name_snapshot IS NULL OR owner_name_snapshot = '') AND username_snapshot IS NOT NULL
        """
    )
    db.execute(
        """
        UPDATE tasks
        SET owner_source = 'ip'
        WHERE owner_source IS NULL OR owner_source = ''
        """
    )


def set_setting(key: str, value):
    db = get_db()
    db.execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), now_text()),
    )
    db.commit()


def get_setting(key: str, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def get_bool_setting(key: str, default: bool = False) -> bool:
    value = get_setting(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def owner_subject_from_ip(ip: str) -> str:
    return f"ip:{str(ip or '0.0.0.0').strip() or '0.0.0.0'}"


def get_ip_username(ip: str) -> str:
    ip = str(ip or "").strip()
    if not ip:
        return ""
    row = get_db().execute("SELECT username FROM ip_usernames WHERE ip = ?", (ip,)).fetchone()
    return row["username"] if row is not None else ""


def set_ip_username(ip: str, username: str):
    ip = str(ip or "").strip()
    username = str(username or "").strip()
    if not ip:
        return
    db = get_db()
    if not username:
        db.execute("DELETE FROM ip_usernames WHERE ip = ?", (ip,))
        db.commit()
        return
    now = now_text()
    db.execute(
        """
        INSERT INTO ip_usernames(ip, username, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ip) DO UPDATE SET username = excluded.username, updated_at = excluded.updated_at
        """,
        (ip, username, now, now),
    )
    db.commit()


DEFAULT_DOCUMENT_CHECK_ITEMS = (
    {
        "code": "compliance",
        "name": "文档规范性检查",
        "description": "检查文档是否符合正式文档写作规范、结构规范和表达规范。",
        "prompt": """你是一名严谨的客户资料规范审查专家兼资深技术文档编辑，主要审查面向客户发布的产品资料、用户手册、安装指南、维护指南、调测指南、白皮书或交付文档。请检查文档是否符合正式客户资料的写作规范、结构规范、表达规范和发布规范，重点发现会影响客户理解、信任、交付质量或发布合规性的规范问题。
注意：文档文本由解析器抽取得到，换行、分页、表格分隔符、行首行尾空白可能与原版版式不同；除非同一原文行内明确可见连续空格或异常空格，不要把解析换行/分页造成的空白判为“多余空格”。
重点关注：
1. 客户资料定位：是否存在内部沟通口吻、研发备注、评审意见、TODO/占位符、草稿痕迹、内部系统名、内部责任人或不应面向客户暴露的信息。
2. 结构与层级：标题层级、章节顺序、编号、目录/章节/附录关系是否清晰；是否存在标题缺失、层级跳跃、同级标题风格不一致、章节内容与标题不匹配。
3. 表达与语气：是否使用正式、客观、面向客户的表达；是否存在口语化、含糊承诺、夸大宣传、主观评价、过度绝对化或不适合客户资料的措辞。
4. 术语与命名规范：产品名称、功能名称、部件名称、界面名称、菜单路径、按钮名、参数名、中英文术语、缩写解释是否符合正式资料写法并保持规范。
5. 图表与引用规范：图题、表题、图号、表号、步骤号、章节引用、附录引用、链接、公式、示例编号是否规范；图表说明、正文引用与对象关系是否清楚。
6. 操作与安全提示规范：前提条件、操作步骤、注意/警告/危险/提示等安全信息是否格式清晰、语气恰当、位置合理；禁令、强制要求和建议是否表达明确。
7. 技术信息呈现：参数、单位、范围、默认值、版本、环境要求、约束条件、例外条件是否以客户可理解的方式呈现；表格字段和说明是否完整。
8. 发布完整性：是否存在空章节、重复章节、明显缺失的说明、未定义术语、未解释缩写、附件/参考资料缺失、版本/修订记录/适用范围表达不清等问题。

输出要求：
1. 先给出总体规范性结论，说明是否适合作为面向客户发布资料，以及主要风险类别。
2. 按问题逐条列出：问题类型、位置、原文摘录、问题描述、客户影响、修改建议。
3. 位置优先包含页码/章节/标题/表格/图号/附近文本；无法定位时说明“位置线索不足”。
4. 对可能涉及业务口径、法务合规或品牌规范但文档证据不足的问题，标注“需人工确认”。
5. 不要把解析换行、分页、表格分隔符造成的格式变化误判为版式问题；不要编造文档中不存在的内容。
6. 如果未发现明显问题，明确说明“未发现明显客户资料规范性问题”。""",
        "sort_order": 10,
    },
    {
        "code": "consistency",
        "name": "全文一致性检查",
        "description": "检查全文内时间、数字、名称、术语、口径和前后表述是否一致。",
        "prompt": """你是一名严谨的文档审查专家兼资深技术文档编辑，擅长发现资料文档内部结构、逻辑、内容、术语、技术参数及约束性表述之间的矛盾与不一致。请检查文档内部是否存在前后不一致、互相矛盾、引用错误或口径漂移，不要进行一般润色，也不要脱离文档内容补充判断。
重点关注：
1. 结构与内容一致性：章节标题、段落主题、小节范围是否与正文内容匹配；目录、标题、编号、附件、图表编号、交叉引用是否对应。
2. 逻辑与结论一致性：前后文条件、步骤顺序、因果关系、结论与依据是否矛盾；同一事项在不同章节是否出现相反或遗漏的前提。
3. 数据与技术参数一致性：正文、表格、图片说明、参数表、示例中的数值、单位、阈值、范围、默认值、版本号、接口名、产品型号、产品名称是否一致；单位写法是否统一且不造成歧义。
4. 约束性与安全信息一致性：重点检查安全、环境、安装、运行、维护、故障处理等章节中关于同一事项的强制程度是否一致，包括“严禁、禁止、不可、不得、必须、应、建议、可、允许、例外、请咨询”等表述；对禁令、建议、可选、例外条件和适用范围的冲突要特别标注。
5. 术语与命名一致性：人名/组织名、项目名、产品名、部件名、功能名、术语定义、缩写、中英文名称是否前后一致。
6. 引用与编号一致性：章节号、图号、表号、步骤号、附录号、公式号、链接或引用对象是否存在错指、缺失、重复或与实际标题/内容不匹配。
7. 其他潜在不一致：同一对象的状态、版本、配置、权限、操作对象、适用范围、例外条件、环境要求、维护周期等是否前后不一致。

检查方法：
1. 对同一事项跨章节、正文与表格、正文与图表说明、参数表与步骤说明进行对照。
2. 可优先检索并比对约束性关键词：严禁、禁止、不可、不得、必须、应、建议、可、允许、例外、请咨询。
3. 只依据文档中可定位的文字证据判断；证据不足或需要业务确认时标注“需人工确认”。

输出要求：
1. 先给出总体一致性风险结论，说明风险等级（高/中/低/未发现明显风险）和主要风险类别。
2. 按条清晰列出：问题类型、位置、原文摘录、问题描述、影响说明、修改建议。
3. 每条问题至少提供两处可对照的位置线索或说明缺少对应依据；位置优先包含页码/章节/表格/图号/附近文本。
4. 对约束性表述冲突，明确写出两处表述的强制程度差异（如“必须”与“建议”、“禁止”与“允许”）及适用条件是否一致。
5. 不要把同义表达、合理简称、单位等价换算或上下文已明确的差异误判为不一致。
6. 如果未发现明显问题，明确说明“未发现明显全文一致性问题”。""",
        "sort_order": 20,
    },
    {
        "code": "typo",
        "name": "错别字检查",
        "description": "检查错别字、漏字、多字、标点和常见语病。",
        "prompt": """你是一名中文校对专家。请检查文档中的错别字、漏字、多字、标点误用、重复表达、常见语病和明显不通顺句子。
注意：文档文本由解析器抽取得到，换行、分页、表格分隔符、行首行尾空白可能与原版版式不同；不要把解析换行/分页造成的空白当作多余空格或标点问题。
定位要求：
1. 每条问题必须给出可定位信息，不要只写问题本身。
2. 位置中优先引用文档文本里的页码标记，例如“[第12页]”；如果文档文本没有页码标记，明确写“页码：未提取”，不要编造页码。
3. 同时给出最近的章节/标题/小节编号、工作表名或表格行线索；如果无法识别章节，写“章节：未识别”，并补充附近短文本作为定位线索。
输出要求：
1. 按条列出：位置（文件/页码/章节或工作表/附近线索）、原文片段、疑似问题、建议修改、理由。
2. 对专业术语、人名、地名、品牌名保持谨慎，不确定时标注“疑似”。
3. 如果未发现明显问题，明确说明“未发现明显错别字或语病”。""",
        "sort_order": 30,
    },
)

DEFAULT_CONSISTENCY_CHECK_ITEMS = (
    {
        "code": "consistency-cross-document",
        "name": "多文档对照检查",
        "description": "以素材文档为依据，检查资料是否存在偏差、遗漏、冲突或缺少依据的说法。",
        "prompt": """你是一名多文档对照审查专家。用户会提供两组内容：素材文档和资料。资料是根据素材文档写作生成的，请以素材文档作为依据，检查资料内容是否与素材内容一致，是否存在偏差、遗漏或需要人工确认的地方。
重点关注：
1. 产品/项目/组织/人名/地点/日期/版本/编号/术语是否一致。
2. 指标、参数、规格、数量、单位、阈值、流程步骤和限制条件是否一致。
3. 资料是否遗漏素材文档中的关键约束，或新增了素材文档没有支撑的说法。
4. 多份资料之间如存在互相冲突，也请标注，但优先说明它们与素材文档的关系。

输出要求：
1. 先给出总体结论，说明一致性风险等级。
2. 按条列出偏差：资料名称、位置线索、资料表述、素材文档依据、偏差说明、修改建议。
3. 对证据不足或需要业务判断的问题标注“需人工确认”。
4. 如果未发现明显偏差，明确说明“未发现资料内容与素材文档存在明显不一致”。不要编造文档中不存在的内容。""",
        "sort_order": 10,
    },
)

DEFAULT_LANGUAGE_CONSISTENCY_CHECK_ITEMS = (
    {
        "code": "language-consistency-cross-lingual",
        "name": "跨语种内容一致性对比",
        "description": "对比两个不同语种文档的内容是否一致，识别缺失、增补、翻译偏差和关键事实差异。",
        "prompt": """你是一名严谨的跨语种文档一致性审查专家。用户会提供两个不同语种或不同语言版本的资料文档，并附带系统静态预检摘要。请综合静态预检线索和两份文档正文，判断两者表达的业务事实、技术要求、步骤、限制条件、风险提示和资料结构是否一致，重点发现缺失、增补、误译、弱化/强化、冲突或需要人工确认的差异。最终报告必须使用中文陈述。

注意：
1. 静态预检摘要只作为优先核对线索，不要仅凭长度、标题数量或抽取要素差异直接下结论。
2. 两种语言的表达顺序、句式、同义改写、合理本地化、单位等价换算、术语常见译法不应误判为不一致。
3. 只依据提供的文档内容判断，不要补充外部事实，不要编造文档中不存在的内容。
4. 对仅存在措辞、标题或结构细微差异，但不影响理解、无实质影响且无需修改的内容，不要作为差异条目输出。

重点关注：
1. 关键主题和章节覆盖：两份文档是否覆盖相同功能、场景、流程、前提条件、适用范围和结论。
2. 缺失与增补：任一文档是否遗漏另一文档中的关键段落、表格字段、步骤、注意事项、安全/法律/合规提示，或新增另一文档没有对应依据的内容。
3. 事实与参数一致性：产品名、版本、型号、日期、编号、数量、单位、阈值、默认值、接口、URL、IP、邮箱、命令、配置项是否一致。
4. 约束强度一致性：must/shall/required/prohibited/optional/recommended 等约束与中文“必须、应、不得、禁止、可、建议”等是否存在强弱变化。
5. 术语与命名一致性：专业术语、功能名、菜单路径、按钮、字段、角色、组织/地点/人名是否保持一致或有合理译名。
6. 结构与引用一致性：章节、图表、附录、步骤编号、交叉引用、链接或附件说明是否对应。

输出要求：
1. 先给出总体结论：两份文档是否基本一致、风险等级（高/中/低/未发现明显风险）和主要差异类型。
2. 按条列出差异：问题类型、位置、文档A证据、文档B证据、差异说明、影响、修改建议。
3. 位置优先包含文件名、页码/章节/标题/表格/步骤/附近文本；无法定位时说明“位置线索不足”。
4. 对证据不足、可能是合理本地化或需要业务确认的内容，明确标注“需人工确认”。
5. 只列出需要修改、需要补充或需要人工确认的实质性差异；不要列出“无实质影响”“影响不大”“无需修改”“无需处理”的条目。
6. 单独概括“缺失内容”和“关键事实/数字差异”；若没有发现，明确说明“未发现明显缺失内容”或“未发现明显关键事实差异”。
7. 如果整体未发现明显差异，明确说明“未发现两份跨语种文档存在明显内容不一致或缺失”。""",
        "sort_order": 10,
    },
)

DEFAULT_VIDEO_CHECK_ITEMS = (
    {
        "code": "video-installation-sequence",
        "name": "安装步骤顺序检查",
        "description": "检查硬件安装视频中的部件安装、固定、接线前后顺序和关键步骤是否合理。",
        "prompt": """你是一名硬件产品安装调测视频质检专家。系统会从视频中按时间轴抽取关键帧，请结合帧顺序判断安装过程是否存在明显风险。
重点关注：
1. 安装顺序是否合理，例如先断电/验电再接线、先固定设备再接线、先检查配件再上电。
2. 是否遗漏关键步骤，例如固定螺钉、接地、线缆整理、端子紧固、防护盖复位、上电前检查。
3. 是否出现明显反向、跳步、重复操作、拆装顺序冲突或与硬件产品常规安装要求不符的操作。
4. 只依据可见视频帧判断；由于视频是抽帧采样，前后连续动作证据不足时标注“需人工确认”。

输出要求：
1. 先给出总体判断。
2. 按条列出问题：时间点、可见证据、问题描述、可能影响、修改建议。
3. 没有明确问题时说明“未发现明显安装步骤顺序问题”。""",
        "sort_order": 10,
    },
    {
        "code": "video-wiring-terminal",
        "name": "接线与端子检查",
        "description": "检查视频中端子、线缆、极性、接地、线序和接线操作是否存在明显风险。",
        "prompt": """你是一名硬件接线与端子操作审查专家。请检查安装调测视频中可见的接线、端子、线缆和接地操作。
重点关注：
1. 电源线、信号线、通信线、接地线是否接到明显正确的位置，是否存在 L/N/PE、正负极、A/B、DI/DO 等可见混接风险。
2. 端子是否有明显未插紧、裸铜外露、压接不牢、屏蔽/接地遗漏、线缆拉扯、线序混乱或防护不足。
3. 工具操作是否可能损伤端子、线缆或外壳。
4. 仅在画面能看清标识和接线关系时判定为明确问题；看不清、被遮挡或缺少图纸依据时标注“需人工确认”。

输出要求：
1. 按条列出：时间点、可见端子/线缆证据、问题描述、可能影响、修改建议。
2. 没有明确问题时说明“未发现明显接线与端子问题”。""",
        "sort_order": 20,
    },
    {
        "code": "video-safety-protection",
        "name": "安全与防护检查",
        "description": "检查安装调测视频中的断电、防护、工具使用、个人安全和设备保护风险。",
        "prompt": """你是一名硬件安装安全质检专家。请检查视频中是否存在安全防护和设备保护方面的明显风险。
重点关注：
1. 是否出现带电接线、上电前未检查、手部接近裸露导体、未复位防护盖、未佩戴必要防护用品等风险。
2. 是否存在工具误用、用力过大、设备跌落/磕碰、线缆被夹压、液体/金属异物靠近设备等风险。
3. 是否缺少安全提醒或关键安全动作无法确认。
4. 抽帧无法证明的连续动作放入“需人工确认”，不要凭空推断。

输出要求：
1. 按条列出：时间点、可见风险、可能后果、建议处理。
2. 没有明确问题时说明“未发现明显安全与防护问题”。""",
        "sort_order": 30,
    },
    {
        "code": "video-commissioning-ui-parameter",
        "name": "调测界面与参数检查",
        "description": "检查调测视频中屏幕、仪表、指示灯、参数配置和验证结果是否存在明显异常。",
        "prompt": """你是一名硬件产品调测过程质检专家。请检查视频中可见的调测界面、仪表读数、指示灯、参数配置和验证结果。
重点关注：
1. 界面/仪表上的关键参数、告警、状态灯、测试结果是否显示异常或与操作目标明显不符。
2. 是否存在未保存配置、未执行验证、测试失败仍继续、告警未处理、指示灯状态异常等问题。
3. 参数值、单位、端口、设备型号、软件界面文字如看不清，应标注“需人工确认”。
4. 不要编造画面中不可见的参数或结果。

输出要求：
1. 按条列出：时间点、界面/仪表证据、问题描述、影响、修改建议。
2. 没有明确问题时说明“未发现明显调测界面与参数问题”。""",
        "sort_order": 40,
    },
    {
        "code": "video-clarity-completeness",
        "name": "视频清晰度与完整性检查",
        "description": "检查视频是否清晰覆盖关键安装调测动作，是否存在遮挡、失焦、跳剪或关键步骤不可见。",
        "prompt": """你是一名安装调测视频质量审查专家。请检查视频帧是否足以支撑用户理解完整安装调测过程。
重点关注：
1. 关键动作是否被手、工具、设备外壳或画面边缘遮挡。
2. 是否存在画面模糊、曝光过暗/过亮、文字过小、镜头抖动、关键端子或界面看不清。
3. 是否疑似缺少关键步骤、跳剪过大、只展示结果不展示过程。
4. 由于视频按帧采样，若需要回看连续片段才能确认，请标注“需人工确认”。

输出要求：
1. 按条列出：时间点、画面问题、影响、拍摄或补录建议。
2. 没有明确问题时说明“未发现明显视频清晰度与完整性问题”。""",
        "sort_order": 50,
    },
)

DEFAULT_IMAGE_CHECK_ITEMS = (
    {
        "code": "image-text-correspondence",
        "name": "图文与界面步骤一致性检查",
        "description": "综合检查图片、界面截图、操作步骤、参数、图号和说明是否与文档上下文一致。",
        "prompt": """你是一名技术资料图文与界面步骤一致性审查专家，主要审查产品用户手册、安装指南、调测指南等文档。请综合文档文本、图片清单、图片位置和本次提供的页面截图或图片内容，检查图片是否与附近文字描述、操作步骤、界面说明、图题图号、表格参数或引用关系一致。
重点关注：
1. 文档描述的产品对象、软件/Web/App/设备界面、菜单路径、页面名称、按钮、页签、字段、参数、状态、告警或调测结果，是否与图片可见内容对应。
2. 文档说明“点击/选择/输入/保存/提交/重启/验证”的对象，是否能在截图中对应看到；截图是否停留在错误页面、旧版界面、不相关页面或缺少关键结果。
3. 操作步骤顺序与截图顺序是否明显冲突，例如先保存后配置、前后截图状态倒置、步骤编号与截图内容不匹配。
4. 图片中的编号、单位、IP、端口号、协议、开关状态、图例、方向或告警文字，是否与附近文字描述冲突。
5. 文档提到必须展示的关键对象或操作结果，但图片没有体现；或图片展示了关键内容，但附近文字没有必要说明。
6. 同一批图片之间如存在步骤顺序、截图界面、图号、标题或内容重复/错位，也请标注。
7. 对只凭图片顺序、文件名或页码无法证明的问题，不要硬判错位，放入“需人工确认”。

输出要求：
1. 先给出总体判断，说明是否发现图文或界面步骤一致性风险。
2. 按条列出问题：图片名称或位置、文档文字/步骤线索、图片可见内容、冲突或缺失说明、建议修改。
3. 将“明确冲突”和“需人工确认”分开描述；对版本差异、截图裁切、文字模糊、上下文不足或需要业务判断的问题标注“需人工确认”。
4. 只有同时看到明确文档线索和图片可见证据时，才判断为“不一致”。
5. 不要仅凭文件名、页码、图片顺序或未提供的上下文推断图片插入错位；证据不足时写“需人工确认”。
6. 如果未发现明显问题，明确说明“未发现明显图文与界面步骤一致性问题”。不要编造文档或图片中不存在的内容。""",
        "sort_order": 10,
    },
    {
        "code": "image-small-language-text",
        "name": "图片语种匹配检查",
        "description": "检查图片、截图、图例和标注中的说明文字语种是否与文档主要语种一致。",
        "prompt": """你是一名图片文字语种一致性审查专家。请先根据提供的文档上下文判断文档主要语种（如中文、英文、中英混排或其他语种），再检查本次图片中可见文字、标注、截图界面、图例和说明的语种是否与文档主要语种一致。
重点关注：
1. 英文文档中图片出现中文说明、中文界面、中文标注等明显不匹配内容。
2. 中文文档中图片出现大段英文或其他语种说明，且文档上下文没有对应语种使用习惯。
3. 多语种文档中，图片文字语种是否超出文档正文、标题或图注使用的语种范围。
4. 不要把产品名、型号、单位、接口名、标准缩写、命令、URL、代码片段、配置项、寄存器名等技术性英文/符号直接判为异常，除非出现大段说明文字语种明显不匹配。
5. 对界面截图中的系统默认英文、第三方库名称或协议字段，仅在影响文档整体语种一致性时标注。

输出要求：
1. 先说明文档主要语种，以及是否发现图片文字语种不匹配。
2. 如发现，逐条列出：图片名称或位置、图片中识别到的文字、图片文字语种、文档主要语种、不匹配原因、建议处理方式。
3. 对看不清、文字过少或无法判断文档主要语种的内容标注“需人工确认”。
4. 如果未发现明显不匹配，明确说明“未发现图片文字语种与文档语种明显不一致”。不要编造图片或文档中不存在的文字。""",
        "sort_order": 20,
    },
    {
        "code": "image-wiring",
        "name": "设备安装与接线检查",
        "description": "检查设备外观、安装方向、端口、附件、端子、极性、线缆颜色、线号和连接关系是否存在明显风险。",
        "prompt": """你是一名产品设备安装与接线审查专家。请结合文档文本、图片位置和本次提供的设备照片、安装图、结构图、接线图或接线照片，检查设备外观、安装方式、端口端子、线缆和连接关系是否与文档说明一致，并标注明显风险。
重点关注：
1. 产品型号、设备正反面、端口/接口/指示灯/按键/拨码/标签位置是否与文档描述或附近图注一致。
2. 安装方向、壁挂/导轨/机柜/桌面安装方式、固定孔位、螺钉、支架、卡扣、接地位置、线缆出线方向是否与步骤说明冲突。
3. 图中展示的配件、工具、线缆、天线、电源、端子或保护件是否与文档列出的物料或安装步骤明显不一致。
4. L/N/PE、+/−、A/B、485+/485−、DI/DO、AI/AO、COM、GND、VCC、电源输入/输出等端子或极性是否与文档说明冲突。
5. 线缆颜色、线号、端子编号、端口名称、屏蔽层、接地符号、跳线或短接关系是否与图中可见标识和文字说明不一致。
6. 接线方向、进出线位置、交叉连接、端子排顺序、设备间连接关系是否存在明显反向、错位或漏接风险。
7. 图片模糊、端子文字过小、线缆被遮挡、缺少现场条件或需要实物规格/BOM/工程设计确认时，标注“需人工确认”。

输出要求：
1. 先给出总体判断，说明是否发现明显设备安装或接线风险。
2. 按条列出问题：图片名称或位置、文档线索、图片可见证据、问题描述、可能影响、建议修改或需核对的依据。
3. 将明确问题和“需人工确认”分开描述。
4. 只依据提供的文本和图片可见内容，不要补全不可见接线，也不要替代专业电气设计审核。
5. 如果未发现明显问题，明确说明“未发现明显设备安装与接线问题”。""",
        "sort_order": 25,
    },
    {
        "code": "image-figure-table-title-standard",
        "name": "图表标题可见性复核",
        "description": "复核页面截图中的图、表附近是否可见“图x-x 标题”“表x-x 标题”等规范标题，并标注疑似缺失项。",
        "prompt": """你是一名技术文档图表标题可见性复核专家。请结合文档上下文、图片位置和本次提供的页面截图，复核图、表、流程图、设备图、界面截图或表格附近是否可见规范标题。
正确形式示例：
1. 图标题：类似“图3-2 iIOT-WEC04C5网关外观（02314WHE）”，通常包含“图”+章节编号/序号+标题文字。
2. 表标题：类似“表3-1 IoT网关型号介绍”，通常包含“表”+章节编号/序号+标题文字。

复核步骤：
1. 先逐项识别当前页面中可见的图示、设备图、流程图、界面截图、表格或跨页续表区域。
2. 对每个对象查找其上方、下方或相邻文档文本中是否可见“图/表+编号+标题文字”。
3. 章节标题、页眉、文档名、页码、版权信息、正文段落、步骤说明、红框标注或空白占位不能直接替代图表标题。
4. 如果对象附近只有章节标题（如“7.4.1 App 开站”）但没有“图x-x/表x-x 标题”，列为“疑似标题缺失”；请明确写出“章节标题不能替代图表标题”。
5. 表格位于页眉/文档名/页码下方，紧接着就是表格边框和表头，但未见“表x-x 标题”或“续表x-x 标题”时，列为“疑似表标题缺失”；请明确写出“页眉或文档名不能替代表标题”。
6. 跨页续表、截图裁切不完整、标题可能在上一页/下一页或 OCR 看不清时，标注“需人工确认”，不要直接判为正常。
7. 如果标题只有编号没有标题文字，或只有标题文字没有“图/表+编号”，列为“标题不完整”或“需人工确认”。

输出要求：
1. 先给出总体判断，说明是否发现疑似图表标题缺失、标题不完整或需要人工确认的对象。
2. 按条列出：图片名称或位置、对象类型（图/表/截图/流程图等）、可见内容线索、附近可见标题文字、问题判断、建议补充的标题形式。
3. 对证据不足的问题单独列入“需人工确认”。
4. 如果未发现明显问题，明确说明“未发现明显图表标题可见性问题”。不要编造图片或文档中不存在的标题。""",
        "sort_order": 30,
    },
    {
        "code": "image-integrity-clarity",
        "name": "图片完整性和清晰度检查",
        "description": "检查图片是否裁切、遮挡、坏图、模糊、低分辨率、拉伸变形，关键文字或线条是否可读。",
        "prompt": """你是一名技术文档图片质量审查专家。请结合文档上下文、图片位置和本次提供的图片内容，检查图片的完整性和清晰度是否满足用户手册、安装指南、调测指南的发布要求。
完整性重点关注：
1. 图片只显示一部分，主体被裁切，边缘内容缺失，或明显超出页面/截图边界。
2. 图片被文字、图形、浮层、页眉页脚、遮罩或其他对象遮挡、覆盖。
3. 图片出现异常红块、白块、黑块、灰块、马赛克块、空白块、坏图占位、渲染失败区域或颜色异常色块。
4. 图片内容被错误叠加、重影、错位，导致主体不可辨认或信息缺失。
5. 表格、流程图、接线图或界面截图缺少关键行列、步骤、箭头、端子、按钮、字段或结果区域。

清晰度重点关注：
1. 图片模糊、失焦、文字/线条不可读。
2. 分辨率过低，放大后锯齿明显，关键标注、端子号、菜单名、按钮名、字段值、单位或图例无法辨认。
3. 过度拉伸、压缩、比例变形，设备外观、图形元素或文字形状明显失真。
4. 压缩痕迹、噪点、色带、块状失真严重影响阅读。

输出要求：
1. 先给出总体判断，说明是否发现图片完整性或清晰度风险。
2. 按条列出问题：图片名称或位置、问题类型（完整性/清晰度）、可见线索、影响、建议处理方式。
3. 对图片本身分辨率不足、无法判断是否由截图造成、或需要原始文件核对的情况标注“需人工确认”。
4. 如果未发现明显问题，明确说明“未发现明显图片完整性和清晰度问题”。不要编造图片中不存在的缺陷。""",
        "sort_order": 35,
    },
    {
        "code": "image-drawing-standard",
        "name": "图片画图规范检查",
        "description": "检查流程图、结构图、示意图、接线图和截图标注是否存在明显表达不清、方向错误、编号混乱或图例缺失。",
        "prompt": """你是一名技术图示表达规范审查专家。请结合文档文本、图片位置和本次提供的图片内容，检查流程图、结构图、示意图、接线图、安装示意图或截图标注是否存在明显表达问题。
重点关注：
1. 流程箭头方向、步骤编号、分支关系、输入输出、开始/结束节点是否清楚，是否存在明显断线、反向、遗漏或闭环不明。
2. 结构图、安装图、接线图中的方向、比例、层级、对齐、标注引线、符号图例、单位、编号是否容易误读。
3. 同一图片内的图例、颜色、编号、端口、参数单位是否前后不一致，或与文档文字明显冲突。
4. 截图上的红框、箭头、圈注、序号是否准确指向操作对象，是否遮挡关键文字。
5. 行业标准或企业制图规范无法从图片直接判断时，标注“需人工确认”，不要泛泛套用外部标准。

输出要求：
1. 先概括图片是否存在明显画图规范风险。
2. 按条列出：图片名称或位置、文档线索、问题描述、影响、修改建议。
3. 对行业标准或业务口径不明确的问题标注“需人工确认”。
4. 如果未发现明显问题，明确说明“未发现明显画图规范问题”。""",
        "sort_order": 40,
    },
)

DEFAULT_CHECK_ITEMS = tuple(
    {**item, "task_type": DOCUMENT_TASK_TYPE}
    for item in DEFAULT_DOCUMENT_CHECK_ITEMS
) + tuple(
    {**item, "task_type": CONSISTENCY_TASK_TYPE}
    for item in DEFAULT_CONSISTENCY_CHECK_ITEMS
) + tuple(
    {**item, "task_type": LANGUAGE_CONSISTENCY_TASK_TYPE}
    for item in DEFAULT_LANGUAGE_CONSISTENCY_CHECK_ITEMS
) + tuple(
    {**item, "task_type": VIDEO_TASK_TYPE}
    for item in DEFAULT_VIDEO_CHECK_ITEMS
) + tuple(
    {**item, "task_type": IMAGE_TASK_TYPE}
    for item in DEFAULT_IMAGE_CHECK_ITEMS
)
DEFAULT_CHECK_ITEMS_BY_CODE = {item["code"]: item for item in DEFAULT_CHECK_ITEMS}
_IMAGE_LANGUAGE_MATCH_CODE = "image-small-language-text"
_REMOVED_DEFAULT_CHECK_ITEM_CODES = ("consistency-translation-coverage",)
_TYPO_LOCATION_PROMPT_MARKERS = (
    "按条列出：原文片段、疑似问题、建议修改、理由",
    "未发现明显错别字或语病",
)
_COMPLIANCE_PROMPT_MARKERS = (
    "标题层级、章节结构、编号、术语、格式表达、引用说明",
    "先给出总体结论",
    "未发现明显规范性问题",
)
_CONSISTENCY_PROMPT_MARKERS = (
    "包括但不限于人名/组织名、项目名、日期、金额、数量、单位",
    "先概括一致性风险等级",
    "建议统一口径",
)
_LANGUAGE_CONSISTENCY_PROMPT_MARKERS = (
    "最终报告必须使用中文陈述",
    "静态预检摘要只作为优先核对线索",
    "单独概括“缺失内容”和“关键事实/数字差异”",
)
_LEGACY_IMAGE_LANGUAGE_MARKERS = ("小语种", "非中文、非英文")
_QWEN_VL_OPTIMIZED_IMAGE_PROMPT_MARKERS = {
    "image-text-correspondence": ("图文一致性审查专家",),
    "image-wiring": ("电气接线图和设备接线审查专家",),
    "image-figure-table-title-standard": ("必须判为表标题缺失", "同一张图片中可能同时出现"),
    "image-integrity-clarity": ("异常红块", "过度拉伸"),
    "image-drawing-standard": ("技术制图和图示规范审查专家", "线型线宽"),
}
_MERGED_IMAGE_CHECK_ITEM_MARKERS = {
    "image-ui-step-consistency": {
        "name": "界面截图与步骤一致性检查",
        "markers": ("产品界面截图与操作步骤审查专家",),
    },
    "image-device-installation": {
        "name": "设备外观与安装图检查",
        "markers": ("产品设备外观与安装图审查专家",),
    },
}


def default_check_item_codes(task_type: str | None = None) -> set[str]:
    return {
        item["code"]
        for item in DEFAULT_CHECK_ITEMS
        if task_type is None or item["task_type"] == task_type
    }


def reset_default_check_item_prompt(item_id: int) -> bool:
    db = get_db()
    row = db.execute("SELECT code FROM check_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return False
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get(row["code"])
    if default_item is None:
        return False
    db.execute(
        "UPDATE check_items SET prompt = ?, updated_at = ? WHERE id = ?",
        (default_item["prompt"], now_text(), item_id),
    )
    db.commit()
    return True


def seed_defaults():
    db = get_db()
    now = now_text()

    defaults = {
        "global_concurrency": 3,
        "user_concurrency": 1,
        "check_item_concurrency": 1,
        "image_check_batch_size": 4,
        "image_page_check_max_pages": 120,
        "issue_output_limit": 20,
        "report_retention_days": 0,
        "llm_stream_trace_enabled": False,
    }
    for key, value in defaults.items():
        exists = db.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
        if exists is None:
            db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    for item in DEFAULT_CHECK_ITEMS:
        exists = db.execute("SELECT 1 FROM check_items WHERE code = ?", (item["code"],)).fetchone()
        if exists is None:
            db.execute(
                """
                INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    item["task_type"],
                    item["code"],
                    item["name"],
                    item["description"],
                    item["prompt"],
                    item["sort_order"],
                    now,
                    now,
                ),
            )

    _sync_renamed_default_check_items(db, now)
    _sync_compliance_prompt(db, now)
    _sync_typo_location_prompt(db, now)
    _sync_consistency_prompt(db, now)
    _sync_language_consistency_prompt(db, now)
    _sync_qwen_vl_optimized_image_check_items(db, now)
    _disable_merged_image_check_items(db, now)
    _remove_retired_default_check_items(db)
    db.commit()


def _remove_retired_default_check_items(db):
    for code in _REMOVED_DEFAULT_CHECK_ITEM_CODES:
        db.execute("DELETE FROM check_items WHERE code = ?", (code,))


def _sync_compliance_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("compliance")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'compliance'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_legacy_stock_prompt = (
        all(marker in prompt for marker in _COMPLIANCE_PROMPT_MARKERS)
        and "客户资料规范审查专家" not in prompt
    )
    if not is_legacy_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'compliance'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_typo_location_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("typo")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'typo'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_legacy_stock_prompt = (
        all(marker in prompt for marker in _TYPO_LOCATION_PROMPT_MARKERS)
        and "页码：未提取" not in prompt
    )
    if not is_legacy_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'typo'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_consistency_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("consistency")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'consistency'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_legacy_stock_prompt = (
        all(marker in prompt for marker in _CONSISTENCY_PROMPT_MARKERS)
        and "约束性与安全信息一致性" not in prompt
    )
    if not is_legacy_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'consistency'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_language_consistency_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("language-consistency-cross-lingual")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'language-consistency-cross-lingual'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_legacy_stock_prompt = (
        all(marker in prompt for marker in _LANGUAGE_CONSISTENCY_PROMPT_MARKERS)
        and "无实质影响" not in prompt
        and "无需修改" not in prompt
    )
    if not is_legacy_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'language-consistency-cross-lingual'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_renamed_default_check_items(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get(_IMAGE_LANGUAGE_MATCH_CODE)
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = ?",
        (_IMAGE_LANGUAGE_MATCH_CODE,),
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    should_update_prompt = any(marker in prompt for marker in _LEGACY_IMAGE_LANGUAGE_MARKERS)
    next_prompt = default_item["prompt"] if should_update_prompt else prompt
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and next_prompt == prompt
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = ?
        """,
        (
            default_item["name"],
            default_item["description"],
            next_prompt,
            default_item["sort_order"],
            updated_at,
            _IMAGE_LANGUAGE_MATCH_CODE,
        ),
    )


def _sync_qwen_vl_optimized_image_check_items(db, updated_at: str):
    for code, markers in _QWEN_VL_OPTIMIZED_IMAGE_PROMPT_MARKERS.items():
        default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get(code)
        if default_item is None:
            continue
        row = db.execute(
            "SELECT name, description, prompt, sort_order FROM check_items WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            continue
        prompt = str(row["prompt"] or "")
        is_stock_prompt = prompt == default_item["prompt"] or all(marker in prompt for marker in markers)
        if not is_stock_prompt:
            continue
        if (
            row["name"] == default_item["name"]
            and (row["description"] or "") == default_item["description"]
            and prompt == default_item["prompt"]
            and int(row["sort_order"] or 0) == int(default_item["sort_order"])
        ):
            continue
        db.execute(
            """
            UPDATE check_items
            SET name = ?,
                description = ?,
                prompt = ?,
                sort_order = ?,
                updated_at = ?
            WHERE code = ?
            """,
            (
                default_item["name"],
                default_item["description"],
                default_item["prompt"],
                default_item["sort_order"],
                updated_at,
                code,
            ),
        )


def _disable_merged_image_check_items(db, updated_at: str):
    for code, legacy in _MERGED_IMAGE_CHECK_ITEM_MARKERS.items():
        row = db.execute(
            "SELECT name, prompt, enabled FROM check_items WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            continue
        prompt = str(row["prompt"] or "")
        is_stock_item = all(marker in prompt for marker in legacy["markers"])
        if not is_stock_item or not int(row["enabled"] or 0):
            continue
        db.execute(
            "UPDATE check_items SET enabled = 0, updated_at = ? WHERE code = ?",
            (updated_at, code),
        )
