import json
import sqlite3
from datetime import datetime

from flask import current_app, g

from .limits import DEFAULT_ISSUE_OUTPUT_LIMIT, normalize_issue_output_limit
from .task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE, IMAGE_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE, VIDEO_TASK_TYPE


MODEL_THINKING_DEFAULT_MIGRATION_KEY = "model_force_disable_thinking_default_v2"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g.db = db
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


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
            provider_id INTEGER,
            provider_name TEXT,
            model_name TEXT NOT NULL,
            api_base TEXT NOT NULL,
            api_key TEXT,
            request_timeout INTEGER NOT NULL DEFAULT 3600,
            max_input_chars INTEGER NOT NULL DEFAULT 80000,
            force_disable_thinking INTEGER NOT NULL DEFAULT 0,
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
    _ensure_column(db, "check_items", "task_type", f"TEXT NOT NULL DEFAULT '{DOCUMENT_TASK_TYPE}'")
    _ensure_column(db, "tasks", "task_type", f"TEXT NOT NULL DEFAULT '{DOCUMENT_TASK_TYPE}'")
    _ensure_column(db, "tasks", "document_text", "TEXT")
    _ensure_column(db, "tasks", "document_meta_json", "TEXT")
    _ensure_column(db, "tasks", "checks_snapshot_json", "TEXT")
    _ensure_column(db, "tasks", "provider_id", "INTEGER")
    _ensure_column(db, "tasks", "force_disable_thinking", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "tasks", "owner_subject", "TEXT")
    _ensure_column(db, "tasks", "owner_name_snapshot", "TEXT")
    _ensure_column(db, "tasks", "owner_source", "TEXT")
    _ensure_column(db, "tasks", "submission_token", "TEXT")
    _ensure_column(db, "tasks", "claim_token", "TEXT")
    _ensure_column(db, "tasks", "lease_expires_at", "TEXT")
    _ensure_column(db, "tasks", "source_files_cleaned_at", "TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_created ON tasks(owner_subject, created_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_type_created ON tasks(task_type, created_at DESC, id DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(task_type, status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_lease ON tasks(status, lease_expires_at)")
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
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
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
        WHERE status IN ('completed', 'failed', 'canceled')
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
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
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
    row = get_db().execute("SELECT username FROM ip_usernames WHERE ip = ?", (ip,)).fetchone()
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


_DOCUMENT_CHECK_OUTPUT_REQUIREMENTS = """统一定位与输出要求：
1. 每条问题必须给出真实、可核对的文档证据，不要只写结论。
2. 位置优先使用文档文本中明确出现的章节号、标题、小节号或小节标题，并尽量给出完整章节路径，例如“第2章 > 2.1节 > 参数配置”。无法识别章节时写“章节：未识别”。
3. 页码仅作为章节位置的辅助信息。只有文档文本中明确存在“[第12页]”等页码标记时才能引用；不得根据篇幅、段落数量或上下文推测页码。页码与章节或原文线索疑似冲突时，以章节信息和原文摘录为准。
4. 同时补充附近原文线索；对工作表或表格内容，可使用工作表名、字段名、行内关键文字定位。不得使用“大约第几页”“文档中部”等模糊位置。
5. 遵循系统规定的结构化报告格式，不增加、删除或改变报告字段。每条填写：问题类型、位置、原文摘录、问题描述、影响说明、修改建议；严重程度和证据可信度按系统规则填写。
6. 原文摘录必须来自所提供的文档文本，不得拼接、改写或编造。修改建议应具体、客观、可执行，不得补充文档外事实。
7. 同一问题只输出一次；同类问题在多个位置出现时合并位置。明确不是问题或无需修改的内容不要生成报告条目。"""


