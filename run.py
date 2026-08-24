from gevent import monkey

monkey.patch_all()

import os

from gevent.pywsgi import WSGIServer

from app import create_app
from app.network import access_urls


app = create_app()


if __name__ == "__main__":
    host = (
        os.environ.get("HOST", app.config["LISTEN_HOST"])
        if app.config["PLATFORM"]
        else "127.0.0.1"
    )
    port = int(os.environ.get("PORT", app.config["LISTEN_PORT"]))
    app.logger.info(
        "服务监听：http://%s:%s pid=%s url_prefix=%s proxy_fix=%s",
        host,
        port,
        os.getpid(),
        app.config["APPLICATION_ROOT"],
        app.config["PROXY_FIX"],
    )
    for url in access_urls(host, port):
        app.logger.info("可访问地址：%s", url)
    try:
        WSGIServer((host, port), app).serve_forever()
    except Exception:
        app.logger.exception("服务启动或运行失败 host=%s port=%s", host, port)
        raise
