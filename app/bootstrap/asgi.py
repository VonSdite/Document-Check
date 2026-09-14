from app.bootstrap.diagnostics import run_entrypoint


def create_asgi_app():
    """为每个 Web 进程创建独立 Flask 应用和 WSGI 请求线程池。"""
    return run_entrypoint(_create_asgi_app, logger_name="app.bootstrap.asgi")


def _create_asgi_app():
    from a2wsgi import WSGIMiddleware

    from app.bootstrap.factory import create_app

    app = create_app()

    def wsgi_app(environ, start_response):
        # ASGI 提供明确的请求体结束信号，Flask 可据此读取分块上传并限制大小。
        environ["wsgi.input_terminated"] = True
        return app(environ, start_response)

    return WSGIMiddleware(wsgi_app, workers=app.config["WEB_THREADS"])