DEFAULT_DOCUMENT_CHECK_ITEMS = (
    {
        "code": "compliance",
        "name": "文档规范性检查",
        "description": "检查错别字、语法、术语书写、数字单位格式和客户表达等文字规范问题。",
        "prompt": """你是资深技术文档规范审查专家，熟悉面向客户资料的语言文字规范、术语规范、书写格式和客户表达要求。请只检查能够从抽取文本中直接确认的文字与表达规范问题。

文档文本由解析器抽取得到，换行、分页、表格分隔符、空白和部分内容顺序可能与原始版式不同。不要把解析造成的变化判为原文问题。

检查范围：
1. 语言文字规范：明确的错别字、同音字或形近字误用、固定词语误写；能够根据上下文确定的漏字、多字；“的的”“进行进行”等机械性重复；明确的标点误用、成对标点缺失或中英文标点误用；修改方式基本唯一的成分残缺、搭配错误、关联词误用等语法错误。
2. 术语与命名书写：产品名、功能名、模块名、接口名、参数名、字段名、中英文名称、缩写和英文大小写的明确书写形式问题；不适合对外使用的内部代号、研发代号或临时代称。没有企业术语表、命名规范或文内明确依据时，不要仅凭语言习惯认定专业术语错误。
3. 数字、单位和符号：日期、时间、数字、单位、全角/半角符号、中英文及数字间空格的明确格式问题；同类日期、数值和单位的书写形式不统一。例如“10 MB”和“10MB”属于格式问题，而同一参数出现“10 MB”和“20 MB”属于内容正确性问题。
4. 客户资料表达：内部沟通、研发评审、聊天式、情绪化、主观化、责备客户、推卸责任或明显过于随意的表达；“研发暂定”“客户自己处理”“随便配置即可”等未转换为正式客户语言的内容。必须结合上下文判断，不能仅因出现某个词语就报告。

严格边界：
1. 修改应采用最小必要改动。存在多种合理写法、仅是个人偏好或只是可以写得更好时，不要报告。
2. 对人名、地名、机构名、品牌名、产品名、型号、命令、代码、路径、URL和变量名保持谨慎；无法高可信度确认时直接忽略，不输出“疑似错别字”。
3. 不检查长句、指代不清、信息组织复杂等易理解性问题。
4. 不检查事实、参数、结论、名称含义或操作步骤的错误与前后冲突。
5. 不检查前提条件、术语解释、操作信息等内容是否缺失。
6. 不检查标题层级、章节顺序、目录、编号、交叉引用、字体、缩进、对齐、分页、页面布局、图片内容或链接有效性。
7. 不检查合规风险、商业承诺、敏感信息、安全提示、版本、版权、保密级别和修订记录。

问题类型建议使用：错别字、漏字或多字、机械性重复、标点误用、明确语法错误、术语或命名书写不规范、数字单位或符号格式不规范、客户表达不规范。"""
        + "\n\n"
        + _DOCUMENT_CHECK_OUTPUT_REQUIREMENTS
        + "\n8. 如果未发现明确问题，summary 写“未发现明显规范性问题”，items 返回空数组。",
        "sort_order": 10,
    },
    {
        "code": "understandability",
        "name": "易理解性检查",
        "description": "检查内容已提供但主体、对象、条件、指代或表达组织导致客户难以理解的问题。",
        "prompt": """你是资深技术文档易理解性审查专家，熟悉用户手册、安装指南、配置指南、维护指南和调测指南等客户资料的表达要求。请从客户阅读和执行角度，检查“内容已经提供，但表达方式导致客户难以准确理解”的问题。

文档文本由解析器抽取得到，换行、分页、表格分隔符、空白和部分内容顺序可能与原始版式不同。不要把解析造成的变化判为易理解性问题。

检查范围：
1. 主体、对象和动作不明确：无法确定由谁执行、针对哪个设备/模块/页面/参数操作，或一句话中多个对象与后续动作的对应关系不清。
2. 指代不清：“其”“该”“上述”“前者”“后者”“相关”等存在多个可能指向对象。只有能够指出至少两个合理指向或确实无法确定指向时才报告。
3. 条件、范围和边界不清：“必要时”“适当”“部分场景”“相关版本”等缺少可理解的判断边界；多个条件之间的并且、或者、除非、仅当关系表达不清。
4. 句子结构复杂：一个句子混合过多条件、动作、例外、风险和结果，多层从句、插入语或多重否定使句子主干难以识别。不能仅按字数判断长句。
5. 多动作混杂：一个步骤同时要求点击、输入、选择、保存、重启和验证，动作边界或先后关系难以辨认；正常操作、验证和异常处理混在一起。
6. 含糊或不可操作表达：“进行相关操作”“完成相应配置”“根据实际情况处理”“适当调整参数”等在上下文中仍无法对应到明确动作、对象或判断标准。
7. 信息关系不清：原因、条件、操作、结果、例外之间的承接关系不清，同一段落频繁切换对象，已有信息的呈现顺序使客户难以建立对应关系。
8. 专业表达难懂：术语已经解释但解释仍然抽象，多个缩写连续堆叠，参数说明只是重复名称而未清楚说明作用。
9. 语义重复和冗余：语法正确但相同含义反复表达，大量背景说明掩盖关键动作。为强调安全、限制或关键操作而进行的合理重复不要报告。
10. 表格文字难理解：能够从文本中确认的表头、字段名、参数说明或单元格条件关系过于抽象、相近或混杂。表格可能因解析顺序失真时不要报告。

严格边界：
1. 错别字、漏字、多字、机械重复、标点、明确语法错误和书写格式属于文档规范性检查。
2. 信息完全没有提供属于内容完整性检查；信息已经提供但难以理解才属于本检查项。
3. 事实、参数、步骤顺序或逻辑结论错误以及前后冲突属于内容正确性检查。
4. 不检查标题层级、目录、编号、交叉引用、字体、页面布局、图片内容或链接有效性。
5. 不要把个人写作偏好当成问题。每条必须指出具体理解障碍，不能只写“表达不清”或“建议优化”。
6. 建议改写不得增加条件、参数、步骤、结论或其他技术事实；无法保持原意时，只提出拆分或澄清建议。

问题类型建议使用：主体不明确、操作对象不明确、指代不清、条件边界不清、含糊表达、句子结构复杂、多动作混杂、信息关系不清、专业表达难懂、重复冗余、表格文字难理解。"""
        + "\n\n"
        + _DOCUMENT_CHECK_OUTPUT_REQUIREMENTS
        + "\n8. 如果未发现明确问题，summary 写“未发现明显易理解性问题”，items 返回空数组。",
        "sort_order": 15,
    },
    {
        "code": "consistency",
        "name": "内容正确性检查",
        "description": "检查文内可验证的事实、参数、条件、步骤、结论和约束是否错误或相互冲突。",
        "prompt": """你是严谨的技术文档内容正确性审查专家。请检查文档中已经写出的事实、参数、条件、步骤、结论和约束是否存在文内可验证的错误、矛盾或口径冲突。

重要能力边界：如果只提供一份待检查文档而没有产品规格、需求、接口定义、标准或其他权威素材，只能判断文内正确性和一致性，不能凭外部常识断言真实技术事实错误。只有系统同时提供权威依据时，才可以依据该材料判断外部事实正确性。

检查范围：
1. 名称与概念冲突：同一对象使用多个名称却未说明关系；同一名称在不同位置指代不同对象；术语定义、中英文名称或缩写含义互相冲突。合理简称和已说明的上下位关系不要报告。
2. 数据与参数冲突：同一参数、默认值、阈值、范围、端口、容量、版本、日期、环境要求或配置值在正文、表格、示例中不一致。单位等价换算且数值等价时不要报告。
3. 条件与适用范围冲突：同一功能、步骤、限制或例外在不同位置的适用对象、版本、场景、权限、状态或前提条件不一致。
4. 逻辑与结论冲突：前提与结论、原因与结果、正文与总结之间存在直接矛盾；同一状态被同时描述为支持和不支持、允许和禁止、成功和失败。
5. 操作内容冲突：同一操作在不同位置的入口、对象、参数、动作、顺序或预期结果互相冲突。只有一处描述且缺乏权威依据时，不要凭经验判断步骤技术上错误。
6. 约束强度冲突：关于同一事项的“严禁、禁止、不得、必须、应、建议、可、允许、例外”等强制程度不同，且适用条件不能解释差异。
7. 风险与处置冲突：同一风险的后果、处置方式、备份要求、权限要求、停机或重启要求前后矛盾。缺少风险说明属于完整性问题，不属于本检查项。

判定方法：
1. 每条明确问题原则上至少提供两处可以直接对照的文档证据；如果依据系统提供的权威素材判断，则提供待检查内容和权威依据各一处证据。
2. 在 location 中分别列出证据A、证据B的章节位置；在 excerpt 中分别引用冲突原文；在 description 中明确写出两处内容为什么不能同时成立。
3. 上下文、适用条件、版本或对象不同可以合理解释差异时，不要报告。证据不足但确有直接冲突线索时标为 suggestion，并明确需要核对的依据；不要把猜测写成 issue。
4. 不检查错别字、标点、书写格式和表达正式程度；不检查内容是否容易理解；不检查客户需要的信息是否缺失。
5. 不检查标题层级、目录、编号、图表视觉内容、页面布局和链接有效性。

问题类型建议使用：名称或术语冲突、参数或数据冲突、版本或日期冲突、条件或适用范围冲突、逻辑或结论冲突、操作内容冲突、约束强度冲突、风险处置冲突。"""
        + "\n\n"
        + _DOCUMENT_CHECK_OUTPUT_REQUIREMENTS
        + "\n8. 如果未发现明确问题，summary 写“未发现明显内容正确性问题”，items 返回空数组。",
        "sort_order": 20,
    },
    {
        "code": "completeness",
        "name": "内容完整性检查",
        "description": "检查客户完成理解和操作所必需的前提、步骤、参数、结果、异常及限制信息是否缺失。",
        "prompt": """你是严谨的技术文档内容完整性审查专家。请检查客户理解、配置、安装、操作、维护或排障所必需的信息是否缺失。

完整性判断必须保守。不能仅凭通用写作经验推测文档“应该有某一章”或“应该补充某项内容”。只有文档类型、上下文、已写出的操作、系统提供的模板/清单或文内明确引用能够证明某项信息必需时，才能报告缺失。

检查范围：
1. 适用信息：文档或具体功能已经涉及多个对象、版本、场景或用户角色，但没有说明内容适用于谁、什么产品/版本或什么场景。
2. 前提与环境：操作已经给出，但完成操作所必需的设备状态、环境、网络、权限、依赖、准备工作或前置配置没有说明。
3. 操作入口与对象：要求客户执行操作，但没有给出必要的入口、页面、菜单、命令位置、操作对象或选择范围。
4. 步骤与参数：操作过程存在可直接确认的关键动作断点；要求输入、选择或配置但没有给出必要的参数含义、取值、格式、单位或选择依据。
5. 结果与验证：文档要求完成配置、安装、升级、删除、重启或排障，但没有说明完成标志、预期结果或必要的验证方法。
6. 异常与恢复：文档明确涉及可能失败的操作或已经提到异常场景，但没有给出对应处理、回退、恢复或继续执行条件。
7. 限制与风险：文档明确描述删除数据、重启、停机、权限变更、证书/密钥、不可逆操作等风险行为，但没有说明必要限制、影响、备份或注意事项。
8. 术语与字段说明：缩写、专业术语、参数或表格字段是理解和执行的关键，但首次出现或使用时没有必要解释；普通行业通用词不要武断要求解释。
9. 表格与示例要素：从抽取文本能够确认表格或示例存在，但理解数据所必需的字段含义、单位、条件、占位值说明或示例结果缺失。无法确认表格解析顺序时不要报告。
10. 未完成内容：TODO、TBD、XXX、待补充、此处插入、后续提供、见附件/下文但未提供相应内容等明确占位或未闭合引用。

判定方法：
1. 每条问题必须引用能够证明“该信息是必需的”的触发原文，例如已有操作要求、参数输入、风险动作、引用承诺或模板要求。不要编造本应存在的原文。
2. 在 description 中说明“已提供了什么、缺少什么、为什么缺失内容会阻碍客户理解或执行”；在 suggestion 中只说明需要补充的信息类型，不得自行编造参数值、步骤或业务规则。
3. 信息已经提供但表述含糊、难懂属于易理解性问题；信息已提供但错误或前后冲突属于内容正确性问题；错别字和格式问题属于文档规范性问题。
4. 不检查标题层级、目录、章节数量、编号、交叉引用、字体、页面布局、图片内容或链接有效性。不能因为没有看到某个常见章节就直接判为缺失。
5. 如果文档类型、目标读者或任务范围无法确定，并且无法证明某项信息必需，直接忽略，不输出泛化的“建议补充”。

问题类型建议使用：适用范围缺失、前提条件缺失、环境或权限要求缺失、操作入口或对象缺失、关键步骤缺失、参数说明缺失、结果或验证方法缺失、异常或恢复说明缺失、限制或风险说明缺失、术语或字段解释缺失、占位或未完成内容。"""
        + "\n\n"
        + _DOCUMENT_CHECK_OUTPUT_REQUIREMENTS
        + "\n8. 如果未发现明确问题，summary 写“未发现明显内容完整性问题”，items 返回空数组。",
        "sort_order": 25,
    },
    {
        "code": "typo",
        "name": "错别字检查",
        "description": "该检查内容已合并至“文档规范性检查”，默认停用以避免重复报告。",
        "prompt": """你是一名中文校对专家。请检查文档中的错别字、漏字、多字、标点误用、重复表达、常见语病和明显不通顺句子。
注意：文档文本由解析器抽取得到，换行、分页、表格分隔符、行首行尾空白可能与原版版式不同；不要把解析换行/分页造成的空白当作多余空格或标点问题。
定位要求：
1. 每条问题必须给出可定位信息，不要只写问题本身。
2. 位置中优先引用文档文本里的页码标记，例如“[第12页]”；如果文档文本没有页码标记，明确写“页码：未提取”，不要编造页码。
3. 同时给出最近的章节/标题/小节编号、工作表名或表格行线索；如果无法识别章节，写“章节：未识别”，并补充附近短文本作为定位线索。
输出要求：
1. 按条列出：位置（文件/页码/章节或工作表/附近线索）、原文片段、疑似问题、建议修改、理由。
2. 对专业术语、人名、地名、品牌名保持谨慎，不确定时标注“疑似”。
3. 如果未发现明显问题，明确说明“未发现明显错别字或语病”。""",
        "enabled": False,
        "sort_order": 30,
    },
    {
        "code": "sensitive-terms",
        "name": "敏感词检查",
        "description": "根据本地敏感词表检查文档中的不规范用语，并给出对应规范用语。",
        "prompt": """本检查项由系统读取本地敏感词表并进行确定性匹配，不依赖模型提示词判断。
词表应包含“不规范用语”和“规范用语”两列。系统会在文档抽取文本中查找不规范用语，输出命中次数、位置线索、原文片段和建议替换的规范用语。
如果未配置词表或词表格式不正确，报告会提示需要补充本地词表。""",
        "sort_order": 40,
    },
    {
        "code": "common-terms",
        "name": "常用词检查",
        "description": "根据本地常用词检查表核对错误、不推荐用法及大小写不一致问题。",
        "prompt": """本检查项由系统读取本地常用词检查表并进行确定性匹配，不依赖模型提示词判断。
检查表应包含“常用词”和“常见错误/不推荐用法”两列。“常用词”列是唯一正确写法，系统会严格区分大小写；大小写不完全一致、命中常见错误或不推荐用法时，报告会输出命中次数、位置线索、原文片段和正确写法。
检查表可选增加“适用语种”列；留空或填写“全部/all”时适用于所有文档，填写“中文/zh”时仅对中文为主的文档执行。中英混合、语种特征不足或无法识别的文档会跳过仅中文规则。
如果未配置检查表或检查表格式不正确，报告会提示需要补充本地检查表。""",
        "sort_order": 50,
    },
)

