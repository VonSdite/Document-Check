import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import xmlsec
from flask import current_app, request
from lxml import etree


SAML1_PROTOCOL_NS = "urn:oasis:names:tc:SAML:1.0:protocol"
SAML1_ASSERTION_NS = "urn:oasis:names:tc:SAML:1.0:assertion"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
NSMAP = {
    "samlp": SAML1_PROTOCOL_NS,
    "saml": SAML1_ASSERTION_NS,
    "ds": DSIG_NS,
}
CLOCK_SKEW = timedelta(minutes=3)


class Saml1ConfigError(Exception):
    pass


class Saml1ResponseError(Exception):
    pass


def saml1_login_url(next_path: str) -> str:
    saml1 = _saml1_config()
    idp_sso_url = str(saml1.get("idp_sso_url") or "").strip()
    if not idp_sso_url:
        raise Saml1ConfigError("SAML 1.x 未配置 idp_sso_url，请通过公司统一入口访问。")
    return _append_query(idp_sso_url, {"TARGET": next_path or "/"})


def process_saml1_response() -> tuple[str, str]:
    saml1 = _saml1_config()
    _require_saml1_fields(saml1, ("acs_url", "idp_issuer", "idp_x509_cert"))
    xml_text = _decode_saml_response(request.form.get("SAMLResponse"))
    root = _parse_xml(xml_text)
    assertion = _response_assertion(root)

    _validate_signature(root, assertion, saml1["idp_x509_cert"])
    _validate_status(root)
    _validate_issuer(root, assertion, saml1["idp_issuer"])
    _validate_recipient(root, assertion, saml1["acs_url"])
    _validate_conditions(assertion, saml1.get("audience"))

    user_id_attribute = str(saml1.get("user_id_attribute") or "").strip()
    username_attribute = str(saml1.get("username_attribute") or "").strip()
    user_id = _attribute_value(assertion, user_id_attribute) if user_id_attribute else _name_identifier(assertion)
    username = _attribute_value(assertion, username_attribute) if username_attribute else ""
    if not user_id:
        raise Saml1ResponseError("SAML 1.x 响应缺少用户 ID")
    return user_id, username or user_id


def _saml1_config() -> dict:
    auth_config = current_app.config.get("AUTH", {})
    saml1 = auth_config.get("saml1", {}) if isinstance(auth_config, dict) else {}
    return saml1 if isinstance(saml1, dict) else {}


def _require_saml1_fields(saml1: dict, fields: tuple[str, ...]):
    missing = [field for field in fields if not str(saml1.get(field) or "").strip()]
    if missing:
        raise Saml1ConfigError("SAML 1.x 配置缺少字段：" + ", ".join(missing))


def _decode_saml_response(value) -> str:
    value = str(value or "").strip()
    if not value:
        raise Saml1ResponseError("未收到 SAMLResponse")
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise Saml1ResponseError("SAMLResponse 不是有效的 Base64 XML") from error


def _parse_xml(xml_text: str):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as error:
        raise Saml1ResponseError("SAMLResponse XML 解析失败") from error
    if root.tag != f"{{{SAML1_PROTOCOL_NS}}}Response":
        raise Saml1ResponseError("SAMLResponse 不是 SAML 1.x Response")
    return root


def _response_assertion(root):
    assertions = root.xpath("./saml:Assertion", namespaces=NSMAP)
    if len(assertions) != 1:
        raise Saml1ResponseError("SAML 1.x Response 必须包含且仅包含一个 Assertion")
    return assertions[0]


def _validate_signature(root, assertion, cert: str):
    xmlsec.tree.add_ids(root, ["ID", "ResponseID", "AssertionID"])
    signature_node = root.find("./ds:Signature", namespaces=NSMAP)
    signed_node = root
    if signature_node is None:
        signature_node = assertion.find("./ds:Signature", namespaces=NSMAP)
        signed_node = assertion
    if signature_node is None:
        raise Saml1ResponseError("SAML 1.x Response 缺少签名")

    try:
        key = xmlsec.Key.from_memory(_pem_cert(cert), xmlsec.KeyFormat.CERT_PEM, None)
        context = xmlsec.SignatureContext()
        context.key = key
        context.verify(signature_node)
    except Exception as error:
        raise Saml1ResponseError("SAML 1.x 签名校验失败") from error

    if signature_node.getparent() is not signed_node:
        raise Saml1ResponseError("SAML 1.x 签名位置无效")
    _validate_signature_reference(signature_node, signed_node)


