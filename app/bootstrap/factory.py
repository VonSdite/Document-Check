from pathlib import Path

from app.infrastructure.runtime import _create_base_app
from app.persistence.defaults import seed_defaults
from app.persistence.schema import init_db
from app.web import register_routes
from app.web.formatting import render_markdown
from app.web.observability import configure_access_logging, register_observability


def create_app(root_dir: Path | None = None):
    app = _create_base_app(root_dir)
    configure_access_logging(app)

    with app.app_context():
        init_db()
        seed_defaults()

    register_observability(app)
    register_routes(app)
    app.add_template_filter(render_markdown, "markdown")
    return app