DEFAULT_CONSISTENCY_CHECK_ITEMS = (
    {
        "code": "consistency-cross-document",
        "name": "多文档对照检查",
        "description": "以素材文档为依据，检查资料是否存在偏差、遗漏、冲突或缺少依据的说法。",
        "prompt": """你是一名多文档对照审查专家。用户会提供两组内容：素材文档和资料。资料是根据素材文档写作生成的，请以素材文档作为依据，检查资料内容是否与素材内容一致，是否存在偏差、遗漏或需要人工确认的地方。
重点关注：
1. 产品/项目/组织/人名/地点/日期/版本/编号/术语是否一致。
2. 指标、参数、规格、数量、单位、阈值、流程步骤和限制条件是否一致。
3. 资料是否遗漏素材文档中的关键约束，或新增了素材文档没有支撑的说法。
4. 多份资料之间如存在互相冲突，也请标注，但优先说明它们与素材文档的关系。

输出要求：
1. 先给出总体结论，说明一致性风险等级。
2. 按条列出偏差：资料名称、位置线索、资料表述、素材文档依据、偏差说明、修改建议。
3. 对证据不足或需要业务判断的问题标注“需人工确认”。
4. 如果未发现明显偏差，明确说明“未发现资料内容与素材文档存在明显不一致”。不要编造文档中不存在的内容。""",
        "sort_order": 10,
    },
)

