import json

from app.persistence.connection import get_db, now_text


def delete_task_record(db, task_id: int):
    db.execute("DELETE FROM report_suppression_hits WHERE task_id = ?", (task_id,))
    return db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


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
    row = (
        get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    )
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
    row = (
        get_db()
        .execute("SELECT username FROM ip_usernames WHERE ip = ?", (ip,))
        .fetchone()
    )
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
