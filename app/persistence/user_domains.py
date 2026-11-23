"""产品领域目录与用户领域关联。"""

from app.contracts.user_domains import normalize_domain_names
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