DEFAULT_LANGUAGE_CONSISTENCY_CHECK_ITEMS = (
    {
        "code": "language-consistency-cross-lingual",
        "name": "跨语种内容一致性检查",
        "description": "检查两个不同语种文档的内容是否一致，识别缺失、增补、翻译偏差和关键事实差异。",
        "prompt": """你是一名严谨的跨语种文档一致性审查专家。用户会提供两个不同语种或不同语言版本的资料文档，并附带系统静态预检摘要。请综合静态预检线索和两份文档正文，判断两者表达的业务事实、技术要求、步骤、限制条件、风险提示和资料结构是否一致，重点发现缺失、增补、误译、弱化/强化、冲突或需要人工确认的差异。最终报告必须使用中文陈述。

注意：
1. 静态预检摘要只作为优先核对线索，不要仅凭长度、标题数量或抽取要素差异直接下结论。
2. 两种语言的表达顺序、句式、同义改写、合理本地化、单位等价换算、术语常见译法不应误判为不一致。
3. 只依据提供的文档内容判断，不要补充外部事实，不要编造文档中不存在的内容。
4. 对仅存在措辞、标题或结构细微差异，但不影响理解、无实质影响且无需修改的内容，不要作为差异条目输出。

重点关注：
1. 关键主题和章节覆盖：两份文档是否覆盖相同功能、场景、流程、前提条件、适用范围和结论。
2. 缺失与增补：任一文档是否遗漏另一文档中的关键段落、表格字段、步骤、注意事项、安全/法律/合规提示，或新增另一文档没有对应依据的内容。
3. 事实与参数一致性：产品名、版本、型号、日期、编号、数量、单位、阈值、默认值、接口、URL、IP、邮箱、命令、配置项是否一致。
4. 约束强度一致性：must/shall/required/prohibited/optional/recommended 等约束与中文“必须、应、不得、禁止、可、建议”等是否存在强弱变化。
5. 术语与命名一致性：专业术语、功能名、菜单路径、按钮、字段、角色、组织/地点/人名是否保持一致或有合理译名。
6. 结构与引用一致性：章节、图表、附录、步骤编号、交叉引用、链接或附件说明是否对应。

输出要求：
1. 先给出总体结论：两份文档是否基本一致、风险等级（高/中/低/未发现明显风险）和主要差异类型。
2. 按条列出差异：问题类型、位置、文档A证据、文档B证据、差异说明、影响、修改建议。
3. 位置优先包含文件名、页码/章节/标题/表格/步骤/附近文本；无法定位时说明“位置线索不足”。
4. 对证据不足、可能是合理本地化或需要业务确认的内容，明确标注“需人工确认”。
5. 只列出需要修改、需要补充或需要人工确认的实质性差异；不要列出“无实质影响”“影响不大”“无需修改”“无需处理”的条目。
6. 单独概括“缺失内容”和“关键事实/数字差异”；若没有发现，明确说明“未发现明显缺失内容”或“未发现明显关键事实差异”。
7. 如果整体未发现明显差异，明确说明“未发现两份跨语种文档存在明显内容不一致或缺失”。""",
        "sort_order": 10,
    },
)

DEFAULT_VIDEO_CHECK_ITEMS = (
    {
        "code": "video-installation-sequence",
        "name": "安装步骤顺序检查",
        "description": "检查硬件安装视频中的部件安装、固定、接线前后顺序和关键步骤是否合理。",
        "prompt": """你是一名硬件产品安装调测视频质检专家。系统会从视频中按时间轴抽取关键帧，请结合帧顺序判断安装过程是否存在明显风险。
重点关注：
1. 安装顺序是否合理，例如先断电/验电再接线、先固定设备再接线、先检查配件再上电。
2. 是否遗漏关键步骤，例如固定螺钉、接地、线缆整理、端子紧固、防护盖复位、上电前检查。
3. 是否出现明显反向、跳步、重复操作、拆装顺序冲突或与硬件产品常规安装要求不符的操作。
4. 只依据可见视频帧判断；由于视频是抽帧采样，前后连续动作证据不足时标注“需人工确认”。

输出要求：
1. 先给出总体判断。
2. 按条列出问题：时间点、可见证据、问题描述、可能影响、修改建议。
3. 没有明确问题时说明“未发现明显安装步骤顺序问题”。""",
        "sort_order": 10,
    },
    {
        "code": "video-wiring-terminal",
        "name": "接线与端子检查",
        "description": "检查视频中端子、线缆、极性、接地、线序和接线操作是否存在明显风险。",
        "prompt": """你是一名硬件接线与端子操作审查专家。请检查安装调测视频中可见的接线、端子、线缆和接地操作。
重点关注：
1. 电源线、信号线、通信线、接地线是否接到明显正确的位置，是否存在 L/N/PE、正负极、A/B、DI/DO 等可见混接风险。
2. 端子是否有明显未插紧、裸铜外露、压接不牢、屏蔽/接地遗漏、线缆拉扯、线序混乱或防护不足。
3. 工具操作是否可能损伤端子、线缆或外壳。
4. 仅在画面能看清标识和接线关系时判定为明确问题；看不清、被遮挡或缺少图纸依据时标注“需人工确认”。

输出要求：
1. 按条列出：时间点、可见端子/线缆证据、问题描述、可能影响、修改建议。
2. 没有明确问题时说明“未发现明显接线与端子问题”。""",
        "sort_order": 20,
    },
    {
        "code": "video-safety-protection",
        "name": "安全与防护检查",
        "description": "检查安装调测视频中的断电、防护、工具使用、个人安全和设备保护风险。",
        "prompt": """你是一名硬件安装安全质检专家。请检查视频中是否存在安全防护和设备保护方面的明显风险。
重点关注：
1. 是否出现带电接线、上电前未检查、手部接近裸露导体、未复位防护盖、未佩戴必要防护用品等风险。
2. 是否存在工具误用、用力过大、设备跌落/磕碰、线缆被夹压、液体/金属异物靠近设备等风险。
3. 是否缺少安全提醒或关键安全动作无法确认。
4. 抽帧无法证明的连续动作放入“需人工确认”，不要凭空推断。

输出要求：
1. 按条列出：时间点、可见风险、可能后果、建议处理。
2. 没有明确问题时说明“未发现明显安全与防护问题”。""",
        "sort_order": 30,
    },
    {
        "code": "video-commissioning-ui-parameter",
        "name": "调测界面与参数检查",
        "description": "检查调测视频中屏幕、仪表、指示灯、参数配置和验证结果是否存在明显异常。",
        "prompt": """你是一名硬件产品调测过程质检专家。请检查视频中可见的调测界面、仪表读数、指示灯、参数配置和验证结果。
重点关注：
1. 界面/仪表上的关键参数、告警、状态灯、测试结果是否显示异常或与操作目标明显不符。
2. 是否存在未保存配置、未执行验证、测试失败仍继续、告警未处理、指示灯状态异常等问题。
3. 参数值、单位、端口、设备型号、软件界面文字如看不清，应标注“需人工确认”。
4. 不要编造画面中不可见的参数或结果。

输出要求：
1. 按条列出：时间点、界面/仪表证据、问题描述、影响、修改建议。
2. 没有明确问题时说明“未发现明显调测界面与参数问题”。""",
        "sort_order": 40,
    },
    {
        "code": "video-clarity-completeness",
        "name": "视频清晰度与完整性检查",
        "description": "检查视频是否清晰覆盖关键安装调测动作，是否存在遮挡、失焦、跳剪或关键步骤不可见。",
        "prompt": """你是一名安装调测视频质量审查专家。请检查视频帧是否足以支撑用户理解完整安装调测过程。
重点关注：
1. 关键动作是否被手、工具、设备外壳或画面边缘遮挡。
2. 是否存在画面模糊、曝光过暗/过亮、文字过小、镜头抖动、关键端子或界面看不清。
3. 是否疑似缺少关键步骤、跳剪过大、只展示结果不展示过程。
4. 由于视频按帧采样，若需要回看连续片段才能确认，请标注“需人工确认”。

输出要求：
1. 按条列出：时间点、画面问题、影响、拍摄或补录建议。
2. 没有明确问题时说明“未发现明显视频清晰度与完整性问题”。""",
        "sort_order": 50,
    },
)

