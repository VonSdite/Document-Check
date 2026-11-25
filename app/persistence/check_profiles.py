"""检查方案初始化、版本与管理。"""

import uuid

from app.contracts.check_profiles import DEFAULT_CHECK_PROFILE
from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.connection import get_db, now_text


def initialize_check_profiles(db):
    db.execute("""CREATE TABLE IF NOT EXISTS check_profiles (
        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '',
        template_id TEXT NOT NULL DEFAULT 'default',
        enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(check_profiles)")}
    if "template_id" not in columns:
        db.execute(
            "ALTER TABLE check_profiles ADD COLUMN template_id TEXT NOT NULL DEFAULT 'default'"
        )
    now = now_text()
    for profile_id, name, description in (
        ("default", "现有文档方案", "现有技术文档检查项与规则。"),
        (
            "software",
            "软件文档方案",
            "面向软件操作、配置、部署和接口资料，按文档用途检查。",
        ),
    ):
        db.execute(
            "INSERT OR IGNORE INTO check_profiles(id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (profile_id, name, description, now, now),
        )
    db.execute(
        "UPDATE check_profiles SET template_id = 'software' WHERE id = 'software'"
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(check_items)")}
    if "profile_id" not in columns:
        sequence = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'check_items'"
        ).fetchone()
        db.execute("""CREATE TABLE check_items_scoped (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL DEFAULT 'default',
            task_type TEXT NOT NULL DEFAULT 'document_check', code TEXT NOT NULL,
            name TEXT NOT NULL, description TEXT, prompt TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, default_selected INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id, code)
        )""")
        fields = "id, task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at"
        db.execute(
            f"INSERT INTO check_items_scoped({fields}) SELECT {fields} FROM check_items"
        )
        db.execute("DROP TABLE check_items")
        db.execute("ALTER TABLE check_items_scoped RENAME TO check_items")
        if sequence:
            cursor = db.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'check_items'",
                (sequence[0],),
            )
            if not cursor.rowcount:
                db.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES ('check_items', ?)",
                    (sequence[0],),
                )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_check_items_profile_type ON check_items(profile_id, task_type, enabled, sort_order)"
    )
    for operation, record in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        trigger = f"trg_check_profile_{operation.lower()}"
        sql = f"""CREATE TRIGGER {trigger}
            AFTER {operation} ON check_items
            WHEN {record}.task_type = '{DOCUMENT_TASK_TYPE}' BEGIN
                UPDATE check_profiles SET revision = revision + 1, updated_at = {record}.updated_at
                WHERE id = {record}.profile_id;
            END"""
        existing = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger,),
        ).fetchone()
        if existing is None or existing["sql"] != sql:
            db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            db.execute(sql)


def check_profiles(*, enabled_only=False):
    return [
        dict(row)
        for row in get_db().execute(
            "SELECT * FROM check_profiles "
            + ("WHERE enabled = 1 " if enabled_only else "")
            + "ORDER BY CASE id WHEN 'default' THEN 0 WHEN 'software' THEN 1 ELSE 2 END, created_at, id"
        )
    ]


def save_check_profile(profile_id, name, description, enabled):
    name = " ".join(str(name).split())
    if not name or len(name) > 60 or len(description) > 500:
        raise ValueError("方案名称需为 1～60 个字符，说明最多 500 个字符。")
    db = get_db()
    if db.execute(
        "SELECT 1 FROM check_profiles WHERE name = ? AND id != ?", (name, profile_id)
    ).fetchone():
        raise ValueError("方案名称已存在。")
    if not db.execute(
        "SELECT 1 FROM check_profiles WHERE id = ?", (profile_id,)
    ).fetchone():
        raise ValueError("检查方案不存在。")
    db.execute(
        "UPDATE check_profiles SET name = ?, description = ?, enabled = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
        (name, description, int(enabled), now_text(), profile_id),
    )


def copy_check_profile(source_id, name):
    db = get_db()
    source = db.execute(
        "SELECT * FROM check_profiles WHERE id = ?", (source_id,)
    ).fetchone()
    if source is None:
        raise ValueError("源方案不存在。")
    profile_id = uuid.uuid4().hex
    now = now_text()
    name = " ".join(str(name).split())
    if not name or len(name) > 60:
        raise ValueError("方案名称需为 1～60 个字符。")
    if db.execute("SELECT 1 FROM check_profiles WHERE name = ?", (name,)).fetchone():
        raise ValueError("方案名称已存在。")
    db.execute(
        "INSERT INTO check_profiles(id, name, description, template_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (profile_id, name, source["description"], source["template_id"], now, now),
    )
    db.execute(
        """INSERT INTO check_items(profile_id, task_type, code, name, description, prompt, enabled, default_selected, sort_order, created_at, updated_at)
        SELECT ?, task_type, code, name, description, prompt, enabled, default_selected, sort_order, ?, ?
        FROM check_items WHERE profile_id = ? AND task_type = ?""",
        (profile_id, now, now, source_id, DOCUMENT_TASK_TYPE),
    )
    return profile_id


def available_check_profiles(task_type):
    if task_type != DOCUMENT_TASK_TYPE:
        return []
    db = get_db()
    profiles = check_profiles(enabled_only=True)
    items = [
        dict(row)
        for row in db.execute(
            "SELECT id, profile_id, name, description, default_selected FROM check_items WHERE task_type = ? AND enabled = 1 ORDER BY sort_order, id",
            (task_type,),
        )
    ]
    return [
        {
            **profile,
            "checks": [item for item in items if item["profile_id"] == profile["id"]],
        }
        for profile in profiles
        if any(item["profile_id"] == profile["id"] for item in items)
    ]


def check_profile_snapshot(checks):
    return checks[0].get("profile", {"id": DEFAULT_CHECK_PROFILE})
