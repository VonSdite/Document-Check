"""用户领域标签的输入约束。"""

MAX_USER_DOMAINS = 20
MAX_DOMAIN_NAME_LENGTH = 40
MAX_DOMAIN_BATCH_USERS = 200


def normalize_domain_names(values: list[str]) -> list[str]:
    names = {}
    for value in values:
        name = " ".join(value.split())
        if not name:
            continue
        if len(name) > MAX_DOMAIN_NAME_LENGTH:
            raise ValueError(f"领域名称最多 {MAX_DOMAIN_NAME_LENGTH} 个字符。")
        names.setdefault(name.casefold(), name)
    if len(names) > MAX_USER_DOMAINS:
        raise ValueError(f"每个用户最多设置 {MAX_USER_DOMAINS} 个领域。")
    return list(names.values())