DEFAULT_IMAGE_CHECK_ITEMS = (
    {
        "code": "image-text-correspondence",
        "name": "图文与界面步骤一致性检查",
        "description": "综合检查图片、界面截图、操作步骤、参数、图号和说明是否与文档上下文一致。",
        "prompt": """你是一名技术资料图文与界面步骤一致性审查专家，主要审查产品用户手册、安装指南、调测指南等文档。请综合文档文本、图片清单、图片位置和本次提供的页面截图或图片内容，检查图片是否与附近文字描述、操作步骤、界面说明、图题图号、表格参数或引用关系一致。
重点关注：
1. 文档描述的产品对象、软件/Web/App/设备界面、菜单路径、页面名称、按钮、页签、字段、参数、状态、告警或调测结果，是否与图片可见内容对应。
2. 文档说明“点击/选择/输入/保存/提交/重启/验证”的对象，是否能在截图中对应看到；截图是否停留在错误页面、旧版界面、不相关页面或缺少关键结果。
3. 操作步骤顺序与截图顺序是否明显冲突，例如先保存后配置、前后截图状态倒置、步骤编号与截图内容不匹配。
4. 图片中的编号、单位、IP、端口号、协议、开关状态、图例、方向或告警文字，是否与附近文字描述冲突。
5. 文档提到必须展示的关键对象或操作结果，但图片没有体现；或图片展示了关键内容，但附近文字没有必要说明。
6. 同一批图片之间如存在步骤顺序、截图界面、图号、标题或内容重复/错位，也请标注。
7. 对只凭图片顺序、文件名或页码无法证明的问题，不要硬判错位，放入“需人工确认”。

输出要求：
1. 先给出总体判断，说明是否发现图文或界面步骤一致性风险。
2. 按条列出问题：图片名称或位置、文档文字/步骤线索、图片可见内容、冲突或缺失说明、建议修改。
3. 将“明确冲突”和“需人工确认”分开描述；对版本差异、截图裁切、文字模糊、上下文不足或需要业务判断的问题标注“需人工确认”。
4. 只有同时看到明确文档线索和图片可见证据时，才判断为“不一致”。
5. 不要仅凭文件名、页码、图片顺序或未提供的上下文推断图片插入错位；证据不足时写“需人工确认”。
6. 如果未发现明显问题，明确说明“未发现明显图文与界面步骤一致性问题”。不要编造文档或图片中不存在的内容。""",
        "sort_order": 10,
    },
    {
        "code": "image-small-language-text",
        "name": "图片语种匹配检查",
        "description": "检查图片、截图、图例和标注中的说明文字语种是否与文档主要语种一致。",
        "prompt": """你是一名图片文字语种一致性审查专家。请先根据提供的文档上下文判断文档主要语种（如中文、英文、中英混排或其他语种），再检查本次图片中可见文字、标注、截图界面、图例和说明的语种是否与文档主要语种一致。
重点关注：
1. 英文文档中图片出现中文说明、中文界面、中文标注等明显不匹配内容。
2. 中文文档中图片出现大段英文或其他语种说明，且文档上下文没有对应语种使用习惯。
3. 多语种文档中，图片文字语种是否超出文档正文、标题或图注使用的语种范围。
4. 不要把产品名、型号、单位、接口名、标准缩写、命令、URL、代码片段、配置项、寄存器名等技术性英文/符号直接判为异常，除非出现大段说明文字语种明显不匹配。
5. 对界面截图中的系统默认英文、第三方库名称或协议字段，仅在影响文档整体语种一致性时标注。

输出要求：
1. 先说明文档主要语种，以及是否发现图片文字语种不匹配。
2. 如发现，逐条列出：图片名称或位置、图片中识别到的文字、图片文字语种、文档主要语种、不匹配原因、建议处理方式。
3. 对看不清、文字过少或无法判断文档主要语种的内容标注“需人工确认”。
4. 如果未发现明显不匹配，明确说明“未发现图片文字语种与文档语种明显不一致”。不要编造图片或文档中不存在的文字。""",
        "sort_order": 20,
    },
    {
        "code": "image-wiring",
        "name": "设备安装与接线检查",
        "description": "检查设备外观、安装方向、端口、附件、端子、极性、线缆颜色、线号和连接关系是否存在明显风险。",
        "prompt": """你是一名产品设备安装与接线审查专家。请结合文档文本、图片位置和本次提供的设备照片、安装图、结构图、接线图或接线照片，检查设备外观、安装方式、端口端子、线缆和连接关系是否与文档说明一致，并标注明显风险。
重点关注：
1. 产品型号、设备正反面、端口/接口/指示灯/按键/拨码/标签位置是否与文档描述或附近图注一致。
2. 安装方向、壁挂/导轨/机柜/桌面安装方式、固定孔位、螺钉、支架、卡扣、接地位置、线缆出线方向是否与步骤说明冲突。
3. 图中展示的配件、工具、线缆、天线、电源、端子或保护件是否与文档列出的物料或安装步骤明显不一致。
4. L/N/PE、+/−、A/B、485+/485−、DI/DO、AI/AO、COM、GND、VCC、电源输入/输出等端子或极性是否与文档说明冲突。
5. 线缆颜色、线号、端子编号、端口名称、屏蔽层、接地符号、跳线或短接关系是否与图中可见标识和文字说明不一致。
6. 接线方向、进出线位置、交叉连接、端子排顺序、设备间连接关系是否存在明显反向、错位或漏接风险。
7. 图片模糊、端子文字过小、线缆被遮挡、缺少现场条件或需要实物规格/BOM/工程设计确认时，标注“需人工确认”。

输出要求：
1. 先给出总体判断，说明是否发现明显设备安装或接线风险。
2. 按条列出问题：图片名称或位置、文档线索、图片可见证据、问题描述、可能影响、建议修改或需核对的依据。
3. 将明确问题和“需人工确认”分开描述。
4. 只依据提供的文本和图片可见内容，不要补全不可见接线，也不要替代专业电气设计审核。
5. 如果未发现明显问题，明确说明“未发现明显设备安装与接线问题”。""",
        "sort_order": 25,
    },
    {
        "code": "image-figure-table-title-standard",
        "name": "图表标题可见性复核",
        "description": "复核页面截图中的图、表附近是否可见“图x-x 标题”“表x-x 标题”等规范标题，并标注疑似缺失项。",
        "prompt": """你是一名技术文档图表标题可见性复核专家。请结合文档上下文、图片位置和本次提供的页面截图，复核图、表、流程图、设备图、界面截图或表格附近是否可见规范标题。
正确形式示例：
1. 图标题：类似“图3-2 iIOT-WEC04C5网关外观（02314WHE）”，通常包含“图”+章节编号/序号+标题文字。
2. 表标题：类似“表3-1 IoT网关型号介绍”，通常包含“表”+章节编号/序号+标题文字。

复核步骤：
1. 先逐项识别当前页面中可见的图示、设备图、流程图、界面截图、表格或跨页续表区域。
2. 对每个对象查找其上方、下方或相邻文档文本中是否可见“图/表+编号+标题文字”。
3. 章节标题、页眉、文档名、页码、版权信息、正文段落、步骤说明、红框标注或空白占位不能直接替代图表标题。
4. 如果对象附近只有章节标题（如“7.4.1 App 开站”）但没有“图x-x/表x-x 标题”，列为“疑似标题缺失”；请明确写出“章节标题不能替代图表标题”。
5. 表格位于页眉/文档名/页码下方，紧接着就是表格边框和表头，但未见“表x-x 标题”或“续表x-x 标题”时，列为“疑似表标题缺失”；请明确写出“页眉或文档名不能替代表标题”。
6. 跨页续表、截图裁切不完整、标题可能在上一页/下一页或 OCR 看不清时，标注“需人工确认”，不要直接判为正常。
7. 如果标题只有编号没有标题文字，或只有标题文字没有“图/表+编号”，列为“标题不完整”或“需人工确认”。

输出要求：
1. 先给出总体判断，说明是否发现疑似图表标题缺失、标题不完整或需要人工确认的对象。
2. 按条列出：图片名称或位置、对象类型（图/表/截图/流程图等）、可见内容线索、附近可见标题文字、问题判断、建议补充的标题形式。
3. 对证据不足的问题单独列入“需人工确认”。
4. 如果未发现明显问题，明确说明“未发现明显图表标题可见性问题”。不要编造图片或文档中不存在的标题。""",
        "sort_order": 30,
    },
    {
        "code": "image-integrity-clarity",
        "name": "图片完整性和清晰度检查",
        "description": "检查图片是否裁切、遮挡、坏图、模糊、低分辨率、拉伸变形，关键文字或线条是否可读。",
        "prompt": """你是一名技术文档图片质量审查专家。请结合文档上下文、图片位置和本次提供的图片内容，检查图片的完整性和清晰度是否满足用户手册、安装指南、调测指南的发布要求。
完整性重点关注：
1. 图片只显示一部分，主体被裁切，边缘内容缺失，或明显超出页面/截图边界。
2. 图片被文字、图形、浮层、页眉页脚、遮罩或其他对象遮挡、覆盖。
3. 图片出现异常红块、白块、黑块、灰块、马赛克块、空白块、坏图占位、渲染失败区域或颜色异常色块。
4. 图片内容被错误叠加、重影、错位，导致主体不可辨认或信息缺失。
5. 表格、流程图、接线图或界面截图缺少关键行列、步骤、箭头、端子、按钮、字段或结果区域。

清晰度重点关注：
1. 图片模糊、失焦、文字/线条不可读。
2. 分辨率过低，放大后锯齿明显，关键标注、端子号、菜单名、按钮名、字段值、单位或图例无法辨认。
3. 过度拉伸、压缩、比例变形，设备外观、图形元素或文字形状明显失真。
4. 压缩痕迹、噪点、色带、块状失真严重影响阅读。

输出要求：
1. 先给出总体判断，说明是否发现图片完整性或清晰度风险。
2. 按条列出问题：图片名称或位置、问题类型（完整性/清晰度）、可见线索、影响、建议处理方式。
3. 对图片本身分辨率不足、无法判断是否由截图造成、或需要原始文件核对的情况标注“需人工确认”。
4. 如果未发现明显问题，明确说明“未发现明显图片完整性和清晰度问题”。不要编造图片中不存在的缺陷。""",
        "sort_order": 35,
    },
    {
        "code": "image-drawing-standard",
        "name": "图片画图规范检查",
        "description": "检查流程图、结构图、示意图、接线图和截图标注是否存在明显表达不清、方向错误、编号混乱或图例缺失。",
        "prompt": """你是一名技术图示表达规范审查专家。请结合文档文本、图片位置和本次提供的图片内容，检查流程图、结构图、示意图、接线图、安装示意图或截图标注是否存在明显表达问题。
重点关注：
1. 流程箭头方向、步骤编号、分支关系、输入输出、开始/结束节点是否清楚，是否存在明显断线、反向、遗漏或闭环不明。
2. 结构图、安装图、接线图中的方向、比例、层级、对齐、标注引线、符号图例、单位、编号是否容易误读。
3. 同一图片内的图例、颜色、编号、端口、参数单位是否前后不一致，或与文档文字明显冲突。
4. 截图上的红框、箭头、圈注、序号是否准确指向操作对象，是否遮挡关键文字。
5. 行业标准或企业制图规范无法从图片直接判断时，标注“需人工确认”，不要泛泛套用外部标准。

输出要求：
1. 先概括图片是否存在明显画图规范风险。
2. 按条列出：图片名称或位置、文档线索、问题描述、影响、修改建议。
3. 对行业标准或业务口径不明确的问题标注“需人工确认”。
4. 如果未发现明显问题，明确说明“未发现明显画图规范问题”。""",
        "sort_order": 40,
    },
)

