from urllib.parse import urlsplit

from flask import current_app, request
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.constants import OneLogin_Saml2_Constants
from onelogin.saml2.settings import OneLogin_Saml2_Settings


class SamlConfigError(Exception):
    pass


def create_saml_auth() -> OneLogin_Saml2_Auth:
    auth_config = current_app.config.get("AUTH", {})
    saml = _saml_config(auth_config)
    _require_saml_fields(
        saml,
        ("sp_entity_id", "acs_url", "idp_entity_id", "idp_sso_url", "idp_x509_cert"),
    )
    public_url = saml["acs_url"] if request.endpoint == "saml_acs" else None
    return OneLogin_Saml2_Auth(
        _prepare_request_data(public_url),
        old_settings=_toolkit_settings(auth_config, sp_validation_only=False),
    )


def saml_sp_metadata() -> str:
    auth_config = current_app.config.get("AUTH", {})
    saml = _saml_config(auth_config)
    _require_saml_fields(saml, ("sp_entity_id", "acs_url"))
    settings = OneLogin_Saml2_Settings(
        _toolkit_settings(auth_config, sp_validation_only=True),
        sp_validation_only=True,
    )
    metadata = settings.get_sp_metadata()
    errors = settings.validate_metadata(metadata)
    if errors:
        raise SamlConfigError("SAML SP metadata 配置无效：" + "; ".join(errors))
    if isinstance(metadata, bytes):
        return metadata.decode("utf-8")
    return str(metadata)


def _saml_config(auth_config: dict) -> dict:
    saml = auth_config.get("saml", {}) if isinstance(auth_config, dict) else {}
    return saml if isinstance(saml, dict) else {}


def _require_saml_fields(saml: dict, fields: tuple[str, ...]):
    missing = [field for field in fields if not str(saml.get(field) or "").strip()]
    if missing:
        raise SamlConfigError("SAML 配置缺少字段：" + ", ".join(missing))


def _toolkit_settings(auth_config: dict, *, sp_validation_only: bool) -> dict:
    saml = _saml_config(auth_config)
    settings = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": saml.get("sp_entity_id", ""),
            "assertionConsumerService": {
                "url": saml.get("acs_url", ""),
                "binding": OneLogin_Saml2_Constants.BINDING_HTTP_POST,
            },
            "NameIDFormat": OneLogin_Saml2_Constants.NAMEID_UNSPECIFIED,
        },
        "security": {
            "wantAttributeStatement": False,
            "requestedAuthnContext": False,
        },
    }
    if not sp_validation_only:
        settings["idp"] = {
            "entityId": saml.get("idp_entity_id", ""),
            "singleSignOnService": {
                "url": saml.get("idp_sso_url", ""),
                "binding": OneLogin_Saml2_Constants.BINDING_HTTP_REDIRECT,
            },
            "x509cert": saml.get("idp_x509_cert", ""),
        }
    return settings


def _prepare_request_data(public_url: str | None = None) -> dict:
    parsed = urlsplit(public_url or request.url)
    http_host = parsed.netloc or request.host
    script_name = parsed.path or request.path
    return {
        "https": "on" if parsed.scheme == "https" else "off",
        "http_host": http_host,
        "script_name": script_name,
        "get_data": request.args.copy(),
        "post_data": request.form.copy(),
        "query_string": request.query_string.decode("utf-8", errors="ignore"),
    }
