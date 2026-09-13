import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

HYPERLINK_CHECK_CODE = "hyperlink-validity"
HYPERLINK_REQUEST_TIMEOUT = (5, 8)
HYPERLINK_MAX_REDIRECTS = 5
HYPERLINK_MAX_NETWORK_TARGETS = 100
HYPERLINK_NETWORK_CONCURRENCY = 4
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_GET_FALLBACK_STATUSES = {400, 401, 403, 404, 405, 410, 501}

_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:access|api|auth|credential|key|password|secret|signature|token)",
    re.IGNORECASE,
)


class UnsafeHyperlinkTarget(ValueError):
    pass


def hyperlinks_from_meta(value) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(value, dict):
        return []
    raw_items = value.get("hyperlinks")
    if not isinstance(raw_items, list):
        return []

    items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        target = str(raw_item.get("target") or "").strip()
        display_text = str(raw_item.get("display_text") or "").strip()
        location = str(raw_item.get("location") or "").strip()
        source = str(raw_item.get("source") or "").strip()
        internal_target_exists = raw_item.get("internal_target_exists")
        if not isinstance(internal_target_exists, bool):
            internal_target_exists = None
        items.append(
            {
                "target": target,
                "display_text": display_text,
                "location": location,
                "source": source,
                "internal_target_exists": internal_target_exists,
            }
        )
    return items


