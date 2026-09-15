import json

from app.persistence.connection import get_db, now_text


def delete_task_record(db, task_id: int):
    db.execute("DELETE FROM settings WHERE key = ?", (f"task_activity:{task_id}",))
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


def sync_identity_profile(subject: str, profile: dict) -> dict:
    """按上游查询开始时间保存最新资料，旧缓存读取同一份资料。"""
    key = f"identity_profile:{subject}"
    current = get_setting(key)
    if current is not None and current["version"] >= profile["version"]:
        return current
    db = get_db()
    db.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        WHERE json_extract(excluded.value, '$.version') > json_extract(settings.value, '$.version')
        """,
        (key, json.dumps(profile, ensure_ascii=False), now_text()),
    )
    db.commit()
    return get_setting(key)


def owner_subject_from_ip(ip: str) -> str:
    return f"ip:{str(ip or '0.0.0.0').strip() or '0.0.0.0'}"


def migrate_ip_owner_to_subject(
    ip: str, new_subject: str, source: str = "cookie_session"
) -> bool:
    """把某 IP 下 ip:<IP> 归属的任务和模型配置迁移到新的稳定 owner_subject。

    用于 cookie_session 模式登录时懒迁移：把当前 IP 的历史 ip:<IP> 数据
    归属到当前登录用户的稳定 subject（cookie_session:<稳定标识>）。
    无历史数据时为空操作，返回是否执行了迁移。
    """
    ip = str(ip or "").strip()
    new_subject = str(new_subject or "").strip()
    if not ip or not new_subject:
        return False
    ip_subject = owner_subject_from_ip(ip)
    db = get_db()
    pending = db.execute(
        "SELECT 1 FROM tasks WHERE owner_subject = ? UNION ALL "
        "SELECT 1 FROM user_model_providers WHERE owner_subject = ? LIMIT 1",
        (ip_subject, ip_subject),
    ).fetchone()
    if pending is None:
        return False
    db.execute(
        "UPDATE tasks SET owner_subject = ?, owner_source = ? WHERE owner_subject = ?",
        (new_subject, source, ip_subject),
    )
    db.execute(
        "UPDATE user_model_providers SET owner_subject = ? WHERE owner_subject = ?",
        (new_subject, ip_subject),
    )
    db.commit()
    return True


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
