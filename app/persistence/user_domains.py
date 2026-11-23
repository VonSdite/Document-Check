"""领域目录与用户领域关联。"""

from app.contracts.user_domains import (
    MAX_DOMAIN_BATCH_USERS,
    MAX_USER_DOMAINS,
    normalize_domain_names,
)
from app.persistence.connection import get_db, now_text


def product_domains() -> list[dict]:
    return [
        dict(row)
        for row in get_db().execute(
            "SELECT id, name FROM product_domains ORDER BY name_key, id"
        )
    ]


def user_domains(subjects: list[str]) -> dict[str, list[dict]]:
    result = {subject: [] for subject in subjects}
    if not subjects:
        return result
    placeholders = ",".join("?" for _ in subjects)
    for row in get_db().execute(
        "SELECT u.subject, d.id, d.name FROM user_product_domains u "
        "JOIN product_domains d ON d.id = u.domain_id "
        f"WHERE u.subject IN ({placeholders}) ORDER BY d.name_key, d.id",
        subjects,
    ):
        result[row["subject"]].append({"id": row["id"], "name": row["name"]})
    return result


def replace_user_domains(subject: str, values: list[str]) -> bool:
    names = normalize_domain_names(values)
    db = get_db()
    if (
        db.execute(
            "SELECT 1 FROM user_identities WHERE subject = ?", (subject,)
        ).fetchone()
        is None
    ):
        return False
    with db:
        ids = []
        now = now_text()
        for name in names:
            db.execute(
                "INSERT INTO product_domains(name, name_key, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name_key) DO NOTHING",
                (name, name.casefold(), now),
            )
            ids.append(
                db.execute(
                    "SELECT id FROM product_domains WHERE name_key = ?",
                    (name.casefold(),),
                ).fetchone()["id"]
            )
        db.execute("DELETE FROM user_product_domains WHERE subject = ?", (subject,))
        db.executemany(
            "INSERT INTO user_product_domains(subject, domain_id, assigned_at) VALUES (?, ?, ?)",
            [(subject, domain_id, now) for domain_id in ids],
        )
    return True


def add_user_domains(
    subjects: list[str], values: list[str]
) -> dict[str, list[dict]] | None:
    """为所选用户原子追加领域，复用已有标签并保留各自关联。"""
    subjects = list(
        dict.fromkeys(subject.strip() for subject in subjects if subject.strip())
    )
    if not subjects:
        raise ValueError("请先选择用户。")
    if len(subjects) > MAX_DOMAIN_BATCH_USERS:
        raise ValueError(f"每次最多选择 {MAX_DOMAIN_BATCH_USERS} 位用户。")
    names = normalize_domain_names(values)
    if not names:
        raise ValueError("请至少选择一个领域。")
    db = get_db()
    with db:
        db.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in subjects)
        count = db.execute(
            f"SELECT COUNT(*) FROM user_identities WHERE subject IN ({placeholders})",
            subjects,
        ).fetchone()[0]
        if count != len(subjects):
            return None
        existing = user_domains(subjects)
        keys = {name.casefold() for name in names}
        for subject, domains in existing.items():
            if (
                len(keys | {item["name"].casefold() for item in domains})
                > MAX_USER_DOMAINS
            ):
                raise ValueError(
                    f"用户 {subject} 添加后将超过 {MAX_USER_DOMAINS} 个领域，请调整选择。"
                )
        now = now_text()
        for name in names:
            db.execute(
                "INSERT INTO product_domains(name, name_key, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name_key) DO NOTHING",
                (name, name.casefold(), now),
            )
            domain_id = db.execute(
                "SELECT id FROM product_domains WHERE name_key = ?",
                (name.casefold(),),
            ).fetchone()["id"]
            db.executemany(
                "INSERT INTO user_product_domains(subject, domain_id, assigned_at) VALUES (?, ?, ?) "
                "ON CONFLICT(subject, domain_id) DO NOTHING",
                [(subject, domain_id, now) for subject in subjects],
            )
        return user_domains(subjects)
