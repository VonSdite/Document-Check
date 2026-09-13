import json

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .auth import (
    AuthenticationRequired,
    UserIdentity,
    current_identity,
)
from .db import get_db, now_text
from .llm import normalize_reasoning_effort

PROVIDER_TIMEOUT_DEFAULT = 3600
PROVIDER_TIMEOUT_MIN = 30
PROVIDER_TIMEOUT_MAX = 7200
PROVIDER_INPUT_LIMIT_DEFAULT = 500000
PROVIDER_INPUT_LIMIT_MIN = 5000
PROVIDER_INPUT_LIMIT_MAX = 1000000


def _form_bool(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _platform_enabled() -> bool:
    return bool(current_app.config.get("PLATFORM", True))


def _current_user_identity() -> UserIdentity:
    try:
        return current_identity(require_sso=True)
    except AuthenticationRequired:
        abort(401, description="未收到 SSO 用户信息，请通过公司统一入口访问。")


def _model_page_identity() -> UserIdentity:
    if _platform_enabled():
        return _current_user_identity()
    return current_identity()


def _model_management_response(identity: UserIdentity, redirect_endpoint: str):
    if request.method == "POST":
        action = request.form.get("action", "save")
        provider_id = request.form.get("provider_id")
        if action == "delete" and provider_id:
            _delete_user_model_provider(identity.subject, provider_id)
            flash("模型提供商已删除。", "success")
            return redirect(url_for(redirect_endpoint))

        provider_data = _provider_form_data()
        if isinstance(provider_data, str):
            flash(provider_data, "error")
            return redirect(url_for(redirect_endpoint))

        if provider_id and not _user_provider_exists(identity.subject, provider_id):
            flash("模型提供商不存在。", "error")
            return redirect(url_for(redirect_endpoint))

        _save_user_model_provider(identity.subject, provider_id, provider_data)
        flash("模型提供商已保存。", "success")
        return redirect(url_for(redirect_endpoint))

    providers = _load_user_model_providers(identity.subject)
    models_by_provider = {
        provider["id"]: sorted(
            _provider_model_options(provider),
            key=lambda model: (model["model_name"], model["force_disable_thinking"]),
        )
        for provider in providers
    }
    return render_template(
        "user_models.html",
        providers=providers,
        models_by_provider=models_by_provider,
        active_nav="models",
    )


def _provider_form_data() -> dict | str:
    return _normalize_provider_input(
        {
            "name": request.form.get("name", ""),
            "api_base": request.form.get("api_base", ""),
            "api_key": request.form.get("api_key", ""),
            "request_timeout": request.form.get(
                "request_timeout", str(PROVIDER_TIMEOUT_DEFAULT)
            ),
            "max_input_chars": request.form.get(
                "max_input_chars", str(PROVIDER_INPUT_LIMIT_DEFAULT)
            ),
            "is_active": request.form.get("is_active") == "on",
            "models": _parse_model_configs(
                request.form.get("model_configs", ""),
                request.form.get("models", ""),
            ),
        },
        require_models=True,
    )


def _provider_connection_data(data: dict, name: str) -> dict | str:
    return _normalize_provider_input(
        {
            "name": name,
            "api_base": data.get("api_base", ""),
            "api_key": data.get("api_key", ""),
            "request_timeout": data.get(
                "request_timeout", str(PROVIDER_TIMEOUT_DEFAULT)
            ),
            "max_input_chars": str(PROVIDER_INPUT_LIMIT_DEFAULT),
            "is_active": True,
            "models": [{"model_name": "placeholder", "force_disable_thinking": False}],
        },
        require_models=False,
    )


def _normalize_provider_input(value: dict, *, require_models: bool) -> dict | str:
    name = str(value.get("name") or "").strip()
    api_base = str(value.get("api_base") or "").strip().rstrip("/")
    api_key = str(value.get("api_key") or "").strip()
    if not name or not api_base:
        return "提供商名称和 API 地址不能为空。"
    if not _is_chat_completions_endpoint(api_base):
        return "API 地址必须填写完整的 /chat/completions 请求地址。"
    try:
        request_timeout = int(value.get("request_timeout") or PROVIDER_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        return "超时时间必须是整数秒。"
    try:
        max_input_chars = int(
            value.get("max_input_chars") or PROVIDER_INPUT_LIMIT_DEFAULT
        )
    except (TypeError, ValueError):
        return "文本上限必须是整数。"
    if request_timeout < PROVIDER_TIMEOUT_MIN or request_timeout > PROVIDER_TIMEOUT_MAX:
        return f"超时时间需在 {PROVIDER_TIMEOUT_MIN}-{PROVIDER_TIMEOUT_MAX} 秒之间。"
    if (
        max_input_chars < PROVIDER_INPUT_LIMIT_MIN
        or max_input_chars > PROVIDER_INPUT_LIMIT_MAX
    ):
        return f"文本上限需在 {PROVIDER_INPUT_LIMIT_MIN}-{PROVIDER_INPUT_LIMIT_MAX} 字之间。"
    model_configs = value.get("models") or []
    if require_models and not model_configs:
        return "至少需要填写一个模型 ID。"
    return {
        "name": name,
        "api_base": api_base,
        "api_key": api_key,
        "request_timeout": request_timeout,
        "max_input_chars": max_input_chars,
        "is_active": bool(value.get("is_active")),
        "models": model_configs,
    }


def _load_user_model_providers(owner_subject: str) -> list[dict]:
    rows = (
        get_db()
        .execute(
            """
        SELECT *
        FROM user_model_providers
        WHERE owner_subject = ?
        ORDER BY updated_at DESC, id DESC
        """,
            (owner_subject,),
        )
        .fetchall()
    )
    return [
        _provider_from_row(row, _load_user_model_configs(row["id"])) for row in rows
    ]


def _load_user_model_configs(provider_id: int) -> list[dict]:
    rows = (
        get_db()
        .execute(
            """
        SELECT model_name, force_disable_thinking, reasoning_effort
        FROM user_model_configs
        WHERE provider_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
            (provider_id,),
        )
        .fetchall()
    )
    return [
        {
            "model_name": row["model_name"],
            "force_disable_thinking": bool(row["force_disable_thinking"]),
            "reasoning_effort": normalize_reasoning_effort(row["reasoning_effort"])
            or "",
        }
        for row in rows
    ]


def _provider_from_row(row, models: list[dict]) -> dict:
    return {
        "id": row["id"],
        "owner_subject": row["owner_subject"],
        "name": row["name"],
        "api_base": row["api_base"],
        "api_key": row["api_key"] or "",
        "request_timeout": row["request_timeout"],
        "max_input_chars": row["max_input_chars"],
        "is_active": bool(row["is_active"]),
        "models": models,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _user_provider_exists(owner_subject: str, provider_id) -> bool:
    return (
        get_db()
        .execute(
            "SELECT 1 FROM user_model_providers WHERE id = ? AND owner_subject = ?",
            (provider_id, owner_subject),
        )
        .fetchone()
        is not None
    )


def _save_user_model_provider(owner_subject: str, provider_id, provider_data: dict):
    db = get_db()
    now = now_text()
    if provider_id:
        db.execute(
            """
            UPDATE user_model_providers
            SET name = ?, api_base = ?, api_key = ?,
                request_timeout = ?, max_input_chars = ?, is_active = ?, updated_at = ?
            WHERE id = ? AND owner_subject = ?
            """,
            (
                provider_data["name"],
                provider_data["api_base"],
                provider_data["api_key"],
                provider_data["request_timeout"],
                provider_data["max_input_chars"],
                1 if provider_data["is_active"] else 0,
                now,
                provider_id,
                owner_subject,
            ),
        )
        saved_provider_id = int(provider_id)
    else:
        cursor = db.execute(
            """
            INSERT INTO user_model_providers(
                owner_subject, name, api_base, api_key,
                request_timeout, max_input_chars, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_subject,
                provider_data["name"],
                provider_data["api_base"],
                provider_data["api_key"],
                provider_data["request_timeout"],
                provider_data["max_input_chars"],
                1 if provider_data["is_active"] else 0,
                now,
                now,
            ),
        )
        saved_provider_id = cursor.lastrowid
        if saved_provider_id is None:
            raise RuntimeError("模型提供商保存失败，请稍后重试。")
    _replace_user_model_configs(saved_provider_id, provider_data["models"], now)
    db.commit()


def _replace_user_model_configs(
    provider_id: int, model_configs: list[dict], updated_at: str
):
    db = get_db()
    db.execute("DELETE FROM user_model_configs WHERE provider_id = ?", (provider_id,))
    for index, model_config in enumerate(model_configs, start=1):
        db.execute(
            """
            INSERT INTO user_model_configs(
                provider_id, model_name, force_disable_thinking, reasoning_effort,
                sort_order, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                model_config["model_name"],
                1 if model_config["force_disable_thinking"] else 0,
                normalize_reasoning_effort(model_config.get("reasoning_effort")),
                index * 10,
                updated_at,
                updated_at,
            ),
        )


def _delete_user_model_provider(owner_subject: str, provider_id):
    get_db().execute(
        "DELETE FROM user_model_providers WHERE id = ? AND owner_subject = ?",
        (provider_id, owner_subject),
    )
    get_db().commit()


def _parse_model_configs(model_configs_json: str, models_text: str = "") -> list[dict]:
    configs = []
    try:
        value = json.loads(model_configs_json) if model_configs_json else []
    except json.JSONDecodeError:
        value = []

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                model_name = str(item.get("model_name") or item.get("id") or "").strip()
                force_disable_thinking = _form_bool(
                    item.get("force_disable_thinking", False)
                )
                reasoning_effort = (
                    normalize_reasoning_effort(item.get("reasoning_effort")) or ""
                )
            else:
                model_name = str(item or "").strip()
                force_disable_thinking = False
                reasoning_effort = ""
            configs.append(
                {
                    "model_name": model_name,
                    "force_disable_thinking": force_disable_thinking,
                    "reasoning_effort": reasoning_effort,
                }
            )

    if not configs:
        configs = [
            {
                "model_name": line.strip(),
                "force_disable_thinking": False,
                "reasoning_effort": "",
            }
            for line in str(models_text or "").splitlines()
            if line.strip()
        ]

    result = []
    seen = set()
    for config in configs:
        model_name = str(config.get("model_name") or "").strip()
        force_disable_thinking = bool(config.get("force_disable_thinking"))
        key = (model_name, force_disable_thinking)
        if not model_name or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "model_name": model_name,
                "force_disable_thinking": force_disable_thinking,
                "reasoning_effort": normalize_reasoning_effort(
                    config.get("reasoning_effort")
                )
                or "",
            }
        )
    return result


def _provider_model_options(provider: dict) -> list[dict]:
    return [
        {
            "model_name": _model_config_name(model_config),
            "force_disable_thinking": _model_config_force_disable_thinking(
                model_config
            ),
            "reasoning_effort": _model_config_reasoning_effort(model_config),
            "enabled": True,
        }
        for model_config in provider["models"]
        if _model_config_name(model_config)
    ]


def _model_config_name(model_config) -> str:
    if isinstance(model_config, dict):
        return str(
            model_config.get("model_name") or model_config.get("id") or ""
        ).strip()
    return str(model_config or "").strip()


def _model_config_force_disable_thinking(model_config) -> bool:
    if not isinstance(model_config, dict):
        return False
    return _form_bool(model_config.get("force_disable_thinking", False))


def _model_config_reasoning_effort(model_config) -> str:
    if not isinstance(model_config, dict):
        return ""
    return normalize_reasoning_effort(model_config.get("reasoning_effort")) or ""


def get_enabled_models(owner_subject: str | None = None):
    if owner_subject is None:
        owner_subject = current_identity().subject
    models = []
    for provider in _load_user_model_providers(owner_subject):
        if not provider["is_active"]:
            continue
        for model_config in provider["models"]:
            models.append(_model_option(provider, model_config))
    return sorted(
        models,
        key=lambda model: (
            model["provider_name"],
            model["model_name"],
            model["force_disable_thinking"],
        ),
    )


def _model_option(provider: dict, model_name) -> dict:
    if isinstance(model_name, dict):
        model_config = model_name
        model_name = str(
            model_config.get("model_name") or model_config.get("id") or ""
        ).strip()
        force_disable_thinking = bool(model_config.get("force_disable_thinking"))
        reasoning_effort = _model_config_reasoning_effort(model_config)
    else:
        model_name = str(model_name or "").strip()
        force_disable_thinking = False
        reasoning_effort = ""
    return {
        "id": f"{provider['id']}:{1 if force_disable_thinking else 0}:{model_name}",
        "provider_id": provider["id"],
        "provider_name": provider["name"],
        "model_name": model_name,
        "force_disable_thinking": force_disable_thinking,
        "reasoning_effort": reasoning_effort,
        "api_base": provider["api_base"],
        "api_key": provider["api_key"],
        "request_timeout": provider["request_timeout"],
        "max_input_chars": provider["max_input_chars"],
    }


def _is_chat_completions_endpoint(value: str) -> bool:
    endpoint = str(value or "").strip().rstrip("/")
    return endpoint.startswith(("http://", "https://")) and endpoint.endswith(
        "/chat/completions"
    )


def _find_enabled_model(model_id: str, owner_subject: str | None = None) -> dict | None:
    if ":" not in model_id:
        return None
    if owner_subject is None:
        owner_subject = current_identity().subject
    force_disable_thinking = None
    parts = model_id.split(":", 2)
    if len(parts) == 3 and parts[1] in {"0", "1"}:
        provider_id, thinking_flag, model_name = parts
        force_disable_thinking = thinking_flag == "1"
    else:
        provider_id, model_name = model_id.split(":", 1)
    for provider in _load_user_model_providers(owner_subject):
        if str(provider["id"]) != str(provider_id) or not provider["is_active"]:
            continue
        for model_config in provider["models"]:
            option = _model_option(provider, model_config)
            if option["model_name"] == model_name and (
                force_disable_thinking is None
                or option["force_disable_thinking"] == force_disable_thinking
            ):
                return option
        return None
    return None
