from flask import current_app

from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.connection import (
    MODEL_THINKING_DEFAULT_MIGRATION_KEY,
    close_db,
    get_db,
    now_text,
)

MODEL_READ_TIMEOUT_MIGRATION_KEY = "model_provider_read_timeout_600_v1"

QUERY_INDEXES = {
    "idx_user_product_domains_domain": (
        "user_product_domains",
        "CREATE INDEX IF NOT EXISTS idx_user_product_domains_domain "
        "ON user_product_domains(domain_id, subject)",
    ),
    "idx_tasks_type_owner_status": (
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_type_owner_status "
        "ON tasks(task_type, owner_subject, status)",
    ),
    "idx_tasks_pending_file_cleanup": (
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_pending_file_cleanup "
        "ON tasks(source_files_cleaned_at, COALESCE(finished_at, updated_at, created_at), id) "
        "WHERE source_files_cleaned_at IS NULL "
        "AND status IN ('completed', 'partial', 'failed', 'canceled')",
    ),
    "idx_report_suppression_hits_task": (
        "report_suppression_hits",
        "CREATE INDEX IF NOT EXISTS idx_report_suppression_hits_task "
        "ON report_suppression_hits(task_id)",
    ),
    "idx_report_suppression_rules_enabled_type_updated": (
        "report_suppression_rules",
        "CREATE INDEX IF NOT EXISTS idx_report_suppression_rules_enabled_type_updated "
        "ON report_suppression_rules(task_type, updated_at) WHERE enabled = 1",
    ),
}


