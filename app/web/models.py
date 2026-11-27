from flask import flash, redirect, render_template, request, url_for

from app.identity.service import UserIdentity
from app.infrastructure.network import outbound_network_config
from app.models.client import (
    LLMError,
    normalize_reasoning_effort,
    test_model_connection,
)
from app.models.discovery import ModelDiscoveryError, fetch_models
from app.models.image_capabilities import (
    image_capabilities,
    image_model_status,
    image_test_challenge,
    record_image_capability,
)
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
from app.models.timeouts import MODEL_CONNECT_TIMEOUT
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
    @app.post("/models/test-image", endpoint="user_test_image_model")
    def user_test_model():
        identity = _current_user_identity()
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
        image_test = request.path.endswith("/test-image")
        model = {
            **provider_data,
            "model_name": model_name,
            "reasoning_effort": normalize_reasoning_effort(
                data.get("reasoning_effort")
            ),
            "force_disable_thinking": _form_bool(data.get("force_disable_thinking")),
        }
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
                **({"image_challenge": image_test_challenge()} if image_test else {}),
            )
        except LLMError as exc:
            if image_test:
                record_image_capability(identity.subject, model, verified=False)
            return {"ok": False, "error": str(exc)}, 400
        if image_test:
            record_image_capability(identity.subject, model, verified=True)
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
    capabilities = image_capabilities(identity.subject)
    for provider in providers:
        for model in models_by_provider[provider["id"]]:
            model["image_input_status"] = image_model_status(
                {**provider, **model}, capabilities
            )
    return render_template(
        "user_models.html",
        providers=providers,
        models_by_provider=models_by_provider,
        model_connect_timeout=MODEL_CONNECT_TIMEOUT,
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