def _validate_signature_reference(signature_node, signed_node):
    reference_uris = signature_node.xpath(".//ds:Reference/@URI", namespaces=NSMAP)
    if not reference_uris:
        raise Saml1ResponseError("SAML 1.x 签名缺少 Reference")
    signed_ids = {
        str(signed_node.get("ID") or "").strip(),
        str(signed_node.get("ResponseID") or "").strip(),
        str(signed_node.get("AssertionID") or "").strip(),
    }
    signed_ids.discard("")
    for uri in reference_uris:
        uri = str(uri or "").strip()
        if uri == "":
            continue
        if not uri.startswith("#") or uri[1:] not in signed_ids:
            raise Saml1ResponseError("SAML 1.x 签名 Reference 不匹配")


def _pem_cert(cert: str) -> str:
    cert = str(cert or "").strip()
    if "BEGIN CERTIFICATE" in cert:
        return cert
    lines = [cert[index : index + 64] for index in range(0, len(cert), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----"


def _validate_status(root):
    status_code = root.xpath("string(./samlp:Status/samlp:StatusCode/@Value)", namespaces=NSMAP).strip()
    if status_code and not status_code.endswith(":Success") and status_code != "Success":
        raise Saml1ResponseError("SAML 1.x 登录状态不是 Success")


def _validate_issuer(root, assertion, expected_issuer: str):
    expected_issuer = str(expected_issuer or "").strip()
    issuers = [
        str(root.get("Issuer") or "").strip(),
        str(assertion.get("Issuer") or "").strip(),
    ]
    if expected_issuer not in issuers:
        raise Saml1ResponseError("SAML 1.x Issuer 不匹配")


def _validate_recipient(root, assertion, acs_url: str):
    expected = _normalize_url(acs_url)
    recipients = [str(root.get("Recipient") or "").strip()]
    recipients.extend(
        recipient.strip()
        for recipient in assertion.xpath(".//@Recipient", namespaces=NSMAP)
        if str(recipient or "").strip()
    )
    for recipient in recipients:
        if recipient and _normalize_url(recipient) != expected:
            raise Saml1ResponseError("SAML 1.x Recipient 不匹配")


def _validate_conditions(assertion, expected_audience):
    conditions = assertion.find("./saml:Conditions", namespaces=NSMAP)
    if conditions is None:
        raise Saml1ResponseError("SAML 1.x Assertion 缺少 Conditions")
    now = datetime.now(UTC)
    not_before = _saml_time(conditions.get("NotBefore"))
    not_on_or_after = _saml_time(conditions.get("NotOnOrAfter"))
    if not_before and now + CLOCK_SKEW < not_before:
        raise Saml1ResponseError("SAML 1.x Assertion 尚未生效")
    if not_on_or_after and now - CLOCK_SKEW >= not_on_or_after:
        raise Saml1ResponseError("SAML 1.x Assertion 已过期")

    expected_audience = str(expected_audience or "").strip()
    if expected_audience:
        audiences = [
            str(item.text or "").strip()
            for item in assertion.xpath(
                "./saml:Conditions/saml:AudienceRestrictionCondition/saml:Audience",
                namespaces=NSMAP,
            )
        ]
        if expected_audience not in audiences:
            raise Saml1ResponseError("SAML 1.x Audience 不匹配")


def _saml_time(value):
    value = str(value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise Saml1ResponseError("SAML 1.x 时间格式无效") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _name_identifier(assertion) -> str:
    return assertion.xpath("string(.//saml:NameIdentifier)", namespaces=NSMAP).strip()


def _attribute_value(assertion, name: str) -> str:
    if not name:
        return ""
    values = assertion.xpath(
        ".//saml:Attribute[@AttributeName=$name or @Name=$name]/saml:AttributeValue/text()",
        name=name,
        namespaces=NSMAP,
    )
    return str(values[0]).strip() if values else ""


def _normalize_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, "", ""))


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
