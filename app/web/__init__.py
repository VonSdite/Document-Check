from app.web.admin_tasks import register_admin_tasks_routes
from app.web.auth import register_auth_routes
from app.web.models import register_models_routes
from app.web.overview import register_overview_routes
from app.web.presentation import register_presentation_routes
from app.web.settings import register_settings_routes
from app.web.user_tasks import register_user_tasks_routes


def register_routes(app):
    register_presentation_routes(app)
    register_auth_routes(app)
    register_user_tasks_routes(app)
    register_models_routes(app)
    register_overview_routes(app)
    register_admin_tasks_routes(app)
    register_settings_routes(app)
