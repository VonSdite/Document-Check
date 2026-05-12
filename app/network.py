import socket
import struct

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback.
    fcntl = None


SIOCGIFADDR = 0x8915


def access_urls(host: str, port: int) -> list[str]:
    return [f"http://{address}:{port}" for address in access_addresses(host)]


def access_addresses(host: str) -> list[str]:
    host = str(host or "").strip() or "127.0.0.1"
    if host != "0.0.0.0":
        return [host]
    return _unique_addresses(
        [
            "127.0.0.1",
            *_interface_ipv4_addresses(),
            *_hostname_ipv4_addresses(),
        ]
    )


def _interface_ipv4_addresses() -> list[str]:
    if fcntl is None:
        return []

    addresses = []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return addresses

    for _, name in interfaces:
        try:
            request = struct.pack("256s", name.encode("utf-8")[:15])
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                data = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)
            addresses.append(socket.inet_ntoa(data[20:24]))
        except OSError:
            continue
    return addresses


def _hostname_ipv4_addresses() -> list[str]:
    addresses = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return addresses
    for info in infos:
        addresses.append(info[4][0])
    return addresses


def _unique_addresses(addresses: list[str]) -> list[str]:
    result = []
    seen = set()
    for address in addresses:
        if not address or address == "0.0.0.0" or address in seen:
            continue
        seen.add(address)
        result.append(address)
    return result
