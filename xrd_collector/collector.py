"""This is a module for collection of X-Road services information."""

import argparse
from dataclasses import dataclass, field
from logging import Logger
import logging.config
import queue
import re
from threading import Thread, Event, Lock
import traceback
from typing import Any, Sequence
import urllib.parse as urlparse
import yaml

import xrdinfo
from xrd_collector.storage import PluginBase, PluginError, PluginSkip, load_plugin
from xrd_collector.util import Method, Service, Subsystem

# Default timeout for HTTP requests
DEFAULT_TIMEOUT: float = 5.0

# Do not use threading by default
DEFAULT_THREAD_COUNT: int = 1

# This logger will be used before loading of logger configuration
DEFAULT_LOGGER: dict[str, Any] = {
    'version': 1,
    'disable_existing_loggers': True,
    'formatters': {
        'standard': {
            'format': '%(asctime)s - %(threadName)s - %(levelname)s: %(message)s'
        },
    },
    'handlers': {
        'default': {
            'level': 'WARNING',
            'formatter': 'standard',
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stderr',
        },
    },
    'loggers': {
        '': {
            'handlers': ['default'],
            'level': 'WARNING',
            'propagate': True
        },
        'catalogue-collector': {
            'handlers': ['default'],
            'level': 'WARNING',
            'propagate': False
        },
    }
}

# Application logger name
LOGGER_NAME = 'catalogue-collector'


@dataclass
class Config:
    """Application configuration"""
    storage_plugin: str = ''
    server_url: str = ''
    client: list[str] = field(default_factory=list)
    instance: str = ''
    timeout: float = DEFAULT_TIMEOUT
    verify: bool | str = True
    cert: tuple[str, str] | None = None
    thread_cnt: int = DEFAULT_THREAD_COUNT
    wsdl_replaces: list[list[str]] = field(default_factory=list)
    excluded_member_codes: list[str] = field(default_factory=list)
    excluded_subsystem_codes: list[list[str]] = field(default_factory=list)


