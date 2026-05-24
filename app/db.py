import json
import sqlite3
from datetime import datetime

from flask import current_app, g

from .task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE


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

DEFAULT_CHECK_ITEMS = tuple(
    {**item, "task_type": DOCUMENT_TASK_TYPE}
    for item in DEFAULT_DOCUMENT_CHECK_ITEMS
) + tuple(
    {**item, "task_type": CONSISTENCY_TASK_TYPE}
    for item in DEFAULT_CONSISTENCY_CHECK_ITEMS
)
DEFAULT_CHECK_ITEMS_BY_CODE = {item["code"]: item for item in DEFAULT_CHECK_ITEMS}


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

    db.commit()
