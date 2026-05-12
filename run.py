from gevent import monkey

monkey.patch_all()

import os

from gevent.pywsgi import WSGIServer

from app import create_app
from app.network import access_urls


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("HOST", app.config["LISTEN_HOST"])
    port = int(os.environ.get("PORT", app.config["LISTEN_PORT"]))
    app.logger.info("服务监听：http://%s:%s", host, port)
    for url in access_urls(host, port):
        app.logger.info("可访问地址：%s", url)
    WSGIServer((host, port), app).serve_forever()
