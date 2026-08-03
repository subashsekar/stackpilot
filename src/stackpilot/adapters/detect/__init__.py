"""Project detection helpers used by framework adapters and the generator."""

from __future__ import annotations

from .entrypoint import (
    AsgiEntrypoint,
    FlaskEntrypoint,
    detect_asgi_entrypoint,
    detect_flask_entrypoint,
    find_python_files,
    module_path_for,
)
from .health_probe import (
    format_health_diagnostic,
    join_base_url,
    select_working_health_endpoint,
)
from .health_routes import (
    HEALTH_ROUTE_PRIORITY,
    HealthEndpointSelection,
    discover_health_path,
    discover_routes,
    normalize_route,
    rank_health_routes,
    resolve_health_endpoint,
    select_best_health_path,
)
from .package_manager import (
    NodePackageManager,
    PythonPackageManager,
    cli_is_runnable,
    detect_node_package_manager,
    detect_python_package_manager,
    node_run_command,
    python_run_prefix,
)
from .ports import detect_infra_port, detect_preferred_port
from .scan import IGNORED_SCAN_NAMES, iter_project_files, should_skip_directory
from .scripts import prefer_node_script, script_implies_reload
from .validation import ValidationWarning, validate_detected_services
from .venv import detect_venv_dir, resolve_python_executable

__all__ = [
    "AsgiEntrypoint",
    "FlaskEntrypoint",
    "HEALTH_ROUTE_PRIORITY",
    "HealthEndpointSelection",
    "IGNORED_SCAN_NAMES",
    "NodePackageManager",
    "PythonPackageManager",
    "ValidationWarning",
    "cli_is_runnable",
    "detect_asgi_entrypoint",
    "detect_flask_entrypoint",
    "detect_infra_port",
    "detect_node_package_manager",
    "detect_preferred_port",
    "detect_python_package_manager",
    "detect_venv_dir",
    "discover_health_path",
    "discover_routes",
    "find_python_files",
    "format_health_diagnostic",
    "iter_project_files",
    "join_base_url",
    "module_path_for",
    "node_run_command",
    "normalize_route",
    "prefer_node_script",
    "python_run_prefix",
    "rank_health_routes",
    "resolve_health_endpoint",
    "resolve_python_executable",
    "script_implies_reload",
    "select_best_health_path",
    "select_working_health_endpoint",
    "should_skip_directory",
    "validate_detected_services",
]
