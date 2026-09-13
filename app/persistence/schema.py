from flask import current_app

from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.connection import (
    MODEL_THINKING_DEFAULT_MIGRATION_KEY,
    close_db,
    get_db,
    now_text,
)


def init_db():
    db = get_db()
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
            request_timeout INTEGER NOT NULL DEFAULT 3600,
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
            request_timeout INTEGER NOT NULL DEFAULT 3600,
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

        CREATE TABLE IF NOT EXISTS task_report_stats (
            task_id INTEGER PRIMARY KEY,
            source_updated_at TEXT NOT NULL,
            suppression_version TEXT NOT NULL DEFAULT '',
            issue_count INTEGER NOT NULL DEFAULT 0,
            suggestion_count INTEGER NOT NULL DEFAULT 0,
            non_issue_count INTEGER NOT NULL DEFAULT 0,
            accepted_issue_count INTEGER NOT NULL DEFAULT 0,
            rejected_issue_count INTEGER NOT NULL DEFAULT 0,
            pending_issue_acceptance_count INTEGER NOT NULL DEFAULT 0,
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
    _ensure_column(
        db, "check_items", "task_type", f"TEXT NOT NULL DEFAULT '{DOCUMENT_TASK_TYPE}'"
    )
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
    _migrate_model_thinking_defaults(db)
    _clear_finished_task_api_keys(db)
    _cleanup_orphaned_report_suppression_hits(db)
    current_app.teardown_appcontext(close_db)
    db.commit()


def _ensure_column(db, table: str, column: str, definition: str):
    columns = {
        row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    migrated = db.execute(
        "SELECT 1 FROM settings WHERE key = ?",
        (MODEL_THINKING_DEFAULT_MIGRATION_KEY,),
    ).fetchone()
    if migrated is not None:
        return

    now = now_text()
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
    db.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, 'true', ?)",
        (MODEL_THINKING_DEFAULT_MIGRATION_KEY, now),
    )


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
