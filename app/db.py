import json
import sqlite3
from datetime import datetime

from flask import current_app, g

from .task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE, IMAGE_TASK_TYPE


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
        "prompt": """你是一名严谨的文档规范审查专家。请检查文档的标题层级、章节结构、编号、术语、格式表达、引用说明、表格/图片说明、落款与附件等规范性问题。
输出要求：
1. 先给出总体结论，说明是否存在明显规范风险。
2. 按问题逐条列出：位置线索、问题描述、影响、修改建议。
3. 如果未发现问题，明确说明“未发现明显规范性问题”。
4. 不要编造文档中不存在的内容。""",
        "sort_order": 10,
    },
    {
        "code": "consistency",
        "name": "全文一致性检查",
        "description": "检查全文内时间、数字、名称、术语、口径和前后表述是否一致。",
        "prompt": """你是一名全文一致性审查专家。请检查文档内部是否存在前后矛盾或口径不一致，包括但不限于人名/组织名、项目名、日期、金额、数量、单位、缩写、术语定义、章节引用、结论与正文依据。
输出要求：
1. 先概括一致性风险等级。
2. 按条列出不一致内容：涉及位置线索、冲突表述、判断依据、建议统一口径。
3. 对不确定的问题标注“需人工确认”，不要武断下结论。""",
        "sort_order": 20,
    },
    {
        "code": "typo",
        "name": "错别字检查",
        "description": "检查错别字、漏字、多字、标点和常见语病。",
        "prompt": """你是一名中文校对专家。请检查文档中的错别字、漏字、多字、标点误用、重复表达、常见语病和明显不通顺句子。
输出要求：
1. 按条列出：原文片段、疑似问题、建议修改、理由。
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

DEFAULT_IMAGE_CHECK_ITEMS = (
    {
        "code": "image-text-correspondence",
        "name": "图文对应检查",
        "description": "综合文档文本、图题图号、参数描述和图片内容，检查图文是否匹配。",
        "prompt": """你是一名图文一致性审查专家。请综合文档文本、图片清单、图片位置和本次提供的图片内容，检查图片是否与文档中的文字描述、图题、图号、表格参数、步骤说明或引用关系一致。
重点关注：
1. 文档文字描述的对象、场景、设备、接线、流程、参数、图号或标题是否与图片可见内容对应。
2. 图片中的文字、编号、参数、方向、状态或图例是否与文档文本冲突。
3. 文档提到的图或图片未体现关键内容，或图片内容在文档中缺少必要说明。
4. 同一批图片之间如存在编号、标题、内容顺序或引用关系冲突，也请标注。

输出要求：
1. 先给出总体判断，说明是否发现图文对应风险。
2. 按条列出问题：图片名称或位置、文档文字线索、图片可见内容、冲突或缺失说明、建议修改。
3. 对证据不足、图片不清晰或需要业务判断的问题标注“需人工确认”。
4. 只有同时看到明确文档线索和图片可见证据时，才判断为“不对应”。
5. 不要仅凭文件名、页码、图片顺序或未提供的上下文推断图片插入错位；证据不足时写“需人工确认”。
6. 如果未发现明显问题，明确说明“未发现明显图文对应问题”。不要编造文档或图片中不存在的内容。""",
        "sort_order": 10,
    },
    {
        "code": "image-small-language-text",
        "name": "图片语种匹配检查",
        "description": "检查图片中的文字语种是否与文档主要语种一致，例如英文文档图片中出现中文说明。",
        "prompt": """你是一名图片文字语种一致性审查专家。请先根据提供的文档上下文判断文档主要语种（如中文、英文、中英混排或其他语种），再检查本次图片中可见文字、标注、截图界面、图例和说明的语种是否与文档主要语种一致。
重点关注：
1. 英文文档中图片出现中文说明、中文界面、中文标注等明显不匹配内容。
2. 中文文档中图片出现大段英文或其他语种说明，且文档上下文没有对应语种使用习惯。
3. 多语种文档中，图片文字语种是否超出文档正文、标题或图注使用的语种范围。
4. 不要把产品名、型号、单位、接口名、标准缩写、命令、URL、代码片段等技术性英文/符号直接判为异常，除非出现大段说明文字语种明显不匹配。

输出要求：
1. 先说明文档主要语种，以及是否发现图片文字语种不匹配。
2. 如发现，逐条列出：图片名称或位置、图片中识别到的文字、图片文字语种、文档主要语种、不匹配原因、建议处理方式。
3. 对看不清、文字过少或无法判断文档主要语种的内容标注“需人工确认”。
4. 如果未发现明显不匹配，明确说明“未发现图片文字语种与文档语种明显不一致”。不要编造图片或文档中不存在的文字。""",
        "sort_order": 20,
    },
    {
        "code": "image-wiring",
        "name": "图片接线问题检查",
        "description": "检查图片中的接线、端子、线缆走向、标识和连接关系是否存在明显问题。",
        "prompt": """你是一名电气接线图和设备接线审查专家。请结合文档文本、图片位置和本次提供的图片内容，检查接线关系、端子编号、线缆走向、极性、颜色/线号标识、连接点和交叉连接是否存在明显风险；如文档中的接线描述、表格或步骤与图片不一致，也请指出。
输出要求：
1. 先给出总体判断，说明是否发现明显接线风险。
2. 按条列出问题：图片名称或位置、文档线索、问题描述、可能影响、建议修改或需核对的依据。
3. 对图片分辨率不足、遮挡或无法确认的地方标注“需人工确认”。
4. 只依据提供的文本和图片可见内容，不要补全不可见接线。""",
        "sort_order": 30,
    },
    {
        "code": "image-figure-table-title-standard",
        "name": "图表标题规范检查",
        "description": "检查图片或页面截图中的图、表是否缺少“图x-x 标题”“表x-x 标题”等规范标题。",
        "prompt": """你是一名技术文档图表标题规范审查专家。请结合文档上下文、图片位置和本次提供的图片内容，检查图、表、页面截图中的图示或表格是否具备规范标题。
正确形式示例：
1. 图标题：类似“图3-2 iIOT-WEC04C5网关外观（02314WHE）”，通常包含“图”+章节编号/序号+标题文字。
2. 表标题：类似“表3-1 IoT网关型号介绍”，通常包含“表”+章节编号/序号+标题文字。

重点关注：
1. 图片中只有一张设备图、结构图、示意图、流程图、截图或外观图，但图片附近或文档上下文未看到规范图标题。
2. 图片中只有一个表格，或表格截图/表格区域占主体，但图片附近或文档上下文未看到规范表标题。
3. 表格上方只有章节标题（如“7.4.1 App 开站”“7.4.2 设置站点参数”）但没有“表x-x 标题”时，必须判为表标题缺失；章节标题不能替代表标题。
4. 图示上方只有章节标题或正文说明，但没有“图x-x 标题”时，必须判为图标题缺失；章节标题不能替代图标题。
5. 同一张图片中可能同时出现一个缺少表标题的表格和另一个有表标题的表格，请分别判断，不要因为图片中存在一个正确标题就忽略另一个缺失标题。
6. 表格出现在新页开头、页眉/文档名/页码/版权信息下方，紧接着就是表格边框和表头，但中间没有“表x-x 标题”或“续表x-x 标题”时，必须判为表标题缺失。
7. 页眉文字、文档名称、页码、版权信息、章节标题、正文段落、步骤说明、空白占位或红框标注，都不能替代表标题。
8. 如果是跨页续表，当前页表格上方也应有“续表x-x”或可明确对应上一页表标题的续表说明；看不到时至少标注“需人工确认”，不能直接判为正常。
9. 标题只有编号没有标题文字，或只有标题文字没有“图/表+编号”，也视为标题不完整。
10. 如果图表标题在图片外部但已出现在提供的相邻文档文本中，并且能明确对应当前图或表，不要判为缺失。
11. 对无法判断标题是否属于当前图片/表格、图片截取不完整或上下文不足的情况，标注“需人工确认”。

输出要求：
1. 先给出总体判断，说明是否发现图表标题缺失或不完整。
2. 按条列出问题：图片名称或位置、对象类型（图/表）、可见内容线索、缺失或不完整说明、建议补充的标题形式。
3. 当表格位于章节标题下方但缺少表标题时，请明确写出“章节标题不能替代表标题”。
4. 当表格位于页眉/文档名/页码下方但缺少表标题时，请明确写出“页眉或文档名不能替代表标题”。
5. 对证据不足的问题单独标注“需人工确认”。
6. 如果未发现明显问题，明确说明“未发现明显图表标题规范问题”。不要编造图片或文档中不存在的标题。""",
        "sort_order": 35,
    },
    {
        "code": "image-integrity-clarity",
        "name": "图片完整性和清晰度检查",
        "description": "检查图片是否裁切不完整、超出页面、被遮挡覆盖、出现异常色块，或存在模糊、低分辨率、拉伸变形、压缩失真等问题。",
        "prompt": """你是一名技术文档图片质量审查专家。请结合文档上下文、图片位置和本次提供的图片内容，检查图片的完整性和清晰度是否满足文档发布要求。
完整性重点关注：
1. 图片只显示一部分，主体被裁切，边缘内容缺失，或明显超出页面/截图边界。
2. 图片被文字、图形、浮层、页眉页脚、遮罩或其他对象遮挡、覆盖。
3. 图片出现异常红块、白块、黑块、灰块、马赛克块、空白块、坏图占位、渲染失败区域或颜色异常色块。
4. 图片内容被错误叠加、重影、错位，导致主体不可辨认或信息缺失。

清晰度重点关注：
1. 图片模糊、失焦、文字/线条不可读。
2. 分辨率过低，放大后锯齿明显，关键标注无法辨认。
3. 过度拉伸、压缩、比例变形，设备外观、图形元素或文字形状明显失真。
4. 压缩痕迹、噪点、色带、块状失真严重影响阅读。

输出要求：
1. 先给出总体判断，说明是否发现图片完整性或清晰度风险。
2. 按条列出问题：图片名称或位置、问题类型（完整性/清晰度）、可见线索、影响、建议处理方式。
3. 对图片本身分辨率不足、无法判断是否由截图造成、或需要原始文件核对的情况标注“需人工确认”。
4. 如果未发现明显问题，明确说明“未发现明显图片完整性和清晰度问题”。不要编造图片中不存在的缺陷。""",
        "sort_order": 38,
    },
    {
        "code": "image-drawing-standard",
        "name": "图片画图规范检查",
        "description": "检查图片中的图纸、流程图、示意图是否符合常见画图规范和表达规范。",
        "prompt": """你是一名技术制图和图示规范审查专家。请结合文档文本、图片位置和本次提供的图片内容，检查图纸、流程图、结构图、示意图或截图是否存在画图规范问题，包括但不限于标题/图号、比例与方向、线型线宽、符号图例、标注单位、编号、对齐、层级、可读性和说明完整性；如图题、图号或文字说明与图片不匹配，也请指出。
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
    {**item, "task_type": IMAGE_TASK_TYPE}
    for item in DEFAULT_IMAGE_CHECK_ITEMS
)
DEFAULT_CHECK_ITEMS_BY_CODE = {item["code"]: item for item in DEFAULT_CHECK_ITEMS}
_IMAGE_LANGUAGE_MATCH_CODE = "image-small-language-text"
_LEGACY_IMAGE_LANGUAGE_MARKERS = ("小语种", "非中文、非英文")


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
    db.commit()


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
