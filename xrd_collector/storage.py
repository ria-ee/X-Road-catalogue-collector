"""Storage plugin management module for xrd_collector"""

from importlib.metadata import entry_points
from logging import Logger
from typing import Any


class PluginError(Exception):
    """Generic plugin Exception"""


class PluginSkip(PluginError):
    """Exception indicating that plugin requests skipping collection"""


class PluginBase:
    """Base class for xrd_collector storage plugins"""
    # Plugin should set logger on initialization
    logger: Logger
    plugin_active: bool = False

    def active(self) -> bool:
        """Return True if plugin is active"""
        return self.plugin_active

    def activate(self) -> None:
        """Mark plugin as activated"""
        # Boolean value change should be thread safe
        self.plugin_active = True

    def deactivate(self) -> None:
        """
        Mark plugin as deactivated
        Plugin should mark itself as inactive on fatal failure
        """
        # Boolean value change should be thread safe
        self.plugin_active = False

    def subsystem_state(self, subsystem_path: str, doc_type: str) -> tuple[
            str, dict[str, str]]:
        """
        Get storage state for a subsystem
        Return tuple: (Documents path used by subsystem, Document hashes)
        """
        raise NotImplementedError

    def save_subsystem_state(
            self, path: str, hashes: dict[str, str], doc_type: str) -> None:
        """Save document hashes to subsystem state"""
        raise NotImplementedError

    def save_doc(
            self, path: str, hashes: dict[str, str], doc: str, file_ext: str,
            service_name: str | None) -> tuple[str, str]:
        """
        Save service description document if it does not exist yet
        Return tuple: (Document name, Document hash)
        """
        raise NotImplementedError

    def save_catalogue(self, results: dict[str, Any]) -> None:
        """Save service catalogue"""
        raise NotImplementedError


def deactivate_on_fail(function):
    """Decorator for plugin deactivation on unhandled exception"""
    def wrapper(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except Exception as err:
            self.logger.warning('Storage plugin failed with error: %s', err)
            self.deactivate()
            raise err
    return wrapper


def load_plugin(config_data, logger, plugin_name) -> PluginBase:
    """Load required storage plugin"""
    # Getting list of available plugins
    plugin_entry_points = entry_points(group="xrd_collector.plugin")
    try:
        available_plugins = {
            entrypoint.name: entrypoint
            for entrypoint in plugin_entry_points
        }
    except AttributeError as err:
        raise PluginError(f'Error while loading plugin entry point: {err}') from err

    # Loading required plugin
    logger.info('Start loading "%s" plugin', plugin_name)
    if plugin_name in available_plugins:
        plugin_class = available_plugins[plugin_name].load()
        plugin = plugin_class(config_data, logger)
    else:
        raise PluginError(f'Plugin {plugin_name} is not available')

    if not isinstance(plugin, PluginBase):
        raise PluginError(f'Plugin {plugin_name} is not compatible')

    plugin.activate()
    logger.info('Finished loading "%s" plugin', plugin_name)
    return plugin
