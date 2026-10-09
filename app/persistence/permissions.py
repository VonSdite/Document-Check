"""用户主体登记与管理权限持久化。"""

from app.persistence.connection import get_db, now_text


def register_subject(subject: str):
    db = get_db()
    if (
        db.execute(
            "SELECT 1 FROM user_identities WHERE subject = ?", (subject,)
        ).fetchone()
        is not None
    ):
        return
    db.execute(
        "INSERT OR IGNORE INTO user_identities(subject, created_at) VALUES (?, ?)",
        (subject, now_text()),
    )
    db.commit()


def subject_permissions(subject: str) -> frozenset[str]:
    return frozenset(
        row["permission"]
        for row in get_db().execute(
            "SELECT permission FROM user_permissions WHERE subject = ?", (subject,)
        )
    )


def replace_subject_permissions(subject: str, permissions: set[str]) -> bool:
    db = get_db()
    if (
        db.execute(
            "SELECT 1 FROM user_identities WHERE subject = ?", (subject,)
        ).fetchone()
        is None
    ):
        return False
    with db:
        db.execute("DELETE FROM user_permissions WHERE subject = ?", (subject,))
        db.executemany(
            "INSERT INTO user_permissions(subject, permission) VALUES (?, ?)",
            [(subject, permission) for permission in sorted(permissions)],
        )
    return True


def permission_users(*, source: str, keyword: str, page: int, per_page: int):
    db = get_db()
    clauses, params = [], []
    if source:
        clauses.append("u.subject LIKE ?")
        params.append(f"{source}:%")
    joins = """
        LEFT JOIN settings profile ON profile.key = 'identity_profile:' || u.subject
        LEFT JOIN ip_usernames names ON u.subject = 'ip:' || names.ip
    """
    label = "COALESCE(NULLIF(json_extract(profile.value, '$.label'), ''), NULLIF(names.username, ''), u.subject)"
    if keyword:
        clauses.append(f"(u.subject LIKE ? ESCAPE '\\' OR {label} LIKE ? ESCAPE '\\')")
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.extend([f"%{escaped}%"] * 2)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = db.execute(
        f"SELECT COUNT(*) FROM user_identities u {joins} {where}", params
    ).fetchone()[0]
    page = min(max(1, page), max(1, (total + per_page - 1) // per_page))
    rows = [
        dict(row)
        for row in db.execute(
            f"SELECT u.subject, {label} AS label FROM user_identities u {joins} {where} "
            "ORDER BY u.subject LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        )
    ]
    by_subject = {row["subject"]: row for row in rows}
    for row in rows:
        row["permissions"] = set()
    if rows:
        placeholders = ",".join("?" for _ in rows)
        for grant in db.execute(
            f"SELECT subject, permission FROM user_permissions WHERE subject IN ({placeholders})",
            tuple(by_subject),
        ):
            by_subject[grant["subject"]]["permissions"].add(grant["permission"])
    return rows, page, total
