import json
import sqlite3
from datetime import datetime

from flask import current_app, g


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

        CREATE TABLE IF NOT EXISTS ip_users (
            ip TEXT PRIMARY KEY,
            username TEXT,
            is_disabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS check_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            ip TEXT NOT NULL,
            username_snapshot TEXT,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            checks_json TEXT NOT NULL,
            provider_name TEXT,
            model_name TEXT NOT NULL,
            api_base TEXT NOT NULL,
            api_key TEXT,
            proxy_mode TEXT NOT NULL DEFAULT 'direct',
            proxy TEXT,
            request_timeout INTEGER NOT NULL DEFAULT 3600,
            max_input_chars INTEGER NOT NULL DEFAULT 60000,
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

        CREATE INDEX IF NOT EXISTS idx_tasks_ip_created ON tasks(ip, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        """
    )
    current_app.teardown_appcontext(close_db)
    db.commit()


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


DEFAULT_CHECK_ITEMS = (
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
DEFAULT_CHECK_ITEMS_BY_CODE = {item["code"]: item for item in DEFAULT_CHECK_ITEMS}


def default_check_item_codes() -> set[str]:
    return set(DEFAULT_CHECK_ITEMS_BY_CODE)


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
                INSERT INTO check_items(code, name, description, prompt, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
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
