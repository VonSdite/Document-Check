import os

import uvicorn


def serve_application(app, host: str, port: int) -> None:
    previous_root = os.environ.get("DOCUMENTCHECK_ROOT_DIR")
    os.environ["DOCUMENTCHECK_ROOT_DIR"] = str(app.config["ROOT_DIR"])
    try:
        uvicorn.run(
            "app.bootstrap.asgi:create_asgi_app",
            factory=True,
            host=host,
            port=port,
            workers=app.config["WEB_WORKERS"],
            interface="asgi3",
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="off",
            access_log=False,
            # 代理信任与身份策略统一由应用层配置处理。
            proxy_headers=False,
        )
    finally:
        if previous_root is None:
            os.environ.pop("DOCUMENTCHECK_ROOT_DIR", None)
        else:
            os.environ["DOCUMENTCHECK_ROOT_DIR"] = previous_root