class Collector:
    """Main class for X-Road collector"""
    storage: PluginBase
    config: Config
    logger: Logger = logging.getLogger(LOGGER_NAME)
    results: dict[str, Subsystem] = {}
    results_lock: Lock = Lock()
    shutdown: Event = Event()
    work_queue: queue.Queue = queue.Queue()

    def __init__(self, argv: Sequence[str] | None = None):
        logging.config.dictConfig(DEFAULT_LOGGER)

        parser = argparse.ArgumentParser(
            description='Collect WSDL and OpenAPI service descriptions from X-Road members.',
            formatter_class=argparse.RawDescriptionHelpFormatter
        )
        parser.add_argument(
            'config', metavar='CONFIG_FILE',
            help='Configuration file')
        args = parser.parse_args(argv)

        config_data = self._load_config(args.config)
        self._configure_logging(config_data)
        self._config(config_data)

        try:
            self.storage = load_plugin(config_data, self.logger, self.config.storage_plugin)
        except PluginSkip as err:
            self.logger.info('Plugin requests skipping collection: %s', err)
            self._exit_program(0)
        except PluginError as err:
            self.logger.error('Cannot initialize plugin: %s', err)
            self._exit_program(1)

    def _load_config(self, config_file: str) -> dict[str, Any]:
        """Load configuration from YAML/JSON file"""
        try:
            with open(config_file, 'r', encoding='utf-8') as conf:
                self.logger.info('Loading configuration from file "%s"', config_file)
                # JSON as a subset of YAML can be also read as YAML
                return yaml.safe_load(conf)
        except IOError as err:
            self.logger.error('Cannot load configuration file "%s": %s', config_file, str(err))
            return {}
        except yaml.YAMLError as err:
            self.logger.error(
                'Invalid YAML/JSON configuration file "%s": %s', config_file, str(err))
            return {}

    def _configure_logging(self, config_data: dict[str, Any]) -> None:
        """Configure logging based on loaded configuration"""
        if 'logging-config' in config_data:
            logging.config.dictConfig(config_data['logging-config'])
            self.logger.info('Logger configured')

    def _config(self, config_data: dict[str, Any]) -> None:
        """Set application configuration based on loaded values"""
        # Default configuration
        self.config = Config()

        if 'storage_plugin' in config_data and config_data['storage_plugin']:
            self.config.storage_plugin = config_data['storage_plugin']
            self.logger.info('Configuring "storage_plugin": %s', self.config.storage_plugin)
        else:
            self.logger.error('Configuration error: Storage plugin is not configured')
            self._exit_program(1)

        if 'server_url' in config_data:
            self.config.server_url = config_data['server_url']
            self.logger.info('Configuring "url": %s', self.config.server_url)
        else:
            self.logger.error('Configuration error: Local Security Server URL is not provided')
            self._exit_program(1)

        if 'client' in config_data and len(config_data['client']) in (3, 4):
            self.config.client = config_data['client']
            self.logger.info('Configuring "client": %s', self.config.client)
        else:
            self.logger.error(
                'Configuration error: Client identifier is incorrect. '
                'Expecting list of identifiers. '
                'Example: ["INST", "CLASS", "MEMBER_CODE", "MEMBER_CLASS"])')
            self._exit_program(1)

        if 'instance' in config_data and config_data['instance']:
            self.config.instance = config_data['instance']
            self.logger.info('Configuring "instance": %s', self.config.instance)

        if 'timeout' in config_data and config_data['timeout'] > 0.0:
            self.config.timeout = config_data['timeout']
            self.logger.info('Configuring "timeout": %s', self.config.timeout)

        if 'server_cert' in config_data and config_data['server_cert']:
            self.config.verify = config_data['server_cert']
            self.logger.info('Configuring "verify": %s', self.config.verify)

        if 'client_cert' in config_data and 'client_key' in config_data \
                and config_data['client_cert'] and config_data['client_key']:
            self.config.cert = (config_data['client_cert'], config_data['client_key'])
            self.logger.info('Configuring "cert": %s', self.config.cert)

        if 'thread_count' in config_data and config_data['thread_count'] > 0:
            self.config.thread_cnt = config_data['thread_count']
            self.logger.info('Configuring "thread_cnt": %s', self.config.thread_cnt)

        if 'wsdl_replaces' in config_data:
            self.config.wsdl_replaces = config_data['wsdl_replaces']
            self.logger.info('Configuring "wsdl_replaces": %s', self.config.wsdl_replaces)

        if 'excluded_member_codes' in config_data:
            self.config.excluded_member_codes = config_data['excluded_member_codes']
            self.logger.info(
                'Configuring "excluded_member_codes": %s', self.config.excluded_member_codes)

        if 'excluded_subsystem_codes' in config_data:
            self.config.excluded_subsystem_codes = config_data['excluded_subsystem_codes']
            self.logger.info(
                'Configuring "excluded_subsystem_codes": %s', self.config.excluded_subsystem_codes)

        self.logger.info('Main configuration loaded')

    @staticmethod
    def _identifier_path(items: Sequence[str]) -> str:
        """Convert identifier in form of sequence to string
        representation of path. It is assumed that no symbols forbidden
        by storage plugins are used in identifiers.
        """
        return '/'.join(items)

    def _prepare_wsdl(self, wsdl: str) -> str:
        """Prepare WSDL for saving"""
        # Replacing dynamically generated comments in WSDL
        # to avoid new WSDL creation because of comments.
        for wsdl_replace in self.config.wsdl_replaces:
            wsdl = re.sub(wsdl_replace[0], wsdl_replace[1], wsdl)
        return wsdl

    @staticmethod
    def _method_item(
            method: Sequence[str], status: str, wsdl: str, doc_hash: str) -> Method:
        """Function that sets the correct structure for method item"""
        return Method(
            service_code=method[4],
            service_version=method[5],
            status=status,
            wsdl=wsdl,
            hash=doc_hash
        )

    @staticmethod
    def _service_item(
            service: Sequence[str], status: str, openapi: str, doc_hash: str,
            endpoints: Sequence[dict[str, str]]) -> Service:
        """Function that sets the correct structure for service item
        If status=='OK' and openapi is empty then:
          * it is REST X-Road service that does not have a description;
          * endpoints array is empty.
        If status=='OK' and openapi is not empty then:
          * it is OpenAPI X-Road service with description;
          * at least one endpoint must be present in OpenAPI description.
        In other cases status must not be 'OK' to indicate problem with
        the service.
        """
        return Service(
            service_code=service[4],
            status=status,
            openapi=openapi,
            hash=doc_hash,
            endpoints=endpoints
        )

    @staticmethod
    def _subsystem_item(
            path: str, subsystem: Sequence[str], methods_status: str, methods: dict[str, Method],
            services_status: str, services: Sequence[Service]) -> Subsystem:
        """Function that sets the correct structure for subsystem item"""
        sorted_methods = []
        for method_key in sorted(methods.keys()):
            sorted_methods.append(methods[method_key])

        return Subsystem(
            path=path,
            x_road_instance=subsystem[0],
            member_class=subsystem[1],
            member_code=subsystem[2],
            subsystem_code=subsystem[3],
            methods_status=methods_status,
            services_status=services_status,
            methods=sorted_methods,
            services=services
        )

    @staticmethod
    def _all_results_failed(subsystems: dict[str, Subsystem]) -> bool:
        """Check if all results have failed status"""
        for subsystem in subsystems.values():
            if subsystem.methods_status == 'OK':
                # Found non-failed subsystem
                return False
        # All results failed
        return True

    def _process_methods(
            self, subsystem: Sequence[str], subsystem_path: str) -> tuple[str, dict[str, Method]]:
        """
        Function that finds SOAP methods of a subsystem
        Return tuple: (Methods status, Methods)
        """
        wsdl_path, hashes = self.storage.subsystem_state(subsystem_path, 'wsdl')

        method_index = {}
        skip_methods = False
        try:
            # Converting iterator to list to properly capture exceptions
            methods = list(xrdinfo.methods(
                addr=self.config.server_url, client=self.config.client, producer=subsystem,
                method='listMethods', timeout=self.config.timeout, verify=self.config.verify,
                cert=self.config.cert))
        except xrdinfo.RequestTimeoutError as err:
            self.logger.info(
                'SOAP: %s: %s', self._identifier_path(subsystem), err)
            return 'TIMEOUT', {}
        except xrdinfo.XrdInfoError as err:
            self.logger.info('SOAP: %s: %s', self._identifier_path(subsystem), err)
            return 'ERROR', {}

        for method in sorted(methods):
            method_name = self._identifier_path(method)
            if method_name in method_index:
                # Method already found in previous WSDL's
                continue

            if skip_methods:
                # Skipping, because previous getWsdl request timed out
                self.logger.info('SOAP: %s - SKIPPING', method_name)
                method_index[method_name] = self._method_item(method, 'SKIPPED', '', '')
                continue

            try:
                wsdl = xrdinfo.wsdl(
                    addr=self.config.server_url, client=self.config.client, service=method,
                    timeout=self.config.timeout, verify=self.config.verify, cert=self.config.cert)
            except xrdinfo.RequestTimeoutError:
                # Skipping all following requests to that subsystem
                skip_methods = True
                self.logger.info('SOAP: %s - TIMEOUT', method_name)
                method_index[method_name] = self._method_item(method, 'TIMEOUT', '', '')
                continue
            except xrdinfo.XrdInfoError as err:
                self.logger.info('SOAP: %s: %s', method_name, err)
                method_index[method_name] = self._method_item(method, 'ERROR', '', '')
                continue

            wsdl = self._prepare_wsdl(wsdl)
            doc_name, doc_hash = self.storage.save_doc(wsdl_path, hashes, wsdl, 'wsdl', None)

            txt = f'SOAP: {doc_name}'
            try:
                for wsdl_method in xrdinfo.wsdl_methods(wsdl):
                    wsdl_method_name = self._identifier_path(list(subsystem) + list(wsdl_method))
                    # We can find other methods in a method WSDL
                    method_index[wsdl_method_name] = self._method_item(
                        list(subsystem) + list(wsdl_method), 'OK',
                        urlparse.quote(doc_name), doc_hash)
                    txt = txt + f'\n    {wsdl_method_name}'
            except xrdinfo.XrdInfoError as err:
                txt = txt + f'\nWSDL parsing failed: {err}'
                method_index[method_name] = self._method_item(method, 'ERROR', '', '')
            self.logger.info(txt)

            if method_name not in method_index:
                self.logger.warning(
                    'SOAP: %s - Method was not found in returned WSDL!', method_name)
                method_index[method_name] = self._method_item(method, 'ERROR', '', '')

        self.storage.save_subsystem_state(wsdl_path, hashes, 'wsdl')

        return 'OK', method_index

    def _process_services(
            self, subsystem: Sequence[str], subsystem_path: str) -> tuple[str, list[Service]]:
        """
        Function that finds REST services of a subsystem
        Return tuple: (Services status, Services)
        """
        openapi_path, hashes = self.storage.subsystem_state(subsystem_path, 'openapi')

        results = []
        skip_services = False

        try:
            # Converting iterator to list to properly capture exceptions
            services = list(xrdinfo.methods_rest(
                addr=self.config.server_url, client=self.config.client, producer=subsystem,
                method='listMethods', timeout=self.config.timeout, verify=self.config.verify,
                cert=self.config.cert))
        except xrdinfo.RequestTimeoutError as err:
            self.logger.info(
                'REST: %s: %s', self._identifier_path(subsystem), err)
            return 'TIMEOUT', []
        except xrdinfo.XrdInfoError as err:
            self.logger.info('REST: %s: %s', self._identifier_path(subsystem), err)
            return 'ERROR', []

        for service in sorted(services):
            service_name = self._identifier_path(service)

            if skip_services:
                # Skipping, because previous getOpenAPI request timed out
                self.logger.info('REST: %s - SKIPPING', service_name)
                results.append(self._service_item(service, 'SKIPPED', '', '', []))
                continue

            try:
                openapi = xrdinfo.openapi(
                    addr=self.config.server_url, client=self.config.client, service=service,
                    timeout=self.config.timeout, verify=self.config.verify, cert=self.config.cert)
            except xrdinfo.RequestTimeoutError:
                # Skipping all following requests to that subsystem
                skip_services = True
                self.logger.info('REST: %s - TIMEOUT', service_name)
                results.append(self._service_item(service, 'TIMEOUT', '', '', []))
                continue
            except xrdinfo.NotOpenapiServiceError:
                results.append(self._service_item(service, 'OK', '', '', []))
                continue
            except xrdinfo.XrdInfoError as err:
                self.logger.info('REST: %s: %s', service_name, err)
                results.append(self._service_item(service, 'ERROR', '', '', []))
                continue

            try:
                _, openapi_type = xrdinfo.load_openapi(openapi)
                endpoints = xrdinfo.openapi_endpoints(openapi)
            except xrdinfo.XrdInfoError as err:
                self.logger.info('REST: %s: %s', service_name, err)
                results.append(self._service_item(service, 'ERROR', '', '', []))
                continue

            doc_name, doc_hash = self.storage.save_doc(
                openapi_path, hashes, openapi, openapi_type, service[4])

            results.append(
                self._service_item(service, 'OK', urlparse.quote(doc_name), doc_hash, endpoints))

        self.storage.save_subsystem_state(openapi_path, hashes, 'openapi')

        return 'OK', results

    def _worker(self) -> None:
        """Main method for worker threads"""
        while True:
            # Checking periodically if it is the time to gracefully shut down the worker.
            try:
                subsystem = self.work_queue.get(True, 0.1)
                # Emptying work queue without processing if storage plugin failed
                if not self.storage.active():
                    self.logger.info(
                        'Skipping %s because of storage failure', self._identifier_path(subsystem))
                    self.work_queue.task_done()
                    continue
                self.logger.info('Start processing %s', self._identifier_path(subsystem))
            except queue.Empty:
                if self.shutdown.is_set():
                    return
                continue

            subsystem_path = ''
            try:
                subsystem_path = self._identifier_path(subsystem)
                methods_status, methods_result = self._process_methods(subsystem, subsystem_path)
                services_status, services_result = self._process_services(subsystem, subsystem_path)

                with self.results_lock:
                    self.results[subsystem_path] = self._subsystem_item(
                        subsystem_path, subsystem, methods_status, methods_result,
                        services_status, services_result)
            # Using broad exception to avoid unexpected exits of workers
            except Exception as err:  # pylint: disable=broad-exception-caught
                with self.results_lock:
                    self.results[subsystem_path] = self._subsystem_item(
                        subsystem_path, subsystem, 'ERROR', {}, 'ERROR', [])
                self.logger.warning('Unexpected exception: %s: %s', type(err).__name__, err)
                self.logger.debug('%s', traceback.format_exc())
            finally:
                self.work_queue.task_done()

    def _process_results(self) -> None:
        """Process results collected by worker threads"""
        if not self.storage.active():
            # Skipping this version
            self.logger.error('Storage plugin failed, skipping this catalogue version!')
            self._exit_program(1)

        if self._all_results_failed(self.results):
            # Skipping this version
            self.logger.error('All subsystems failed, skipping this catalogue version!')
            self._exit_program(1)

        self.storage.save_catalogue(self.results)

    def _exit_program(self, exit_code: int) -> None:
        """Exit program after deactivating plugin"""
        if hasattr(self, 'storage') and self.storage.active():
            self.storage.deactivate()

        raise SystemExit(exit_code)

    def collect(self) -> int:
        """Start X-Road collector"""
        # Initializing 'shared_params' so that editors would not think
        # it could be used before initialization
        shared_params: str = ''
        try:
            shared_params = xrdinfo.shared_params_ss(
                addr=self.config.server_url, instance=self.config.instance,
                timeout=self.config.timeout, verify=self.config.verify, cert=self.config.cert)
        except xrdinfo.XrdInfoError as err:
            self.logger.error('Cannot download Global Configuration: %s', err)
            self._exit_program(1)

        # Create and start new threads
        threads = []
        for _ in range(self.config.thread_cnt):
            thread = Thread(target=self._worker, args=())
            thread.daemon = True
            thread.start()
            threads.append(thread)

        # Populate the queue
        try:
            for subsystem in xrdinfo.registered_subsystems(shared_params):
                if subsystem[2] in self.config.excluded_member_codes:
                    self.logger.info(
                        'Skipping excluded member %s', self._identifier_path(subsystem))
                    continue
                if [subsystem[2], subsystem[3]] in self.config.excluded_subsystem_codes:
                    self.logger.info(
                        'Skipping excluded subsystem %s', self._identifier_path(subsystem))
                    continue
                self.work_queue.put(subsystem)
        except xrdinfo.XrdInfoError as err:
            self.logger.error('Cannot process Global Configuration: %s', err)
            self.logger.debug('%s', traceback.format_exc())
            self._exit_program(1)

        # Block until all tasks in queue are done
        self.work_queue.join()

        # Set shutdown event and wait until all daemon processes finish
        self.shutdown.set()
        for thread in threads:
            thread.join()

        self._process_results()

        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Main function"""
    raise SystemExit(Collector(argv).collect())
