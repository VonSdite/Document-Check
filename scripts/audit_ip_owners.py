"""只读统计仍归属于 IP 的任务和模型配置。"""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def remaining_ip_owners(database: Path) -> list[dict]:
    database = database.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                """
                SELECT ip, SUM(task_count) AS task_count,
                       SUM(provider_count) AS provider_count,
                       SUM(model_count) AS model_count
                FROM (
                    SELECT substr(owner_subject, 4) AS ip, COUNT(*) AS task_count,
                           0 AS provider_count, 0 AS model_count
                    FROM tasks WHERE owner_subject LIKE 'ip:%'
                    GROUP BY owner_subject
                    UNION ALL
                    SELECT substr(p.owner_subject, 4), 0, COUNT(DISTINCT p.id), COUNT(m.id)
                    FROM user_model_providers p
                    LEFT JOIN user_model_configs m ON m.provider_id = p.id
                    WHERE p.owner_subject LIKE 'ip:%'
                    GROUP BY p.owner_subject
                )
                GROUP BY ip ORDER BY ip
                """
            )
        ]


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
    args = parser.parse_args()
    try:
        rows = remaining_ip_owners(args.database)
    except (OSError, sqlite3.Error) as error:
        parser.exit(1, f"查询失败：{error}\n")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif rows:
        print("IP\t任务数\t提供商数\t模型数")
        for row in rows:
            print(
                f"{row['ip']}\t{row['task_count']}\t{row['provider_count']}\t{row['model_count']}"
            )
    else:
        print("没有尚未迁移的 IP 归属数据。")


if __name__ == "__main__":
    main()
