"""检查节点状态按任务执行权持久化。"""

import json

from app.persistence.connection import get_db, now_text


class CheckpointOwnershipLost(RuntimeError):
    pass


class CheckpointStore:
    def __init__(
        self, task_id: int, code: str, signature: str, claim_token: str | None
    ):
        self.task_id = task_id
        self.code = code
        self.signature = signature
        self.claim_token = claim_token

    def load(self) -> dict | None:
        row = (
            get_db()
            .execute(
                "SELECT state_json FROM task_checkpoints WHERE task_id = ? AND check_code = ? AND signature = ?",
                (self.task_id, self.code, self.signature),
            )
            .fetchone()
        )
        return json.loads(row["state_json"]) if row else None

    def save(self, state: dict):
        db = get_db()
        updated = db.execute(
            """
            INSERT INTO task_checkpoints(task_id, check_code, signature, state_json, updated_at)
            SELECT ?, ?, ?, ?, ? WHERE EXISTS (
                SELECT 1 FROM tasks WHERE id = ? AND status = 'running'
                AND cancel_requested = 0 AND (? IS NULL OR claim_token = ?)
            )
            ON CONFLICT(task_id, check_code) DO UPDATE SET signature = excluded.signature,
                state_json = excluded.state_json, updated_at = excluded.updated_at
            """,
            (
                self.task_id,
                self.code,
                self.signature,
                json.dumps(state, ensure_ascii=False),
                now_text(),
                self.task_id,
                self.claim_token,
                self.claim_token,
            ),
        )
        db.commit()
        if updated.rowcount != 1:
            raise CheckpointOwnershipLost("任务执行权已结束，停止保存审查状态")


class MemoryCheckpointStore:
    """独立评测使用相同的节点恢复接口。"""

    def __init__(self):
        self.state = None

    def load(self):
        return json.loads(json.dumps(self.state)) if self.state is not None else None

    def save(self, state):
        self.state = json.loads(json.dumps(state))