def init_db():
    db = get_db()
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    existing_tables = {
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_identities (
            subject TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_permissions (
            subject TEXT NOT NULL,
            permission TEXT NOT NULL CHECK(permission IN (
                'tasks.view_all', 'tasks.manage_all', 'stats.view_all', 'rules.manage',
                'reports.final_review'
            )),
            PRIMARY KEY(subject, permission),
            FOREIGN KEY(subject) REFERENCES user_identities(subject) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS product_domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_product_domains (
            subject TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            assigned_at TEXT NOT NULL,
            PRIMARY KEY(subject, domain_id),
            FOREIGN KEY(subject) REFERENCES user_identities(subject) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES product_domains(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS check_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL DEFAULT 'document_check',
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
            task_type TEXT NOT NULL DEFAULT 'document_check',
            ip TEXT NOT NULL,
            username_snapshot TEXT,
            owner_subject TEXT,
            owner_name_snapshot TEXT,
            owner_source TEXT,
            submission_token TEXT,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            document_text TEXT,
            document_meta_json TEXT,
            checks_json TEXT NOT NULL,
            checks_snapshot_json TEXT,
            retry_check_codes_json TEXT,
            provider_id INTEGER,
            provider_name TEXT,
            model_name TEXT NOT NULL,
            api_base TEXT NOT NULL,
            api_key TEXT,
            request_timeout INTEGER NOT NULL DEFAULT 600,
            max_input_chars INTEGER NOT NULL DEFAULT 80000,
            force_disable_thinking INTEGER NOT NULL DEFAULT 0,
            reasoning_effort TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            claim_token TEXT,
            lease_expires_at TEXT,
            result_json TEXT,
            summary TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            source_files_cleaned_at TEXT
        );

        CREATE TABLE IF NOT EXISTS user_model_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_subject TEXT NOT NULL,
            name TEXT NOT NULL,
            api_base TEXT NOT NULL,
            api_key TEXT,
            request_timeout INTEGER NOT NULL DEFAULT 600,
            max_input_chars INTEGER NOT NULL DEFAULT 500000,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_model_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            force_disable_thinking INTEGER NOT NULL DEFAULT 0,
            reasoning_effort TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(provider_id) REFERENCES user_model_providers(id) ON DELETE CASCADE,
            UNIQUE(provider_id, model_name, force_disable_thinking)
        );

        CREATE TABLE IF NOT EXISTS ip_usernames (
            ip TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS report_suppression_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            check_code TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            item_json TEXT NOT NULL,
            reason TEXT,
            enabled INTEGER NOT NULL DEFAULT 0,
            source_task_id INTEGER,
            source_result_code TEXT,
            source_item_id TEXT,
            hit_count INTEGER NOT NULL DEFAULT 0,
            last_hit_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_type, check_code, fingerprint)
        );

        CREATE TABLE IF NOT EXISTS report_suppression_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            result_code TEXT NOT NULL,
            item_id TEXT NOT NULL,
            item_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(rule_id) REFERENCES report_suppression_rules(id) ON DELETE CASCADE,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            UNIQUE(rule_id, task_id, result_code, item_id)
        );

        CREATE TABLE IF NOT EXISTS report_review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            result_code TEXT NOT NULL,
            item_id TEXT NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ('initial', 'final')),
            decision TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            actor_subject TEXT NOT NULL,
            actor_name TEXT NOT NULL,
            actor_source TEXT NOT NULL,
            channel TEXT NOT NULL,
            item_hash TEXT NOT NULL,
            item_json TEXT NOT NULL,
            acceptance_json TEXT NOT NULL,
            initial_review_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_report_review_events_item
            ON report_review_events(task_id, result_code, item_id, id);
        CREATE TABLE IF NOT EXISTS task_final_review_stats (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            total INTEGER NOT NULL,
            initial_count INTEGER NOT NULL,
            required_count INTEGER NOT NULL DEFAULT 0,
            confirmed INTEGER NOT NULL,
            returned INTEGER NOT NULL,
            deferred INTEGER NOT NULL,
            pending INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_tasks_final_review_stats_invalidate
        AFTER UPDATE OF result_json, status ON tasks
        BEGIN
            DELETE FROM task_final_review_stats WHERE task_id = NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_review_events_stats_invalidate
        AFTER INSERT ON report_review_events
        BEGIN
            DELETE FROM task_final_review_stats WHERE task_id = NEW.task_id;
        END;

        CREATE TABLE IF NOT EXISTS task_report_stats (
            task_id INTEGER PRIMARY KEY,
            source_updated_at TEXT NOT NULL,
            suppression_version TEXT NOT NULL DEFAULT '',
            issue_count INTEGER NOT NULL DEFAULT 0,
            suggestion_count INTEGER NOT NULL DEFAULT 0,
            non_issue_count INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            suppressed_count INTEGER NOT NULL DEFAULT 0,
            reviewed_item_count INTEGER NOT NULL DEFAULT 0,
            pending_review_item_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_live_results (
            task_id INTEGER PRIMARY KEY,
            result_json TEXT,
            summary TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TRIGGER IF NOT EXISTS trg_tasks_report_stats_invalidate
        AFTER UPDATE OF result_json ON tasks
        BEGIN
            DELETE FROM task_report_stats WHERE task_id = NEW.id;
        END;

        CREATE INDEX IF NOT EXISTS idx_tasks_ip_created ON tasks(ip, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_user_model_providers_owner ON user_model_providers(owner_subject, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_model_configs_provider ON user_model_configs(provider_id, sort_order ASC, id ASC);
        CREATE INDEX IF NOT EXISTS idx_report_suppression_rules_lookup
            ON report_suppression_rules(task_type, check_code, fingerprint, enabled);
        CREATE INDEX IF NOT EXISTS idx_report_suppression_hits_rule
            ON report_suppression_hits(rule_id, created_at DESC);
        """
    )
    _ensure_final_review_permission(db)
    _ensure_final_review_stats_scope(db)
    _ensure_column(
        db, "check_items", "task_type", f"TEXT NOT NULL DEFAULT '{DOCUMENT_TASK_TYPE}'"
    )
    _normalize_check_item_schema(db, existing_tables)
    _ensure_column(
        db, "tasks", "task_type", f"TEXT NOT NULL DEFAULT '{DOCUMENT_TASK_TYPE}'"
    )
    _ensure_column(db, "tasks", "document_text", "TEXT")
    _ensure_column(db, "tasks", "document_meta_json", "TEXT")
    _ensure_column(db, "tasks", "checks_snapshot_json", "TEXT")
    _ensure_column(db, "tasks", "retry_check_codes_json", "TEXT")
    _ensure_column(db, "tasks", "provider_id", "INTEGER")
    _ensure_column(db, "tasks", "force_disable_thinking", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "tasks", "reasoning_effort", "TEXT")
    _ensure_column(db, "user_model_configs", "reasoning_effort", "TEXT")
    _ensure_column(db, "tasks", "owner_subject", "TEXT")
    _ensure_column(db, "tasks", "owner_name_snapshot", "TEXT")
    _ensure_column(db, "tasks", "owner_source", "TEXT")
    _ensure_column(db, "tasks", "submission_token", "TEXT")
    _ensure_column(db, "tasks", "claim_token", "TEXT")
    _ensure_column(db, "tasks", "lease_expires_at", "TEXT")
    _ensure_column(db, "tasks", "source_files_cleaned_at", "TEXT")
    _ensure_column(
        db, "task_report_stats", "accepted_count", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        db, "task_report_stats", "rejected_count", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        db, "task_report_stats", "reviewed_item_count", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        db,
        "task_report_stats",
        "pending_review_item_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_owner_created ON tasks(owner_subject, created_at DESC)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_type_created ON tasks(task_type, created_at DESC, id DESC)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(task_type, status)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_lease ON tasks(status, lease_expires_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_created_id "
        "ON tasks(status, created_at ASC, id ASC)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_owner "
        "ON tasks(status, owner_subject)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_owner_created_id "
        "ON tasks(status, owner_subject, created_at ASC, id ASC)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_provider ON tasks(provider_id)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_type_owner_created "
        "ON tasks(task_type, owner_subject, created_at DESC, id DESC)"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_submission_token "
        "ON tasks(task_type, owner_subject, submission_token) "
        "WHERE submission_token IS NOT NULL"
    )
    _migrate_task_owners(db)
    if "user_identities" not in existing_tables:
        _register_existing_subjects(db)
    _migrate_model_thinking_defaults(db)
    _migrate_model_read_timeouts(db)
    _clear_finished_task_api_keys(db)
    _initialize_query_indexes(db, existing_tables)
    _cleanup_orphaned_report_suppression_hits(db)
    current_app.teardown_appcontext(close_db)
    db.commit()


def _ensure_final_review_stats_scope(db):
    """终审缓存独立记录初审不认可的条目数量。"""
    db.execute("SAVEPOINT final_review_scope")
    try:
        db.execute("DELETE FROM task_final_review_stats WHERE 0")
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(task_final_review_stats)")
        }
        if "required_count" not in columns:
            db.execute(
                "ALTER TABLE task_final_review_stats "
                "ADD COLUMN required_count INTEGER NOT NULL DEFAULT 0"
            )
            db.execute("DELETE FROM task_final_review_stats")
    except Exception:
        db.execute("ROLLBACK TO final_review_scope")
        raise
    finally:
        db.execute("RELEASE final_review_scope")


def _ensure_final_review_permission(db):
    """权限取值包含终审能力，保留各主体的授权。"""
    db.execute("SAVEPOINT final_review_permission")
    try:
        db.execute("UPDATE user_permissions SET permission = permission WHERE 0")
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'user_permissions'"
        ).fetchone()[0]
        if "reports.final_review" not in sql:
            db.execute("""
                CREATE TABLE user_permissions_expanded (
                    subject TEXT NOT NULL,
                    permission TEXT NOT NULL CHECK(permission IN (
                        'tasks.view_all', 'tasks.manage_all', 'stats.view_all', 'rules.manage',
                        'reports.final_review'
                    )),
                    PRIMARY KEY(subject, permission),
                    FOREIGN KEY(subject) REFERENCES user_identities(subject) ON DELETE CASCADE
                )
            """)
            db.execute(
                "INSERT INTO user_permissions_expanded SELECT * FROM user_permissions"
            )
            db.execute("DROP TABLE user_permissions")
            db.execute(
                "ALTER TABLE user_permissions_expanded RENAME TO user_permissions"
            )
    except Exception:
        db.execute("ROLLBACK TO final_review_permission")
        raise
    finally:
        db.execute("RELEASE final_review_permission")


def _register_existing_subjects(db):
    db.execute(
        """
        INSERT OR IGNORE INTO user_identities(subject, created_at)
        SELECT subject, ? FROM (
            SELECT owner_subject AS subject FROM tasks
            UNION SELECT owner_subject FROM user_model_providers
            UNION SELECT 'ip:' || ip FROM ip_usernames
            UNION SELECT substr(key, 18) FROM settings
                  WHERE substr(key, 1, 17) = 'identity_profile:'
        )
        WHERE (subject LIKE 'ip:%' AND length(subject) > 3)
           OR (subject LIKE 'cookie_session:%' AND length(subject) > 15)
        """,
        (now_text(),),
    )


def _initialize_query_indexes(db, existing_tables: set[str]):
    # 新建表使用完整的索引定义，字段准备完成后创建索引。
    for table, sql in QUERY_INDEXES.values():
        if table not in existing_tables:
            db.execute(sql)

    # TODO(index-compat): BEGIN 兼容窗口结束后删除此调用，保留新建表的索引初始化。
    _ensure_existing_query_indexes(db, existing_tables)
    # TODO(index-compat): END


# TODO(index-compat): BEGIN 已有表的索引补齐；兼容窗口结束后删除此函数及调用。
def _ensure_existing_query_indexes(db, existing_tables: set[str]):
    if not existing_tables.intersection(table for table, _ in QUERY_INDEXES.values()):
        return
    existing_indexes = {
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    indexed_tables = set()
    for name, (table, sql) in QUERY_INDEXES.items():
        if table in existing_tables and name not in existing_indexes:
            db.execute(sql)
            indexed_tables.add(table)
    for table in sorted(indexed_tables):
        db.execute(f"ANALYZE {table}")


# TODO(index-compat): END


def _ensure_column(db, table: str, column: str, definition: str):
    columns = {
        row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _normalize_check_item_schema(db, existing_tables):
    """统一检查项字段，保留普通检查项和任务历史。"""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(check_items)")}
    if not (
        columns & {"kind", "definition_json", "version"}
        or "skill_cases" in existing_tables
        or db.execute(
            "SELECT 1 FROM settings WHERE key = 'task_report_limit'"
        ).fetchone()
    ):
        return
    db.execute("SAVEPOINT check_item_schema")
    try:
        db.execute("DELETE FROM settings WHERE key = 'task_report_limit'")
        db.execute("DELETE FROM task_report_stats")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(check_items)")}
        db.execute("DROP TABLE IF EXISTS skill_cases")
        if "kind" in columns:
            db.execute("DELETE FROM check_items WHERE kind = 'specialist'")
        for column in ("kind", "definition_json", "version"):
            if column in columns:
                db.execute(f"ALTER TABLE check_items DROP COLUMN {column}")
    except Exception:
        db.execute("ROLLBACK TO check_item_schema")
        raise
    finally:
        db.execute("RELEASE check_item_schema")


def _migrate_task_owners(db):
    db.execute(
        """
        UPDATE tasks
        SET owner_subject = 'ip:' || ip
        WHERE owner_subject IS NULL OR owner_subject = ''
        """
    )
    db.execute(
        """
        UPDATE tasks
        SET owner_name_snapshot = username_snapshot
        WHERE (owner_name_snapshot IS NULL OR owner_name_snapshot = '') AND username_snapshot IS NOT NULL
        """
    )
    db.execute(
        """
        UPDATE tasks
        SET owner_source = 'ip'
        WHERE owner_source IS NULL OR owner_source = ''
        """
    )


def _migrate_model_thinking_defaults(db):
    now = now_text()
    db.execute("SAVEPOINT model_thinking_defaults")
    try:
        inserted = db.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, 'true', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (MODEL_THINKING_DEFAULT_MIGRATION_KEY, now),
        )
        if inserted.rowcount:
            db.execute(
                """
                DELETE FROM user_model_configs AS current
                WHERE current.force_disable_thinking = 1
                  AND EXISTS (
                      SELECT 1
                      FROM user_model_configs AS enabled
                      WHERE enabled.provider_id = current.provider_id
                        AND enabled.model_name = current.model_name
                        AND enabled.force_disable_thinking = 0
                  )
                """
            )
            db.execute(
                """
                UPDATE user_model_configs
                SET force_disable_thinking = 0, updated_at = ?
                WHERE force_disable_thinking = 1
                """,
                (now,),
            )
    except Exception:
        db.execute("ROLLBACK TO model_thinking_defaults")
        raise
    finally:
        db.execute("RELEASE model_thinking_defaults")


def _migrate_model_read_timeouts(db):
    now = now_text()
    db.execute("SAVEPOINT model_read_timeout_defaults")
    try:
        inserted = db.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, 'true', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (MODEL_READ_TIMEOUT_MIGRATION_KEY, now),
        )
        if inserted.rowcount:
            db.execute(
                """
                UPDATE user_model_providers
                SET request_timeout = 600, updated_at = ?
                WHERE request_timeout != 600
                """,
                (now,),
            )
    except Exception:
        db.execute("ROLLBACK TO model_read_timeout_defaults")
        raise
    finally:
        db.execute("RELEASE model_read_timeout_defaults")


def _clear_finished_task_api_keys(db):
    db.execute(
        """
        UPDATE tasks
        SET api_key = NULL
        WHERE status IN ('completed', 'partial', 'failed', 'canceled')
          AND api_key IS NOT NULL
        """
    )


def _cleanup_orphaned_report_suppression_hits(db):
    db.execute(
        """
        DELETE FROM report_suppression_hits
        WHERE NOT EXISTS (
            SELECT 1 FROM tasks WHERE tasks.id = report_suppression_hits.task_id
        )
        """
    )