DEFAULT_CHECK_ITEMS = tuple(
    {**item, "task_type": DOCUMENT_TASK_TYPE}
    for item in DEFAULT_DOCUMENT_CHECK_ITEMS
) + tuple(
    {**item, "task_type": CONSISTENCY_TASK_TYPE}
    for item in DEFAULT_CONSISTENCY_CHECK_ITEMS
) + tuple(
    {**item, "task_type": LANGUAGE_CONSISTENCY_TASK_TYPE}
    for item in DEFAULT_LANGUAGE_CONSISTENCY_CHECK_ITEMS
) + tuple(
    {**item, "task_type": VIDEO_TASK_TYPE}
    for item in DEFAULT_VIDEO_CHECK_ITEMS
) + tuple(
    {**item, "task_type": IMAGE_TASK_TYPE}
    for item in DEFAULT_IMAGE_CHECK_ITEMS
)
DEFAULT_CHECK_ITEMS_BY_CODE = {item["code"]: item for item in DEFAULT_CHECK_ITEMS}
_IMAGE_LANGUAGE_MATCH_CODE = "image-small-language-text"
_REMOVED_DEFAULT_CHECK_ITEM_CODES = ("consistency-translation-coverage",)
_TYPO_LOCATION_PROMPT_MARKERS = (
    "按条列出：原文片段、疑似问题、建议修改、理由",
    "未发现明显错别字或语病",
)
_COMPLIANCE_PROMPT_MARKERS = (
    "标题层级、章节结构、编号、术语、格式表达、引用说明",
    "先给出总体结论",
    "未发现明显规范性问题",
)
_COMPLIANCE_STOCK_PROMPT_MARKER_SETS = (
    _COMPLIANCE_PROMPT_MARKERS,
    (
        "客户资料规范审查专家兼资深技术文档编辑",
        "结构与层级",
        "未发现明显客户资料规范性问题",
    ),
    (
        "资深技术文档规范审查专家，熟悉面向客户资料的语言规范、结构规范",
        "10. 版本、版权和发布信息规范",
        "未发现明显规范性问题",
    ),
)
_CONSISTENCY_PROMPT_MARKERS = (
    "包括但不限于人名/组织名、项目名、日期、金额、数量、单位",
    "先概括一致性风险等级",
    "建议统一口径",
)
_CONSISTENCY_STOCK_PROMPT_MARKER_SETS = (
    _CONSISTENCY_PROMPT_MARKERS,
    (
        "严谨的文档审查专家兼资深技术文档编辑",
        "约束性与安全信息一致性",
        "未发现明显全文一致性问题",
    ),
    (
        "你是一个严谨的文档审查专家兼资深技术文档编辑",
        "检查每个章节标题是否准确概括其正文内容",
        "未发现明显不一致问题",
    ),
)
_TYPO_STOCK_PROMPT_MARKER_SETS = (
    _TYPO_LOCATION_PROMPT_MARKERS,
    (
        "你是一名中文校对专家",
        "位置（文件/页码/章节或工作表/附近线索）",
        "未发现明显错别字或语病",
    ),
)
_LANGUAGE_CONSISTENCY_PROMPT_MARKERS = (
    "最终报告必须使用中文陈述",
    "静态预检摘要只作为优先核对线索",
    "单独概括“缺失内容”和“关键事实/数字差异”",
)
_LEGACY_IMAGE_LANGUAGE_MARKERS = ("小语种", "非中文、非英文")
_QWEN_VL_OPTIMIZED_IMAGE_PROMPT_MARKERS = {
    "image-text-correspondence": ("图文一致性审查专家",),
    "image-wiring": ("电气接线图和设备接线审查专家",),
    "image-figure-table-title-standard": ("必须判为表标题缺失", "同一张图片中可能同时出现"),
    "image-integrity-clarity": ("异常红块", "过度拉伸"),
    "image-drawing-standard": ("技术制图和图示规范审查专家", "线型线宽"),
}
_MERGED_IMAGE_CHECK_ITEM_MARKERS = {
    "image-ui-step-consistency": {
        "name": "界面截图与步骤一致性检查",
        "markers": ("产品界面截图与操作步骤审查专家",),
    },
    "image-device-installation": {
        "name": "设备外观与安装图检查",
        "markers": ("产品设备外观与安装图审查专家",),
    },
}


