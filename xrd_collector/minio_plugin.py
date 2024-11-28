"""MinIO storage plugin for xrd_collector"""

# FS and MinIO plugins have some duplicate code because they are using
# the same file based storage model. Reduction of code duplication
# may increase complexity of each plugin and decrease readability.
# pylint: disable=duplicate-code
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from io import BytesIO
import json
import os
import re
import time
from typing import Any

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error
import urllib3
from xrd_collector.storage import PluginBase, PluginError, deactivate_on_fail
from xrd_collector import util

HISTORY_FILE_NAME = 'history.json'


@dataclass
class Config:
    """Plugin configuration"""
    minio_url: str = ''
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_secure: bool = True
    minio_ca_certs: str | None = None
    minio_bucket: str = 'catalogue'
    minio_path: str = ''
    # Instance is required for documents cleanup
    instance: str = ''
    filtered_hours: int = 24
    filtered_days: int = 30
    filtered_months: int = 12
    cleanup_interval: int = 7
    days_to_keep: int = 30


class MinIOPlugin(PluginBase):
    """Class for MinIO storage plugin"""
    client: Minio

    def __init__(self, config_data, logger) -> None:
        self.logger = logger
        self._config(config_data)
        self._prepare_client()

    def _config(self, config_data: dict[str, Any]) -> None:
        """Process plugin configuration"""
        # Default configuration
        self.config = Config()

        if 'minio_url' in config_data and config_data['minio_url']:
            self.config.minio_url = config_data['minio_url']
            self.logger.info('Configuring "minio_url": %s', self.config.minio_url)
        else:
            raise PluginError('Configuration error: "minio_url" is not configured')

        if 'minio_access_key' in config_data:
            self.config.minio_access_key = config_data['minio_access_key']
            self.logger.info(
                'Configuring "minio_access_key": %s', self.config.minio_access_key)

        if 'minio_secret_key' in config_data:
            self.config.minio_secret_key = config_data['minio_secret_key']
            self.logger.info('Configuring "minio_secret_key": <password hidden>')

        if 'minio_secure' in config_data:
            self.config.minio_secure = config_data['minio_secure']
            self.logger.info(
                'Configuring "minio_secure": %s', self.config.minio_secure)

        if 'minio_ca_certs' in config_data:
            self.config.minio_ca_certs = config_data['minio_ca_certs']
            self.logger.info(
                'Configuring "minio_ca_certs": %s', self.config.minio_ca_certs)

        if 'minio_bucket' in config_data:
            self.config.minio_bucket = config_data['minio_bucket']
            self.logger.info(
                'Configuring "minio_bucket": %s', self.config.minio_bucket)

        if 'minio_path' in config_data:
            self.config.minio_path = config_data['minio_path']
            self.config.minio_path.strip('/')
            if self.config.minio_path:
                self.config.minio_path += '/'
            self.logger.info(
                'Configuring "minio_path": %s', self.config.minio_path)

        if 'instance' in config_data and config_data['instance']:
            self.config.instance = config_data['instance']
            self.logger.info('Configuring "instance": %s', self.config.instance)

        if 'filtered_hours' in config_data and config_data['filtered_hours'] > 0:
            self.config.filtered_hours = config_data['filtered_hours']
            self.logger.info(
                'Configuring "filtered_hours": %s', self.config.filtered_hours)

        if 'filtered_days' in config_data and config_data['filtered_days'] > 0:
            self.config.filtered_days = config_data['filtered_days']
            self.logger.info(
                'Configuring "filtered_days": %s', self.config.filtered_days)

        if 'filtered_months' in config_data and config_data['filtered_months'] > 0:
            self.config.filtered_months = config_data['filtered_months']
            self.logger.info(
                'Configuring "filtered_months": %s', self.config.filtered_months)

        if 'cleanup_interval' in config_data and config_data['cleanup_interval'] > 0:
            self.config.cleanup_interval = config_data['cleanup_interval']
            self.logger.info(
                'Configuring "cleanup_interval": %s', self.config.cleanup_interval)

        if 'days_to_keep' in config_data and config_data['days_to_keep'] > 0:
            self.config.days_to_keep = config_data['days_to_keep']
            self.logger.info(
                'Configuring "days_to_keep": %s', self.config.days_to_keep)

    def _prepare_client(self):
        """Prepares MinIO client"""
        if self.config.minio_ca_certs:
            http_client = urllib3.PoolManager(
                ca_certs=self.config.minio_ca_certs
            )
            self.client = Minio(
                self.config.minio_url,
                access_key=self.config.minio_access_key,
                secret_key=self.config.minio_secret_key,
                secure=self.config.minio_secure,
                http_client=http_client)
        else:
            self.client = Minio(
                self.config.minio_url,
                access_key=self.config.minio_access_key,
                secret_key=self.config.minio_secret_key,
                secure=self.config.minio_secure)

    @staticmethod
    def _minio_prefix(path: str) -> str:
        """
        Minio list_objects expects prefix to be empty or ending with '/' for subfolders
        Return: Fixed prefix
        """
        if path:
            return f'{path.strip("/")}/'
        return ''

    def _hash_docs(self, path: str, doc_type: str):
        """Find hashes of all documents with specified document type in directory"""
        hashes = {}
        path_prefix = self._minio_prefix(path)
        for obj in self.client.list_objects(
                self.config.minio_bucket, prefix=path_prefix, recursive=False):
            file_name = obj.object_name[len(path_prefix):]
            match doc_type:
                case 'wsdl':
                    pattern = r'^(\d+)\.wsdl$'
                case 'openapi':
                    pattern = r'^.+_(\d+)\.(yaml|json)$'
                case _:
                    raise PluginError(f'Unknown document type: "{doc_type}"')
            search_res = re.search(pattern, file_name)
            if search_res:
                doc_object = self.client.get_object(
                    self.config.minio_bucket, os.path.join(path, file_name))
                hashes[file_name] = hashlib.md5(doc_object.data).hexdigest()
        return hashes

    def _get_hashes(self, path: str, doc_type: str):
        """Get document hashes of the specified document types in a directory"""
        try:
            wsdl_hashes_file = self.client.get_object(
                self.config.minio_bucket, os.path.join(path, f'_{doc_type}_hashes'))
            hashes = json.loads(wsdl_hashes_file.data.decode('utf-8'))
        except S3Error:
            hashes = self._hash_docs(path, doc_type)
        return hashes

    def _write_json(self, file_name: str, json_data: Any):
        """Write data to JSON file"""
        json_binary = json.dumps(json_data, indent=2, ensure_ascii=False).encode()
        self.client.put_object(
            self.config.minio_bucket, file_name,
            BytesIO(json_binary), len(json_binary), content_type='application/json')

    def _get_catalogue_reports(self, history=False):
        """Get list of reports"""
        reports = []
        path_prefix = self._minio_prefix(self.config.minio_path)
        for obj in self.client.list_objects(
                self.config.minio_bucket, prefix=path_prefix, recursive=False):
            file_name = obj.object_name[len(self.config.minio_path):]
            util.add_report_file(file_name, reports, history=history)
        reports.sort(key=util.sort_by_report_time, reverse=True)
        return reports

    def _get_old_reports(self):
        """Get old reports that need to be removed"""
        old_reports = []
        all_reports = self._get_catalogue_reports()
        cur_time = datetime.today()
        fresh_time = datetime(cur_time.year, cur_time.month, cur_time.day) - timedelta(
            days=self.config.days_to_keep)
        paths_to_keep = util.get_reports_to_keep(all_reports, fresh_time)

        for report in all_reports:
            if report['reportPath'] not in paths_to_keep:
                old_reports.append(report['reportPath'])

        old_reports.sort()
        return old_reports

    def _get_reports_set(self):
        """Get set of reports"""
        reports = set()
        path_prefix = self._minio_prefix(self.config.minio_path)
        for obj in self.client.list_objects(
                self.config.minio_bucket, prefix=path_prefix, recursive=False):
            file_name = obj.object_name[len(self.config.minio_path):]
            search_res = re.search(
                r'^index_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\.json$',
                file_name)
            if search_res:
                reports.add(file_name)
        return reports

    def _get_docs_in_report(self, report_file):
        obj = self.client.get_object(
            self.config.minio_bucket, os.path.join(self.config.minio_path, report_file))
        report_data = json.loads(obj.data.decode('utf-8'))

        used_docs = set()
        for system in report_data:
            for method in system['methods']:
                if method['wsdl']:
                    used_docs.add(os.path.join(self.config.minio_path, method['wsdl']))
            if 'services' in system:
                for service in system['services']:
                    if service['openapi']:
                        used_docs.add(os.path.join(self.config.minio_path, service['openapi']))
        return used_docs

    def _get_available_docs(self):
        available_docs = set()
        path_prefix = self._minio_prefix(
            os.path.join(self.config.minio_path, self.config.instance))
        for obj in self.client.list_objects(
                self.config.minio_bucket, prefix=path_prefix, recursive=True):
            util.add_doc_file(
                os.path.basename(obj.object_name), os.path.dirname(obj.object_name),
                available_docs)
        return available_docs

    def _get_unused_docs(self):
        reports = self._get_reports_set()
        if not reports:
            self.logger.warning('Did not find any reports!')
            return set()

        used_docs = set()
        for report_file in reports:
            used_docs = used_docs.union(self._get_docs_in_report(report_file))
        if not used_docs:
            self.logger.info(
                'Did not find any documents in reports. This is might be an error.')
            return set()

        available_docs = self._get_available_docs()
        return available_docs - used_docs

    def _cleanup(self) -> None:
        """Perform storage cleanup and remove old catalogue versions and documents"""
        last_cleanup = None
        try:
            json_file = self.client.get_object(
                self.config.minio_bucket,
                os.path.join(self.config.minio_path, 'cleanup_status.json'))
            cleanup_status = json.loads(json_file.data.decode('utf-8'))
            last_cleanup = datetime.strptime(cleanup_status['lastCleanup'], util.DATE_FORMAT)
        except (S3Error, ValueError):
            self.logger.info('Cleanup status not found')

        if last_cleanup:
            if datetime.today() - timedelta(
                    days=self.config.cleanup_interval) < util.day_start(last_cleanup):
                self.logger.info('Cleanup interval is not passed yet')
                return

        self.logger.info('Starting cleanup')

        # Cleanup reports
        old_reports = self._get_old_reports()
        if old_reports:
            self.logger.info('Removing %s old JSON reports:', len(old_reports))
            for report_path in old_reports:
                self.logger.info(
                    'Removing %s', os.path.join(self.config.minio_path, report_path))
                self.client.remove_object(
                    self.config.minio_bucket, os.path.join(self.config.minio_path, report_path))

            # Recreating history.json
            reports = self._get_catalogue_reports(history=True)
            if reports:
                self.logger.info('Writing %s reports to history.json', len(reports))
                self._write_json(os.path.join(self.config.minio_path, HISTORY_FILE_NAME), reports)
        else:
            self.logger.info(
                'No old JSON reports found in directory: %s', self.config.minio_path)

        # Cleanup documents
        unused_docs = self._get_unused_docs()
        changed_dirs = set()
        if unused_docs:
            self.logger.info(f'Removing {len(unused_docs)} unused document(s):')
            for doc_path in unused_docs:
                self.logger.info('Removing %s', doc_path)
                self.client.remove_object(self.config.minio_bucket, doc_path)
                changed_dirs.add(os.path.dirname(doc_path))
        else:
            self.logger.info('No unused documents found')

        # Recreating document hashes cache
        for doc_path in changed_dirs:
            self.logger.info('Recreating WSDL hashes cache for %s', doc_path)
            hashes = self._hash_docs(doc_path, 'wsdl')
            self.save_subsystem_state(doc_path, hashes, 'wsdl')
            self.logger.info('Recreating OpenAPI hashes cache for %s', doc_path)
            hashes = self._hash_docs(doc_path, 'openapi')
            self.save_subsystem_state(doc_path, hashes, 'openapi')

        # Updating status
        cleanup_time = time.strftime(util.DATE_FORMAT, time.localtime(time.time()))
        json_status = {'lastCleanup': cleanup_time}
        self._write_json(os.path.join(self.config.minio_path, 'cleanup_status.json'), json_status)

    @deactivate_on_fail
    def subsystem_state(self, subsystem_path: str, doc_type: str) -> tuple[
            str, dict[str, str]]:
        """
        Get storage state for a subsystem
        Return tuple: (Documents path used by subsystem, Document hashes)
        """
        path = os.path.join(self.config.minio_path, subsystem_path)
        try:
            hashes = self._get_hashes(path, doc_type)
        except S3Error as err:
            raise PluginError(err) from err
        return path, hashes

    @deactivate_on_fail
    def save_subsystem_state(
            self, path: str, hashes: dict[str, str], doc_type: str) -> None:
        """Save document hashes to subsystem state"""
        self._write_json(f'{path}/_{doc_type}_hashes', hashes)

    @deactivate_on_fail
    def save_doc(
            self, path: str, hashes: dict[str, str], doc: str, file_ext: str,
            service_name: str | None) -> tuple[str, str]:
        """
        Save service description document if it does not exist yet
        Return tuple: (Document name, Document hash)
        """
        doc_hash = hashlib.md5(doc.encode('utf-8')).hexdigest()
        max_doc = -1
        for file_name in hashes.keys():
            match file_ext:
                case 'wsdl':
                    pattern = r'^(\d+)\.wsdl$'
                case 'yaml' | 'json':
                    pattern = rf'^{service_name}_(\d+)\.(yaml|json)$'
                case _:
                    raise PluginError(f'Unknown file extension: "{file_ext}"')
            search_res = re.search(pattern, file_name)
            if search_res:
                if doc_hash == hashes[file_name]:
                    # Matching document found (both name pattern and hash)
                    return file_name, doc_hash
                max_doc = max(max_doc, int(search_res.group(1)))
        # Creating new file
        match file_ext:
            case 'wsdl':
                content_type = 'text/xml'
                new_file = f'{int(max_doc) + 1}.wsdl'
            case 'yaml':
                content_type = 'text/yaml'
                new_file = f'{service_name}_{int(max_doc) + 1}.yaml'
            case 'json':
                content_type = 'application/json'
                new_file = f'{service_name}_{int(max_doc) + 1}.json'
            case _:
                raise PluginError(f'Unknown file extension: "{file_ext}"')
        openapi_binary = doc.encode('utf-8')
        self.client.put_object(
            self.config.minio_bucket, os.path.join(path, new_file),
            BytesIO(openapi_binary), len(openapi_binary), content_type=content_type)
        hashes[new_file] = doc_hash
        return new_file, doc_hash

    @deactivate_on_fail
    def save_catalogue(self, results: dict[str, util.Subsystem]) -> None:
        """Save service catalogue"""
        json_data = []
        for subsystem_key in sorted(results.keys()):
            json_data.append(util.export_subsystem(results[subsystem_key]))

        report_time = time.localtime(time.time())
        formatted_time = time.strftime(util.DATE_FORMAT, report_time)
        suffix = time.strftime('%Y%m%d%H%M%S', report_time)

        self._write_json(os.path.join(self.config.minio_path, f'index_{suffix}.json'), json_data)

        json_history = []
        try:
            json_history_file = self.client.get_object(
                self.config.minio_bucket, os.path.join(self.config.minio_path, HISTORY_FILE_NAME))
            json_history = json.loads(json_history_file.data.decode('utf-8'))
        except S3Error:
            self.logger.info('History file history.json not found')

        json_history.append({'reportTime': formatted_time, 'reportPath': f'index_{suffix}.json'})
        json_history.sort(key=util.sort_by_report_time, reverse=True)

        self._write_json(os.path.join(self.config.minio_path, HISTORY_FILE_NAME), json_history)
        self._write_json(
            os.path.join(self.config.minio_path, 'filtered_history.json'),
            util.filtered_history(
                json_history, self.config.filtered_hours,
                self.config.filtered_days, self.config.filtered_months))

        # Replace index.json with latest report
        self.client.copy_object(
            self.config.minio_bucket, os.path.join(self.config.minio_path, 'index.json'),
            CopySource(
                self.config.minio_bucket,
                os.path.join(self.config.minio_path, f'index_{suffix}.json')))

        # Updating status
        json_status = {'lastReport': formatted_time}
        self._write_json(os.path.join(self.config.minio_path, 'status.json'), json_status)

        self._cleanup()
