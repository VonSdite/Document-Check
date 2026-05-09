import os

from app import create_app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("HOST", app.config["LISTEN_HOST"])
    port = int(os.environ.get("PORT", app.config["LISTEN_PORT"]))
    app.run(host=host, port=port, debug=False)
