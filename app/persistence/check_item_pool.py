"""单文档检查项池、方案引用与历史检查项标识。"""

import json

from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.connection import get_db, now_text

POOL_MIGRATION_KEY = "document_check_item_pool_v1"
LOCAL_CHECK_CODES = {"sensitive-terms", "common-terms", "hyperlink-validity"}


def initialize_check_item_pool_schema(db):
    columns = {row["name"] for row in db.execute("PRAGMA table_info(check_items)")}
    for name, definition in (
        ("pool_item_id", "INTEGER"),
        ("template_code", "TEXT NOT NULL DEFAULT ''"),
        ("check_method", "TEXT NOT NULL DEFAULT 'prompt'"),
        ("skill_package_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE check_items ADD COLUMN {name} {definition}")
    db.execute("""CREATE TABLE IF NOT EXISTS check_profile_items (
        profile_id TEXT NOT NULL, check_item_id INTEGER NOT NULL,
        PRIMARY KEY(profile_id, check_item_id)
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_profile_items_item ON check_profile_items(check_item_id)"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS check_item_aliases (
        profile_id TEXT NOT NULL, code TEXT NOT NULL, canonical_code TEXT NOT NULL,
        check_item_id INTEGER NOT NULL, PRIMARY KEY(profile_id, code)
    )""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS trg_check_item_pool_identity
        AFTER INSERT ON check_items WHEN NEW.pool_item_id IS NULL BEGIN
            UPDATE check_items SET pool_item_id = NEW.id WHERE id = NEW.id;
        END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS trg_check_item_pool_delete
        AFTER DELETE ON check_items BEGIN
            DELETE FROM check_profile_items WHERE check_item_id = OLD.id;
        END""")


def seed_check_item_pool(db):
    if db.execute(
        "SELECT 1 FROM settings WHERE key = ?", (POOL_MIGRATION_KEY,)
    ).fetchone():
        return
    rows = db.execute(
        """SELECT c.*, COALESCE(p.template_id, 'default') AS template_id,
            COALESCE(p.name, c.profile_id) AS profile_name
        FROM check_items c LEFT JOIN check_profiles p ON p.id = c.profile_id
        WHERE c.task_type = ? ORDER BY c.profile_id != 'default', c.id""",
        (DOCUMENT_TASK_TYPE,),
    ).fetchall()
    canonical = {}
    for item in rows:
        template = (
            "default" if item["code"] in LOCAL_CHECK_CODES else item["template_id"]
        )
        signature = tuple(
            item[key] or ""
            for key in ("code", "name", "description", "prompt", "enabled")
        ) + (template,)
        target = canonical.get(signature)
        if target is None:
            code = (
                item["code"]
                if item["profile_id"] == "default"
                else f"pool-{item['id']}"
            )
            target = (item["id"], code)
            canonical[signature] = target
            name = (
                item["name"]
                if item["profile_id"] == "default"
                else f"{item['profile_name']} · {item['name']}"
            )
            db.execute(
                "UPDATE check_items SET pool_item_id = id, template_code = code, code = ?, name = ? WHERE id = ?",
                (code, name, item["id"]),
            )
        else:
            db.execute(
                "UPDATE check_items SET pool_item_id = ?, template_code = code WHERE id = ?",
                (target[0], item["id"]),
            )
        db.execute(
            "INSERT INTO check_item_aliases(profile_id, code, canonical_code, check_item_id) VALUES (?, ?, ?, ?)",
            (item["profile_id"], item["code"], target[1], target[0]),
        )
        if item["default_selected"]:
            db.execute(
                "INSERT OR IGNORE INTO check_profile_items(profile_id, check_item_id) VALUES (?, ?)",
                (item["profile_id"], target[0]),
            )
    db.execute(
        """UPDATE report_suppression_rules AS r SET check_code = (
            SELECT canonical_code FROM check_item_aliases a WHERE a.profile_id = r.profile_id AND a.code = r.check_code
        ) WHERE r.task_type = ? AND EXISTS (
            SELECT 1 FROM check_item_aliases a WHERE a.profile_id = r.profile_id AND a.code = r.check_code
        )""",
        (DOCUMENT_TASK_TYPE,),
    )
    db.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, 'true', ?)",
        (POOL_MIGRATION_KEY, now_text()),
    )


def document_check_pool(db=None, *, enabled_only=False):
    db = db if db is not None else get_db()
    return db.execute(
        "SELECT * FROM check_items WHERE task_type = ? AND pool_item_id = id "
        + ("AND enabled = 1 " if enabled_only else "")
        + "ORDER BY sort_order, id",
        (DOCUMENT_TASK_TYPE,),
    ).fetchall()


def profile_check_ids(profile_id):
    return {
        row[0]
        for row in get_db().execute(
            "SELECT check_item_id FROM check_profile_items WHERE profile_id = ?",
            (profile_id,),
        )
    }


def save_profile_check_items(profile_id, values):
    db = get_db()
    try:
        ids = {int(value) for value in values}
    except (TypeError, ValueError) as exc:
        raise ValueError("请选择检查项池中的检查项。") from exc
    valid = {row["id"] for row in document_check_pool(db)}
    if not ids <= valid:
        raise ValueError("包含无效或已删除的检查项，请刷新页面后重试。")
    if ids == profile_check_ids(profile_id):
        return
    db.execute("DELETE FROM check_profile_items WHERE profile_id = ?", (profile_id,))
    db.executemany(
        "INSERT INTO check_profile_items(profile_id, check_item_id) VALUES (?, ?)",
        [(profile_id, item_id) for item_id in sorted(ids)],
    )
    db.execute(
        "UPDATE check_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
        (now_text(), profile_id),
    )


def task_check_code_aliases(task, *, cache=None):
    values = dict(task)
    if values.get("task_type", DOCUMENT_TASK_TYPE) != DOCUMENT_TASK_TYPE:
        return {}
    profile_id = values.get("check_profile_id") or "default"
    if profile_id == "default":
        return {}
    try:
        snapshot = json.loads(values.get("check_profile_snapshot_json") or "{}")
    except (ValueError, TypeError):
        snapshot = {}
    if isinstance(snapshot, dict) and snapshot.get("checks_format") == 2:
        return {}
    if cache is not None and profile_id in cache:
        return cache[profile_id]
    aliases = {
        row["code"]: row["canonical_code"]
        for row in get_db().execute(
            "SELECT code, canonical_code FROM check_item_aliases WHERE profile_id = ?",
            (profile_id,),
        )
    }
    if cache is not None:
        cache[profile_id] = aliases
    return aliases
