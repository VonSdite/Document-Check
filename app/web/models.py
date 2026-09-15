from flask import flash, redirect, render_template, request, url_for

from app.identity.service import UserIdentity
from app.infrastructure.network import outbound_network_config
from app.models.client import (
    LLMError,
    normalize_reasoning_effort,
    test_model_connection,
)
from app.models.discovery import ModelDiscoveryError, fetch_models
from app.models.service import (
    PROVIDER_INPUT_LIMIT_DEFAULT,
    PROVIDER_TIMEOUT_DEFAULT,
    _delete_user_model_provider,
    _load_user_model_providers,
    _normalize_provider_input,
    _parse_model_configs,
    _provider_connection_data,
    _provider_model_options,
    _save_user_model_provider,
    _user_provider_exists,
)
from app.web.auth import (
    _current_user_identity,
    admin_required,
)
from app.web.common import _form_bool
from app.web.constants import MODEL_TEST_TIMEOUT_MAX


def register_models_routes(app):
    admin_prefix = app.config["ADMIN_URL"]

    @app.route("/models", methods=["GET", "POST"])
    def user_models():
        return _model_management_response(_current_user_identity(), "user_models")

    @app.post("/models/fetch")
    def user_fetch_models():
        _current_user_identity()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return {"error": "请求数据格式不正确。"}, 400
        provider_data = _provider_connection_data(data, "模型拉取")
        if isinstance(provider_data, str):
            return {"error": provider_data}, 400
        network = outbound_network_config()
        try:
            models = fetch_models(
                api_base=provider_data["api_base"],
                api_key=provider_data["api_key"],
                proxy_mode=network["proxy_mode"],
                proxy=network["proxy"],
                ssl_verify=network["ssl_verify"],
                request_timeout=provider_data["request_timeout"],
            )
        except ModelDiscoveryError as exc:
            return {"error": str(exc)}, 400
        return {"fetched_models": models, "fetched_count": len(models)}

    @app.post("/models/test")
    def user_test_model():
        _current_user_identity()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return {"ok": False, "error": "请求数据格式不正确。"}, 400
        provider_data = _provider_connection_data(data, "模型测试")
        if isinstance(provider_data, str):
            return {"ok": False, "error": provider_data}, 400
        model_name = str(data.get("model_name") or "").strip()
        if not model_name:
            return {"ok": False, "error": "请先填写模型 ID。"}, 400
        network = outbound_network_config()
        try:
            message = test_model_connection(
                api_base=provider_data["api_base"],
                api_key=provider_data["api_key"],
                proxy_mode=network["proxy_mode"],
                proxy=network["proxy"],
                ssl_verify=network["ssl_verify"],
                request_timeout=min(
                    provider_data["request_timeout"], MODEL_TEST_TIMEOUT_MAX
                ),
                model_name=model_name,
                reasoning_effort=normalize_reasoning_effort(
                    data.get("reasoning_effort")
                ),
                force_disable_thinking=_form_bool(data.get("force_disable_thinking")),
            )
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "message": message}

    @app.route(f"{admin_prefix}/models", methods=["GET", "POST"])
    @admin_required
    def admin_models():
        return _model_management_response(_current_user_identity(), "admin_models")


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
