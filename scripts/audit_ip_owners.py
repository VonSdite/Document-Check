"""只读统计切换到 cookie_session 后仍归属于 IP 的任务和模型配置。

脚本只检查 owner_subject = ip:<IP> 的数据。tasks.ip 是审计字段，已迁移到
cookie_session:<稳定ID> 的任务仍会保留原始 IP，但不会计入未迁移数据。
"""

import argparse
import json
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

COLUMNS = (
    ("ip", "IP", "left"),
    ("username", "用户名", "left"),
    ("task_count", "未迁移任务数", "right"),
    ("provider_count", "未迁移提供商数", "right"),
    ("model_count", "未迁移模型数", "right"),
)


def remaining_ip_owners(database: Path) -> list[dict]:
    database = database.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return _remaining_ip_owners(db)


def delete_ip_owner_data(database: Path, ip: str, *, confirmed: bool) -> dict:
    database = database.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    ip = str(ip or "").strip()
    if not ip:
        raise ValueError("IP 不能为空")
    owner_subject = f"ip:{ip}"
    with closing(sqlite3.connect(database, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        rows = _remaining_ip_owners(db, owner_subject)
        summary = rows[0] if rows else _empty_summary(ip)
        result = {
            "ip": ip,
            "confirmed": confirmed,
            "matched": dict(summary),
            "deleted": {
                "tasks": 0,
                "task_activity_settings": 0,
                "report_suppression_hits": 0,
                "task_live_results": 0,
                "task_report_stats": 0,
                "providers": 0,
                "models": 0,
            },
        }
        if not confirmed:
            return result

        task_ids = _ids_for_owner(db, "tasks", owner_subject)
        provider_ids = _ids_for_owner(db, "user_model_providers", owner_subject)
        external_refs = _provider_external_task_refs(db, provider_ids, owner_subject)
        if external_refs:
            refs = ", ".join(str(task_id) for task_id in external_refs[:20])
            suffix = "..." if len(external_refs) > 20 else ""
            raise ValueError(
                "待删除提供商仍被非目标归属任务引用，未执行删除。"
                f"任务 ID：{refs}{suffix}"
            )
        with db:
            activity_keys = [f"task_activity:{task_id}" for task_id in task_ids]
            result["deleted"]["task_activity_settings"] = _delete_by_values(
                db, "settings", "key", activity_keys
            )
            for table in (
                "report_suppression_hits",
                "task_live_results",
                "task_report_stats",
            ):
                result["deleted"][table] = _delete_by_values(
                    db, table, "task_id", task_ids
                )
            result["deleted"]["tasks"] = _delete_by_values(db, "tasks", "id", task_ids)
            result["deleted"]["models"] = _delete_by_values(
                db, "user_model_configs", "provider_id", provider_ids
            )
            result["deleted"]["providers"] = _delete_by_values(
                db, "user_model_providers", "id", provider_ids
            )
        return result


def _remaining_ip_owners(
    db: sqlite3.Connection, owner_subject: str | None = None
) -> list[dict]:
    task_filter = "owner_subject LIKE 'ip:%'"
    provider_filter = "p.owner_subject LIKE 'ip:%'"
    params = []
    if owner_subject is not None:
        task_filter += " AND owner_subject = ?"
        provider_filter += " AND p.owner_subject = ?"
        params.extend([owner_subject, owner_subject])
    query = f"""
        SELECT owners.ip,
               COALESCE(NULLIF(MAX(u.username), ''),
                        NULLIF(MAX(owners.task_username), ''), '') AS username,
               SUM(owners.task_count) AS task_count,
               SUM(owners.provider_count) AS provider_count,
               SUM(owners.model_count) AS model_count
        FROM (
            SELECT substr(owner_subject, 4) AS ip, COUNT(*) AS task_count,
                   0 AS provider_count, 0 AS model_count,
                   COALESCE(MAX(NULLIF(owner_name_snapshot, '')),
                            MAX(NULLIF(username_snapshot, '')), '') AS task_username
            FROM tasks WHERE {task_filter}
            GROUP BY owner_subject
            UNION ALL
            SELECT substr(p.owner_subject, 4), 0, COUNT(DISTINCT p.id),
                   COUNT(m.id), ''
            FROM user_model_providers p
            LEFT JOIN user_model_configs m ON m.provider_id = p.id
            WHERE {provider_filter}
            GROUP BY p.owner_subject
        ) owners
        LEFT JOIN ip_usernames u ON u.ip = owners.ip
        GROUP BY owners.ip ORDER BY owners.ip
    """
    return [dict(row) for row in db.execute(query, params)]


def _empty_summary(ip: str) -> dict:
    return {
        "ip": ip,
        "username": "",
        "task_count": 0,
        "provider_count": 0,
        "model_count": 0,
    }


def _ids_for_owner(db: sqlite3.Connection, table: str, owner_subject: str) -> list[int]:
    return [
        row["id"]
        for row in db.execute(
            f"SELECT id FROM {table} WHERE owner_subject = ?", (owner_subject,)
        )
    ]


def _delete_by_values(
    db: sqlite3.Connection, table: str, column: str, values: list[int | str]
) -> int:
    if not values:
        return 0
    if not _table_exists(db, table):
        return 0
    deleted = 0
    for chunk_start in range(0, len(values), 500):
        chunk = values[chunk_start : chunk_start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        cursor = db.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})", chunk
        )
        deleted += cursor.rowcount
    return deleted