def default_check_item_codes(task_type: str | None = None) -> set[str]:
    return {
        item["code"]
        for item in DEFAULT_CHECK_ITEMS
        if task_type is None or item["task_type"] == task_type
    }


def reset_default_check_item_prompt(item_id: int) -> bool:
    db = get_db()
    row = db.execute("SELECT code FROM check_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return False
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get(row["code"])
    if default_item is None:
        return False
    db.execute(
        "UPDATE check_items SET prompt = ?, updated_at = ? WHERE id = ?",
        (default_item["prompt"], now_text(), item_id),
    )
    db.commit()
    return True


def seed_defaults():
    db = get_db()
    now = now_text()

    _migrate_task_file_retention_setting(db, now)

    defaults = {
        "global_concurrency": 3,
        "user_concurrency": 1,
        "check_item_concurrency": 1,
        "image_check_batch_size": 4,
        "image_page_check_max_pages": 120,
        "issue_output_limit": DEFAULT_ISSUE_OUTPUT_LIMIT,
        "task_file_retention_days": 0,
        "llm_stream_trace_enabled": False,
    }
    for key, value in defaults.items():
        exists = db.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
        if exists is None:
            db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    issue_limit_row = db.execute("SELECT value FROM settings WHERE key = 'issue_output_limit'").fetchone()
    if issue_limit_row is not None:
        try:
            stored_issue_limit = json.loads(issue_limit_row["value"])
        except (TypeError, json.JSONDecodeError):
            stored_issue_limit = None
        normalized_issue_limit = normalize_issue_output_limit(stored_issue_limit)
        if stored_issue_limit != normalized_issue_limit:
            db.execute(
                "UPDATE settings SET value = ?, updated_at = ? WHERE key = 'issue_output_limit'",
                (json.dumps(normalized_issue_limit), now),
            )

    for item in DEFAULT_CHECK_ITEMS:
        exists = db.execute("SELECT 1 FROM check_items WHERE code = ?", (item["code"],)).fetchone()
        if exists is None:
            db.execute(
                """
                INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["task_type"],
                    item["code"],
                    item["name"],
                    item["description"],
                    item["prompt"],
                    1 if item.get("enabled", True) else 0,
                    item["sort_order"],
                    now,
                    now,
                ),
            )

    _sync_renamed_default_check_items(db, now)
    _sync_compliance_prompt(db, now)
    _sync_typo_location_prompt(db, now)
    _sync_consistency_prompt(db, now)
    _disable_merged_typo_check_item(db, now)
    _sync_language_consistency_prompt(db, now)
    _sync_qwen_vl_optimized_image_check_items(db, now)
    _disable_merged_image_check_items(db, now)
    _remove_retired_default_check_items(db)
    db.commit()


def _migrate_task_file_retention_setting(db, updated_at: str):
    current = db.execute(
        "SELECT 1 FROM settings WHERE key = 'task_file_retention_days'"
    ).fetchone()
    legacy = db.execute(
        "SELECT value FROM settings WHERE key = 'report_retention_days'"
    ).fetchone()
    if current is None and legacy is not None:
        db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES ('task_file_retention_days', ?, ?)",
            (legacy["value"], updated_at),
        )
    if legacy is not None:
        db.execute("DELETE FROM settings WHERE key = 'report_retention_days'")


def _remove_retired_default_check_items(db):
    for code in _REMOVED_DEFAULT_CHECK_ITEM_CODES:
        db.execute("DELETE FROM check_items WHERE code = ?", (code,))


def _sync_compliance_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("compliance")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'compliance'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_stock_prompt = (
        prompt == default_item["prompt"]
        or any(
            all(marker in prompt for marker in markers)
            for markers in _COMPLIANCE_STOCK_PROMPT_MARKER_SETS
        )
    )
    if not is_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'compliance'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_typo_location_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("typo")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'typo'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_legacy_stock_prompt = (
        all(marker in prompt for marker in _TYPO_LOCATION_PROMPT_MARKERS)
        and "页码：未提取" not in prompt
    )
    if not is_legacy_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'typo'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_consistency_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("consistency")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'consistency'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_stock_prompt = (
        prompt == default_item["prompt"]
        or any(
            all(marker in prompt for marker in markers)
            for markers in _CONSISTENCY_STOCK_PROMPT_MARKER_SETS
        )
    )
    if not is_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'consistency'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _disable_merged_typo_check_item(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("typo")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, enabled, sort_order FROM check_items WHERE code = 'typo'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_stock_item = (
        prompt == default_item["prompt"]
        or any(
            all(marker in prompt for marker in markers)
            for markers in _TYPO_STOCK_PROMPT_MARKER_SETS
        )
    )
    if not is_stock_item:
        return
    next_enabled = 1 if default_item.get("enabled", True) else 0
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["enabled"] or 0) == next_enabled
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            enabled = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'typo'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            next_enabled,
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_language_consistency_prompt(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get("language-consistency-cross-lingual")
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = 'language-consistency-cross-lingual'"
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    is_legacy_stock_prompt = (
        all(marker in prompt for marker in _LANGUAGE_CONSISTENCY_PROMPT_MARKERS)
        and "无实质影响" not in prompt
        and "无需修改" not in prompt
    )
    if not is_legacy_stock_prompt:
        return
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and prompt == default_item["prompt"]
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = 'language-consistency-cross-lingual'
        """,
        (
            default_item["name"],
            default_item["description"],
            default_item["prompt"],
            default_item["sort_order"],
            updated_at,
        ),
    )


def _sync_renamed_default_check_items(db, updated_at: str):
    default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get(_IMAGE_LANGUAGE_MATCH_CODE)
    if default_item is None:
        return
    row = db.execute(
        "SELECT name, description, prompt, sort_order FROM check_items WHERE code = ?",
        (_IMAGE_LANGUAGE_MATCH_CODE,),
    ).fetchone()
    if row is None:
        return
    prompt = str(row["prompt"] or "")
    should_update_prompt = any(marker in prompt for marker in _LEGACY_IMAGE_LANGUAGE_MARKERS)
    next_prompt = default_item["prompt"] if should_update_prompt else prompt
    if (
        row["name"] == default_item["name"]
        and (row["description"] or "") == default_item["description"]
        and next_prompt == prompt
        and int(row["sort_order"] or 0) == int(default_item["sort_order"])
    ):
        return
    db.execute(
        """
        UPDATE check_items
        SET name = ?,
            description = ?,
            prompt = ?,
            sort_order = ?,
            updated_at = ?
        WHERE code = ?
        """,
        (
            default_item["name"],
            default_item["description"],
            next_prompt,
            default_item["sort_order"],
            updated_at,
            _IMAGE_LANGUAGE_MATCH_CODE,
        ),
    )


def _sync_qwen_vl_optimized_image_check_items(db, updated_at: str):
    for code, markers in _QWEN_VL_OPTIMIZED_IMAGE_PROMPT_MARKERS.items():
        default_item = DEFAULT_CHECK_ITEMS_BY_CODE.get(code)
        if default_item is None:
            continue
        row = db.execute(
            "SELECT name, description, prompt, sort_order FROM check_items WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            continue
        prompt = str(row["prompt"] or "")
        is_stock_prompt = prompt == default_item["prompt"] or all(marker in prompt for marker in markers)
        if not is_stock_prompt:
            continue
        if (
            row["name"] == default_item["name"]
            and (row["description"] or "") == default_item["description"]
            and prompt == default_item["prompt"]
            and int(row["sort_order"] or 0) == int(default_item["sort_order"])
        ):
            continue
        db.execute(
            """
            UPDATE check_items
            SET name = ?,
                description = ?,
                prompt = ?,
                sort_order = ?,
                updated_at = ?
            WHERE code = ?
            """,
            (
                default_item["name"],
                default_item["description"],
                default_item["prompt"],
                default_item["sort_order"],
                updated_at,
                code,
            ),
        )


def _disable_merged_image_check_items(db, updated_at: str):
    for code, legacy in _MERGED_IMAGE_CHECK_ITEM_MARKERS.items():
        row = db.execute(
            "SELECT name, prompt, enabled FROM check_items WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            continue
        prompt = str(row["prompt"] or "")
        is_stock_item = all(marker in prompt for marker in legacy["markers"])
        if not is_stock_item or not int(row["enabled"] or 0):
            continue
        db.execute(
            "UPDATE check_items SET enabled = 0, updated_at = ? WHERE code = ?",
            (updated_at, code),
        )