def build_hyperlink_report(
    hyperlinks: list[dict],
    *,
    issue_limit: int,
    network: dict | None = None,
    probe=None,
) -> dict:
    groups = _group_hyperlinks(hyperlinks)
    if not groups:
        return {
            "summary": "超链接有效性检查结论：文档中未提取到可检查的超链接。",
            "items": [],
        }

    network = network if isinstance(network, dict) else {}
    results = [None] * len(groups)
    network_indexes = []
    for index, group in enumerate(groups):
        result = _check_without_network(group)
        if result is None:
            network_indexes.append(index)
        else:
            results[index] = result

    skipped_for_limit = network_indexes[HYPERLINK_MAX_NETWORK_TARGETS:]
    network_indexes = network_indexes[:HYPERLINK_MAX_NETWORK_TARGETS]
    for index in skipped_for_limit:
        results[index] = _result(
            "skipped",
            "检测覆盖限制",
            "文档中的外部链接数量超过单次规则检查上限，本链接未发起网络请求。",
            "本次报告未覆盖该链接的网络可达性。",
            "请人工抽查未覆盖链接，或拆分文档后重新检查。",
            confidence="low",
        )

    probe = probe or probe_http_url
    if network_indexes:
        with ThreadPoolExecutor(
            max_workers=min(HYPERLINK_NETWORK_CONCURRENCY, len(network_indexes)),
            thread_name_prefix="hyperlink-check",
        ) as executor:
            futures = {
                executor.submit(probe, groups[index]["target"], network): index
                for index in network_indexes
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = _result(
                        "suggestion",
                        "链接访问异常",
                        f"检查链接时发生异常：{exc.__class__.__name__}。",
                        "本次任务无法确认链接是否可以稳定访问。",
                        "请在目标客户网络环境中人工打开链接复核。",
                        confidence="low",
                    )

    counts = {"valid": 0, "issue": 0, "suggestion": 0, "skipped": 0}
    report_items = []
    for group, result in zip(groups, results):
        result = result or _result(
            "suggestion",
            "链接检查未完成",
            "链接没有得到有效检查结果。",
            "本次任务无法判断该链接的有效性。",
            "请人工打开链接复核。",
            confidence="low",
        )
        state = result["state"]
        counts[state] += 1
        if state in {"issue", "suggestion"}:
            report_items.append(_report_item(group, result))

    visible_items = report_items[: max(1, int(issue_limit or 1))]
    hidden_count = len(report_items) - len(visible_items)
    summary = (
        f"超链接有效性检查结论：共检查 {len(groups)} 个唯一链接；"
        f"有效 {counts['valid']} 个，明确无效 {counts['issue']} 个，"
        f"需人工确认 {counts['suggestion']} 个，跳过 {counts['skipped']} 个。"
    )
    if hidden_count:
        summary += f"受报告条目上限限制，另有 {hidden_count} 条未展开。"
    checked_https = any(
        urlsplit(groups[index]["target"]).scheme.lower() == "https"
        for index in network_indexes
    )
    if not bool(network.get("ssl_verify")) and checked_https:
        summary += "当前系统未启用 HTTPS 证书校验，本次仅检查连接与 HTTP 响应。"
    return {"summary": summary, "items": visible_items}


def probe_http_url(target: str, network: dict | None = None) -> dict:
    network = network if isinstance(network, dict) else {}
    current_url = target
    session = _http_session(network)
    try:
        for redirect_count in range(HYPERLINK_MAX_REDIRECTS + 1):
            try:
                _ensure_safe_http_destination(current_url)
            except UnsafeHyperlinkTarget as exc:
                return _result(
                    "suggestion",
                    "安全策略未检测",
                    str(exc),
                    "为防止访问服务器本机、内网或保留地址，系统没有发起请求。",
                    "请在允许访问该地址的客户网络环境中人工复核。",
                    confidence="high",
                )
            except socket.gaierror:
                return _result(
                    "suggestion",
                    "域名解析失败",
                    "检查服务器当前无法解析该链接的域名。",
                    "可能是域名错误、临时 DNS 故障或链接仅在特定网络中可用。",
                    "请核对域名，并在目标客户网络环境中重新访问。",
                    confidence="medium",
                )

            response = None
            try:
                response = _request_link(session, "HEAD", current_url, network)
                if response.status_code in _GET_FALLBACK_STATUSES:
                    response.close()
                    response = _request_link(session, "GET", current_url, network)
                status_code = int(response.status_code)
                location = str(response.headers.get("Location") or "").strip()
            except requests.exceptions.SSLError:
                return _result(
                    "suggestion",
                    "HTTPS 证书异常",
                    "启用证书校验时无法验证该链接的 HTTPS 证书。",
                    "客户浏览器可能显示安全警告或拒绝建立连接。",
                    "请检查证书有效期、域名和证书链。",
                    confidence="high",
                )
            except (requests.Timeout, requests.ConnectionError):
                return _result(
                    "suggestion",
                    "链接连接失败",
                    "检查服务器连接该链接时超时或连接失败。",
                    "可能是临时网络故障、访问策略限制或目标服务不可用。",
                    "请稍后重试，并在目标客户网络环境中人工复核。",
                    confidence="medium",
                )
            except requests.RequestException:
                return _result(
                    "suggestion",
                    "链接请求失败",
                    "检查服务器未能完成该链接的 HTTP 请求。",
                    "当前结果不足以确认链接已经失效。",
                    "请人工打开链接并核对网络访问策略。",
                    confidence="low",
                )
            finally:
                if response is not None:
                    response.close()

            if status_code in _REDIRECT_STATUSES:
                if not location:
                    return _result(
                        "suggestion",
                        "重定向目标缺失",
                        f"链接返回 HTTP {status_code}，但没有提供重定向目标。",
                        "客户浏览器可能无法到达最终资料页面。",
                        "请修复服务器重定向配置或更新链接。",
                        confidence="high",
                    )
                if redirect_count >= HYPERLINK_MAX_REDIRECTS:
                    return _result(
                        "suggestion",
                        "重定向次数过多",
                        f"链接在 {HYPERLINK_MAX_REDIRECTS} 次重定向后仍未到达最终页面。",
                        "客户访问时可能陷入重定向循环或无法打开资料。",
                        "请检查并缩短重定向链。",
                        confidence="high",
                    )
                current_url = urljoin(current_url, location)
                continue

            if 200 <= status_code < 300:
                return _result("valid", http_status=status_code, final_url=current_url)
            if status_code in {404, 410}:
                return _result(
                    "issue",
                    "外部链接失效",
                    f"链接返回 HTTP {status_code}，目标资源不存在或已被移除。",
                    "客户无法通过该链接获取引用资料。",
                    "请更新为可访问的资料地址或移除无效引用。",
                    severity="medium",
                    confidence="high",
                    http_status=status_code,
                    final_url=current_url,
                )
            if status_code in {401, 403}:
                detail = (
                    f"链接返回 HTTP {status_code}，可能需要登录、授权或特定网络权限。"
                )
            elif status_code == 429:
                detail = "链接返回 HTTP 429，目标服务当前限制了访问频率。"
            elif status_code >= 500:
                detail = f"链接返回 HTTP {status_code}，目标服务当前异常。"
            else:
                detail = (
                    f"链接返回 HTTP {status_code}，无法确认目标资源是否可正常获取。"
                )
            return _result(
                "suggestion",
                "外部链接需人工确认",
                detail,
                "该响应可能由权限、临时故障或访问环境差异造成，不能直接判定链接失效。",
                "请在目标客户账号和网络环境中人工打开链接复核。",
                confidence="medium",
                http_status=status_code,
                final_url=current_url,
            )
    finally:
        session.close()


def format_hyperlink_report(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def _group_hyperlinks(hyperlinks: list[dict]) -> list[dict]:
    groups = []
    by_target = {}
    for raw_item in hyperlinks:
        if not isinstance(raw_item, dict):
            continue
        target = str(raw_item.get("target") or "").strip()
        key = target
        if key not in by_target:
            by_target[key] = {
                "target": target,
                "display_texts": [],
                "locations": [],
                "internal_states": [],
            }
            groups.append(by_target[key])
        group = by_target[key]
        display_text = str(raw_item.get("display_text") or "").strip()
        location = _link_location(raw_item)
        if display_text and display_text not in group["display_texts"]:
            group["display_texts"].append(display_text)
        if location and location not in group["locations"]:
            group["locations"].append(location)
        internal_state = raw_item.get("internal_target_exists")
        if isinstance(internal_state, bool):
            group["internal_states"].append(internal_state)
    return groups


def _check_without_network(group: dict) -> dict | None:
    target = group["target"]
    if not target:
        return _result(
            "issue",
            "超链接目标缺失",
            "文档包含可点击链接对象，但没有有效的目标地址。",
            "客户点击后无法进入被引用的资料或位置。",
            "请为链接补充正确的目标地址。",
            severity="medium",
            confidence="high",
        )
    if target.startswith("#"):
        states = group["internal_states"]
        if states and not all(states):
            return _result(
                "issue",
                "内部链接目标不存在",
                "文档中的内部超链接没有找到对应书签、页面、标题或工作表位置。",
                "客户点击链接后无法跳转到预期内容。",
                "请修复内部链接目标或重新建立书签。",
                severity="medium",
                confidence="high",
            )
        if states:
            return _result("valid")
        return _result("skipped")

    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError:
        return _invalid_url_result("链接中的主机名或端口格式无效。")
    scheme = parsed.scheme.lower()
    if not scheme:
        return _result("skipped")
    if scheme not in {"http", "https"}:
        if scheme in {"javascript", "data"}:
            return _invalid_url_result(f"链接使用了不安全的 {scheme}: 协议。")
        return _result("skipped")
    if not parsed.hostname:
        return _invalid_url_result("HTTP 链接缺少有效域名或 IP 地址。")
    if parsed.username is not None or parsed.password is not None:
        return _invalid_url_result("链接地址中直接包含用户名或密码。")
    if port == 0:
        return _invalid_url_result("HTTP 链接使用了无效的 0 端口。")
    return None


def _invalid_url_result(description: str) -> dict:
    return _result(
        "issue",
        "超链接格式错误",
        description,
        "客户可能无法安全、正确地打开目标资源。",
        "请修正链接格式并重新验证。",
        severity="medium",
        confidence="high",
    )


def _ensure_safe_http_destination(target: str) -> None:
    try:
        parsed = urlsplit(target)
        explicit_port = parsed.port
        port = explicit_port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeHyperlinkTarget(
            "链接的主机名或端口格式无效，系统未发起请求。"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeHyperlinkTarget(
            "链接不是有效的 HTTP 或 HTTPS 地址，系统未发起请求。"
        )
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeHyperlinkTarget("链接地址包含用户凭据，系统未发起请求。")
    if explicit_port == 0:
        raise UnsafeHyperlinkTarget("链接使用了无效的 0 端口，系统未发起请求。")

    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise socket.gaierror(f"无法解析域名：{parsed.hostname}")
    for address in addresses:
        value = str(address[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(value)
        except ValueError as exc:
            raise UnsafeHyperlinkTarget(
                "域名解析结果不是有效 IP 地址，系统未发起请求。"
            ) from exc
        if not ip.is_global:
            raise UnsafeHyperlinkTarget(
                "链接指向服务器本机、内网、链路本地或保留地址，系统按安全策略未访问。"
            )


def _http_session(network: dict) -> requests.Session:
    session = requests.Session()
    proxy_mode = str(network.get("proxy_mode") or "direct").strip().lower()
    session.trust_env = proxy_mode == "system"
    if proxy_mode == "custom":
        proxy = str(network.get("proxy") or "").strip()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update(
        {
            "User-Agent": "Document-Check-LinkValidator/1.0",
            "Accept": "*/*",
            "Connection": "close",
        }
    )
    return session


def _request_link(session, method: str, target: str, network: dict):
    headers = {"Range": "bytes=0-0"} if method == "GET" else None
    return session.request(
        method,
        target,
        headers=headers,
        allow_redirects=False,
        stream=True,
        timeout=HYPERLINK_REQUEST_TIMEOUT,
        verify=bool(network.get("ssl_verify")),
    )


def _report_item(group: dict, result: dict) -> dict:
    target = _redacted_target(group["target"])
    labels = group["display_texts"][:3]
    excerpt = "、".join(labels)
    if excerpt and target:
        excerpt = f"{excerpt} → {target}"
    elif target:
        excerpt = target
    return {
        "status": result["state"],
        "type": result["state"],
        "severity": result.get("severity") or "low",
        "confidence": result.get("confidence") or "medium",
        "category": result.get("category") or "超链接有效性",
        "location": "；".join(group["locations"][:8]) or "文档中的超链接",
        "excerpt": excerpt,
        "description": result.get("description") or "链接需要人工复核。",
        "impact": result.get("impact") or "可能影响客户获取引用资料。",
        "suggestion": result.get("suggestion") or "请人工打开链接复核。",
    }


def _link_location(item: dict) -> str:
    source = str(item.get("source") or "").strip()
    location = str(item.get("location") or "").strip()
    if source and location:
        return f"{source} > {location}"
    return source or location


def _redacted_target(target: str) -> str:
    try:
        parsed = urlsplit(str(target or ""))
        port = parsed.port
    except ValueError:
        return str(target or "")[:500]
    if parsed.scheme.lower() not in {"http", "https"}:
        return str(target or "")[:500]
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port:
        hostname = f"{hostname}:{port}"
    query = urlencode(
        [
            (key, "***" if _SENSITIVE_QUERY_KEY.search(key) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme, hostname, parsed.path, query, parsed.fragment))[
        :500
    ]


def _result(
    state: str,
    category: str = "",
    description: str = "",
    impact: str = "",
    suggestion: str = "",
    *,
    severity: str = "low",
    confidence: str = "medium",
    http_status: int | None = None,
    final_url: str = "",
) -> dict:
    return {
        "state": state,
        "category": category,
        "description": description,
        "impact": impact,
        "suggestion": suggestion,
        "severity": severity,
        "confidence": confidence,
        "http_status": http_status,
        "final_url": final_url,
    }