def _provider_external_task_refs(
    db: sqlite3.Connection, provider_ids: list[int], owner_subject: str
) -> list[int]:
    if not provider_ids:
        return []
    if not _table_exists(db, "tasks"):
        return []
    refs = []
    for chunk_start in range(0, len(provider_ids), 500):
        chunk = provider_ids[chunk_start : chunk_start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        refs.extend(
            row["id"]
            for row in db.execute(
                f"""
                SELECT id FROM tasks
                WHERE provider_id IN ({placeholders})
                  AND COALESCE(owner_subject, '') <> ?
                ORDER BY id
                """,
                [*chunk, owner_subject],
            )
        )
    return refs


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def format_table(rows: list[dict]) -> str:
    widths = {
        key: max(
            [_display_width(header)]
            + [_display_width(_display_value(row, key)) for row in rows]
        )
        for key, header, _align in COLUMNS
    }
    lines = [
        "  ".join(
            _pad_display(header, widths[key], align) for key, header, align in COLUMNS
        )
    ]
    for row in rows:
        lines.append(
            "  ".join(
                _pad_display(_display_value(row, key), widths[key], align)
                for key, _header, align in COLUMNS
            )
        )
    return "\n".join(lines)


def _display_value(row: dict, key: str) -> str:
    value = row.get(key, "")
    if key == "username" and not value:
        return "-"
    return str(value)


def _pad_display(value: str, width: int, align: str) -> str:
    padding = " " * max(width - _display_width(value), 0)
    if align == "right":
        return f"{padding}{value}"
    return f"{value}{padding}"


def _display_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        for char in str(value)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "instance"
        / "document_check.sqlite3",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--delete-ip",
        metavar="IP",
        help="预览该 IP 仍归属 ip:<IP> 的任务、提供商和模型数据",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="与 --delete-ip 搭配使用，确认执行删除",
    )
    args = parser.parse_args()
    try:
        if args.delete_ip:
            result = delete_ip_owner_data(
                args.database, args.delete_ip, confirmed=args.yes
            )
            return _print_delete_result(result, args.json)
        rows = remaining_ip_owners(args.database)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"查询失败：{error}\n")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif rows:
        print(format_table(rows))
    else:
        print("没有尚未迁移的 IP 归属数据。")


def _print_delete_result(result: dict, json_output: bool):
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(format_table([result["matched"]]))
    if not result["confirmed"]:
        print("未执行删除。确认无误后追加 --yes 执行删除。")
        return
    deleted = result["deleted"]
    print(
        "已删除："
        f"任务 {deleted['tasks']} 条，"
        f"任务活动 {deleted['task_activity_settings']} 条，"
        f"误报命中 {deleted['report_suppression_hits']} 条，"
        f"实时结果 {deleted['task_live_results']} 条，"
        f"报告统计 {deleted['task_report_stats']} 条，"
        f"提供商 {deleted['providers']} 条，"
        f"模型 {deleted['models']} 条。"
    )


if __name__ == "__main__":
    main()
