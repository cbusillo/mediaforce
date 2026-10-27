from mediaforce.web.routes.app_window import register_app_window_routes
from mediaforce.web.routes.completed import register_completed_routes
from mediaforce.web.routes.dashboard import register_dashboard_routes
from mediaforce.web.routes.folders import register_folder_routes
from mediaforce.web.routes.hosts import register_host_routes
from mediaforce.web.routes.operator_work import register_operator_work_routes
from mediaforce.web.routes.posters import register_poster_routes
from mediaforce.web.routes.queues import register_queue_routes
from mediaforce.web.routes.settings import register_compression_settings_routes, \
    register_computer_settings_routes, register_library_settings_routes, register_metadata_settings_routes, \
    register_settings_routes

__all__ = [
    "register_app_window_routes",
    "register_completed_routes",
    "register_compression_settings_routes",
    "register_computer_settings_routes",
    "register_dashboard_routes",
    "register_folder_routes",
    "register_host_routes",
    "register_library_settings_routes",
    "register_metadata_settings_routes",
    "register_operator_work_routes",
    "register_poster_routes",
    "register_queue_routes",
    "register_settings_routes",
]
