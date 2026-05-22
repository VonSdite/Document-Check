from gevent import monkey

monkey.patch_all()

import os
import socket
import threading
import time
import webbrowser

from gevent.pywsgi import WSGIServer

from app import create_app
from app.network import access_urls


def main():
    app = create_app()
    host = os.environ.get("HOST", app.config["LISTEN_HOST"])
    port = int(os.environ.get("PORT", app.config["LISTEN_PORT"]))
    open_path = os.environ.get("OPEN_PATH") or f"{app.config['ADMIN_URL']}/login"
    open_url = _browser_url(host, port, open_path)

    app.logger.info("服务监听：http://%s:%s", host, port)
    for url in access_urls(host, port):
        app.logger.info("可访问地址：%s", url)
    app.logger.info("浏览器将打开：%s", open_url)

    server = WSGIServer((host, port), app)
    server.start()
    threading.Thread(
        target=_open_browser_when_ready,
        args=(open_url, _connect_host(host), port),
        daemon=True,
        name="browser-opener",
    ).start()
    server.serve_forever()


def _browser_url(host: str, port: int, path: str) -> str:
    path = str(path or "/").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return f"http://{_connect_host(host)}:{port}{path}"


def _connect_host(host: str) -> str:
    host = str(host or "").strip()
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _open_browser_when_ready(url: str, host: str, port: int):
    for _ in range(80):
        try:
            with socket.create_connection((host, port), timeout=0.25):
                break
        except OSError:
            time.sleep(0.25)
    webbrowser.open(url)


if __name__ == "__main__":
    main()
