from mediaforce.web.routes.completed import register_completed_routes
from mediaforce.web.routes.dashboard import register_dashboard_routes
from mediaforce.web.routes.frontend import register_frontend_routes
from mediaforce.web.routes.folders import register_folder_routes
from mediaforce.web.routes.hosts import register_host_routes
from mediaforce.web.routes.queues import register_queue_routes
from mediaforce.web.routes.settings import register_settings_routes

__all__ = [
    "register_completed_routes",
    "register_dashboard_routes",
    "register_frontend_routes",
    "register_folder_routes",
    "register_host_routes",
    "register_queue_routes",
    "register_settings_routes",
]
