"""可插拔插件装配子系统。"""

from app.plugins.contract import Plugin, PluginContext, PluginSpec
from app.plugins.errors import (
    CircularDependencyError,
    DuplicatePluginError,
    MissingDependencyError,
    PluginError,
    PluginInitError,
)
from app.plugins.manager import PluginManager
from app.plugins.registry import AssemblyResult, FailureRecord, PluginState

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginSpec",
    "PluginManager",
    "AssemblyResult",
    "FailureRecord",
    "PluginState",
    "PluginError",
    "DuplicatePluginError",
    "MissingDependencyError",
    "CircularDependencyError",
    "PluginInitError",
]
