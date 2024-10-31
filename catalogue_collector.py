#!/usr/bin/env python3

"""This is a module for collection of X-Road services information."""

__version__ = '1.2.2'
__author__ = 'Vitali Stupin'

import queue
from threading import Thread, Event, Lock
from datetime import datetime, timedelta
from io import BytesIO
import argparse
import hashlib
import json
import logging.config
import os
import re
import shutil
import sys
import time
import urllib.parse as urlparse
import urllib3
from minio import Minio
from minio.error import S3Error
import xrdinfo

# TODO: Refactor to use os.path.join instead of '{}{}' and '{}/{}' for path joining
#       Use common path for files params['path'], params['minio_path'] -> params['cat_path']

# Default timeout for HTTP requests
DEFAULT_TIMEOUT = 5.0

# Do not use threading by default
DEFAULT_THREAD_COUNT = 1

DEFAULT_WSDL_REPLACES = [
    # [Pattern, Replacement]
    # Example:
    # "Genereerimise aeg: 22.03.2019 08:00:30"
    # [
    #     'Genereerimise aeg: \\d{2}\\.\\d{2}\\.\\d{4} \\d{2}:\\d{2}:\\d{2}',
    #     'Genereerimise aeg: DELETED'
    # ]
]

# This logger will be used before loading of logger configuration
DEFAULT_LOGGER = {
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

# Application will use this logger
LOGGER = logging.getLogger('catalogue-collector')


def identifier_path(items):
    """Convert identifier in form of list/tuple to string representation
    of filesystem path. We assume that no symbols forbidden by
    filesystem are used in identifiers.
    """
    return '/'.join(items)


def load_config(config_file):
    """Load configuration from JSON file"""
    try:
        with open(config_file, 'r') as conf:
            return json.load(conf)
    except IOError as err:
        LOGGER.error('Cannot load configuration file "%s": %s', config_file, str(err))
        return None
    except json.JSONDecodeError as err:
        LOGGER.error('Invalid JSON configuration file "%s": %s', config_file, str(err))
        return None


def configure_logging(config):
    """Configure logging based on loaded configuration"""
    if 'logging-config' in config:
        logging.config.dictConfig(config['logging-config'])
        LOGGER.info('Logger configured')


def set_params(config):
    """Configure parameters based on loaded configuration"""
    params = {
        'path': None,
        'minio': None,
        'minio_access_key': None,
        'minio_secret_key': None,
        'minio_secure': True,
        'minio_ca_certs': None,
        'minio_bucket': 'catalogue',
        'minio_path': '',
        'url': None,
        'client': None,
        'instance': None,
        'timeout': DEFAULT_TIMEOUT,
        'verify': False,
        'cert': None,
        'thread_cnt': DEFAULT_THREAD_COUNT,
        'wsdl_replaces': DEFAULT_WSDL_REPLACES,
        'excluded_member_codes': [],
        'excluded_subsystem_codes': [],
        'filtered_hours': 24,
        'filtered_days': 30,
        'filtered_months': 12,
        'cleanup_interval': 7,
        'days_to_keep': 30,
        'work_queue': queue.Queue(),
        'results': {},
        'results_lock': Lock(),
        'shutdown': Event()
    }

    if 'output_path' in config:
        params['path'] = config['output_path']
        LOGGER.info('Configuring "path": %s', params['path'])

    if 'minio_url' in config:
        params['minio'] = config['minio_url']
        LOGGER.info('Configuring "minio_url": %s', params['minio'])

    if 'minio_access_key' in config:
        params['minio_access_key'] = config['minio_access_key']
        LOGGER.info('Configuring "minio_access_key": %s', params['minio_access_key'])

    if 'minio_secret_key' in config:
        params['minio_secret_key'] = config['minio_secret_key']
        LOGGER.info('Configuring "minio_secret_key": <password hidden>')

    if 'minio_secure' in config:
        params['minio_secure'] = config['minio_secure']
        LOGGER.info('Configuring "minio_secure": %s', params['minio_secure'])

    if 'minio_ca_certs' in config:
        params['minio_ca_certs'] = config['minio_ca_certs']
        LOGGER.info('Configuring "minio_ca_certs": %s', params['minio_ca_certs'])

    if 'minio_bucket' in config:
        params['minio_bucket'] = config['minio_bucket']
        LOGGER.info('Configuring "minio_bucket": %s', params['minio_bucket'])

    if 'minio_path' in config:
        params['minio_path'] = config['minio_path']
        params['minio_path'].strip('/')
        if params['minio_path']:
            params['minio_path'] += '/'
        LOGGER.info('Configuring "minio_path": %s', params['minio_path'])

    if params['path'] is None and params['minio'] is None:
        LOGGER.error('Configuration error: No output path or MinIO URL are provided')
        return None

    if 'server_url' in config:
        params['url'] = config['server_url']
        LOGGER.info('Configuring "url": %s', params['url'])
    else:
        LOGGER.error('Configuration error: Local Security Server URL is not provided')
        return None

    if 'client' in config and len(config['client']) in (3, 4):
        params['client'] = config['client']
        LOGGER.info('Configuring "client": %s', params['client'])
    else:
        LOGGER.error(
            'Configuration error: Client identifier is incorrect. Expecting list of identifiers. '
            'Example: ["INST", "CLASS", "MEMBER_CODE", "MEMBER_CLASS"])')
        return None

    if 'instance' in config and config['instance']:
        params['instance'] = config['instance']
        LOGGER.info('Configuring "instance": %s', params['instance'])

    if 'timeout' in config and config['timeout'] > 0.0:
        params['timeout'] = config['timeout']
        LOGGER.info('Configuring "timeout": %s', params['timeout'])

    if 'server_cert' in config and config['server_cert']:
        params['verify'] = config['server_cert']
        LOGGER.info('Configuring "verify": %s', params['verify'])

    if 'client_cert' in config and 'client_key' in config \
            and config['client_cert'] and config['client_key']:
        params['cert'] = (config['client_cert'], config['client_key'])
        LOGGER.info('Configuring "cert": %s', params['cert'])

    if 'thread_count' in config and config['thread_count'] > 0:
        params['thread_cnt'] = config['thread_count']
        LOGGER.info('Configuring "thread_cnt": %s', params['thread_cnt'])

    if 'wsdl_replaces' in config:
        params['wsdl_replaces'] = config['wsdl_replaces']
        LOGGER.info('Configuring "wsdl_replaces": %s', params['wsdl_replaces'])

    if 'excluded_member_codes' in config:
        params['excluded_member_codes'] = config['excluded_member_codes']
        LOGGER.info('Configuring "excluded_member_codes": %s', params['excluded_member_codes'])

    if 'excluded_subsystem_codes' in config:
        params['excluded_subsystem_codes'] = config['excluded_subsystem_codes']
        LOGGER.info(
            'Configuring "excluded_subsystem_codes": %s', params['excluded_subsystem_codes'])

    if 'filtered_hours' in config and config['filtered_hours'] > 0:
        params['filtered_hours'] = config['filtered_hours']
        LOGGER.info('Configuring "filtered_hours": %s', params['filtered_hours'])

    if 'filtered_days' in config and config['filtered_days'] > 0:
        params['filtered_days'] = config['filtered_days']
        LOGGER.info('Configuring "filtered_days": %s', params['filtered_days'])

    if 'filtered_months' in config and config['filtered_months'] > 0:
        params['filtered_months'] = config['filtered_months']
        LOGGER.info('Configuring "filtered_months": %s', params['filtered_months'])

    if 'cleanup_interval' in config and config['cleanup_interval'] > 0:
        params['cleanup_interval'] = config['cleanup_interval']
        LOGGER.info('Configuring "cleanup_interval": %s', params['cleanup_interval'])

    if 'days_to_keep' in config and config['days_to_keep'] > 0:
        params['days_to_keep'] = config['days_to_keep']
        LOGGER.info('Configuring "days_to_keep": %s', params['days_to_keep'])

    if params['path'] is not None and params['minio'] is not None:
        LOGGER.warning('Saving to both local and MinIO storage is not supported')

    if params['minio']:
        LOGGER.info('Using MinIO storage')
    else:
        LOGGER.info('Using local storage')

    LOGGER.info('Configuration done')

    return params


def prepare_minio_client(params):
    """Creates minio client and stores that in params"""
    if params['minio_ca_certs']:
        http_client = urllib3.PoolManager(
            ca_certs=params['minio_ca_certs']
        )
        params['minio_client'] = Minio(
            params['minio'],
            access_key=params['minio_access_key'],
            secret_key=params['minio_secret_key'],
            secure=params['minio_secure'],
            http_client=http_client)
    else:
        params['minio_client'] = Minio(
            params['minio'],
            access_key=params['minio_access_key'],
            secret_key=params['minio_secret_key'],
            secure=params['minio_secure'])


def make_dirs(path):
    """Create directories if they do not exist"""
    try:
        os.makedirs(path)
    except OSError:
        pass
    if not os.path.exists(path):
        LOGGER.error('Cannot create directory "%s"', path)
        return False
    return True


def hash_wsdls(path, params):
    """Find hashes of all WSDL's in directory"""
    hashes = {}
    if params['minio']:
        for obj in params['minio_client'].list_objects(
                params['minio_bucket'], prefix=path, recursive=False):
            file_name = obj.object_name[len(path):]
            search_res = re.search('^(\\d+)\\.wsdl$', file_name)
            if search_res:
                wsdl_object = params['minio_client'].get_object(
                    params['minio_bucket'], '{}{}'.format(path, file_name))
                hashes[file_name] = hashlib.md5(wsdl_object.data).hexdigest()
    else:
        for file_name in os.listdir(path):
            search_res = re.search('^(\\d+)\\.wsdl$', file_name)
            if search_res:
                # Reading as bytes to avoid line ending conversion
                with open('{}/{}'.format(path, file_name), 'rb') as wsdl_file:
                    wsdl = wsdl_file.read()
                hashes[file_name] = hashlib.md5(wsdl).hexdigest()
    return hashes


def get_wsdl_hashes(path, params):
    """Get WSDL hashes in a directory"""
    if params['minio']:
        try:
            wsdl_hashes_file = params['minio_client'].get_object(
                params['minio_bucket'], '{}_wsdl_hashes'.format(path))
            hashes = json.loads(wsdl_hashes_file.data.decode('utf-8'))
        except S3Error:
            hashes = hash_wsdls(path, params)
    else:
        try:
            with open('{}/_wsdl_hashes'.format(params['path']), 'r') as json_file:
                hashes = json.load(json_file)
        except IOError:
            hashes = hash_wsdls(path, params)
    return hashes


def save_wsdl(path, hashes, wsdl, wsdl_replaces, params):
    """Save WSDL if it does not exist yet"""
    # Replacing dynamically generated comments in WSDL to avoid new WSDL
    # creation because of comments.
    for wsdl_replace in wsdl_replaces:
        wsdl = re.sub(wsdl_replace[0], wsdl_replace[1], wsdl)
    wsdl_hash = hashlib.md5(wsdl.encode('utf-8')).hexdigest()
    max_wsdl = -1
    for file_name in hashes.keys():
        if wsdl_hash == hashes[file_name]:
            # Matching WSDL found
            return file_name, hashes
        search_res = re.search('^(\\d+)\\.wsdl$', file_name)
        if search_res:
            if int(search_res.group(1)) > max_wsdl:
                max_wsdl = int(search_res.group(1))
    # Creating new file
    new_file = '{}.wsdl'.format(int(max_wsdl) + 1)
    wsdl_binary = wsdl.encode('utf-8')
    if params['minio']:
        params['minio_client'].put_object(
            params['minio_bucket'], '{}{}'.format(path, new_file),
            BytesIO(wsdl_binary), len(wsdl_binary), content_type='text/xml')
    else:
        # Writing as bytes to avoid line ending conversion
        with open('{}/{}'.format(path, new_file), 'wb') as wsdl_file:
            wsdl_file.write(wsdl_binary)
    hashes[new_file] = wsdl_hash
    return new_file, hashes


def hash_openapis(path, params):
    """Find hashes of all OpenAPI documents in directory"""
    hashes = {}
    if params['minio']:
        for obj in params['minio_client'].list_objects(
                params['minio_bucket'], prefix=path, recursive=False):
            file_name = obj.object_name[len(path):]
            search_res = re.search('^.+_(\\d+)\\.(yaml|json)$', file_name)
            if search_res:
                openapi_object = params['minio_client'].get_object(
                    params['minio_bucket'], '{}{}'.format(path, file_name))
                hashes[file_name] = hashlib.md5(openapi_object.data).hexdigest()
    else:
        for file_name in os.listdir(path):
            search_res = re.search('^.+_(\\d+)\\.(yaml|json)$', file_name)
            if search_res:
                # Reading as bytes to avoid line ending conversion
                with open('{}/{}'.format(path, file_name), 'rb') as openapi_file:
                    openapi = openapi_file.read()
                hashes[file_name] = hashlib.md5(openapi).hexdigest()
    return hashes


def get_openapi_hashes(path, params):
    """Get OpenAPI hashes in a directory"""
    if params['minio']:
        try:
            openapi_hashes_file = params['minio_client'].get_object(
                params['minio_bucket'], '{}_openapi_hashes'.format(path))
            hashes = json.loads(openapi_hashes_file.data.decode('utf-8'))
        except S3Error:
            hashes = hash_openapis(path, params)
    else:
        try:
            with open('{}/_openapi_hashes'.format(params['path']), 'r') as json_file:
                hashes = json.load(json_file)
        except IOError:
            hashes = hash_openapis(path, params)
    return hashes


def save_openapi(path, hashes, openapi, service_name, doc_type, params):
    """Save OpenAPI if it does not exist yet"""
    openapi_hash = hashlib.md5(openapi.encode('utf-8')).hexdigest()
    max_openapi = -1
    for file_name in hashes.keys():
        search_res = re.search('^{}_(\\d+)\\.(yaml|json)$'.format(service_name), file_name)
        if search_res:
            if openapi_hash == hashes[file_name]:
                # Matching OpenAPI found (both name pattern and hash)
                return file_name, hashes
            if int(search_res.group(1)) > max_openapi:
                max_openapi = int(search_res.group(1))
    # Creating new file
    new_file = '{}_{}.{}'.format(service_name, int(max_openapi) + 1, doc_type)
    openapi_binary = openapi.encode('utf-8')
    content_type = 'text/yaml'
    if doc_type == 'json':
        content_type = 'application/json'
    if params['minio']:
        params['minio_client'].put_object(
            params['minio_bucket'], '{}{}'.format(path, new_file),
            BytesIO(openapi_binary), len(openapi_binary), content_type=content_type)
    else:
        # Writing as bytes to avoid line ending conversion
        with open('{}/{}'.format(path, new_file), 'wb') as openapi_file:
            openapi_file.write(openapi.encode('utf-8'))
    hashes[new_file] = openapi_hash
    return new_file, hashes


def save_hashes(path, hashes, file_type, params):
    """Save hashes of WSDL/OpenAPI documents (to speedup MinIO)"""
    if params['minio']:
        hashes_binary = json.dumps(hashes, indent=2, ensure_ascii=False).encode()
        params['minio_client'].put_object(
            params['minio_bucket'], '{}_{}_hashes'.format(path, file_type),
            BytesIO(hashes_binary), len(hashes_binary), content_type='text/plain')
    else:
        write_json('{}/_{}_hashes'.format(path, file_type), hashes, params)


def method_item(method, status, wsdl):
    """Function that sets the correct structure for method item"""
    return {
        'serviceCode': method[4],
        'serviceVersion': method[5],
        'methodStatus': status,
        'wsdl': wsdl
    }


def service_item(service, status, openapi, endpoints):
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
    return {
        'serviceCode': service[4],
        'status': status,
        'openapi': openapi,
        'endpoints': endpoints
    }


def subsystem_item(subsystem, methods, services):
    """Function that sets the correct structure for subsystem item"""
    subsystem_status = 'ERROR'
    sorted_methods = []
    if methods is not None:
        subsystem_status = 'OK'
        for method_key in sorted(methods.keys()):
            sorted_methods.append(methods[method_key])

    return {
        'xRoadInstance': subsystem[0],
        'memberClass': subsystem[1],
        'memberCode': subsystem[2],
        'subsystemCode': subsystem[3],
        'subsystemStatus': subsystem_status,
        'servicesStatus': 'OK' if services is not None else 'ERROR',
        'methods': sorted_methods,
        'services': services if services is not None else []
    }


def process_methods(subsystem, params, doc_path):
    """Function that finds SOAP methods of a subsystem"""
    wsdl_path = '{}{}/'.format(params['minio_path'], doc_path)
    if not params['minio']:
        wsdl_path = '{}/{}'.format(params['path'], doc_path)
    try:
        if not params['minio']:
            make_dirs(wsdl_path)
        hashes = get_wsdl_hashes(wsdl_path, params)
    except OSError as err:
        LOGGER.warning('SOAP: %s: %s', identifier_path(subsystem), err)
        return None

    method_index = {}
    skip_methods = False
    try:
        # Converting iterator to list to properly capture exceptions
        methods = list(xrdinfo.methods(
            addr=params['url'], client=params['client'], producer=subsystem,
            method='listMethods', timeout=params['timeout'], verify=params['verify'],
            cert=params['cert']))
    except xrdinfo.XrdInfoError as err:
        LOGGER.info('SOAP: %s: %s', identifier_path(subsystem), err)
        return None

    for method in sorted(methods):
        method_name = identifier_path(method)
        if method_name in method_index:
            # Method already found in previous WSDL's
            continue

        if skip_methods:
            # Skipping, because previous getWsdl request timed out
            LOGGER.info('SOAP: %s - SKIPPING', method_name)
            method_index[method_name] = method_item(method, 'SKIPPED', '')
            continue

        try:
            wsdl = xrdinfo.wsdl(
                addr=params['url'], client=params['client'], service=method,
                timeout=params['timeout'], verify=params['verify'], cert=params['cert'])
        except xrdinfo.RequestTimeoutError:
            # Skipping all following requests to that subsystem
            skip_methods = True
            LOGGER.info('SOAP: %s - TIMEOUT', method_name)
            method_index[method_name] = method_item(method, 'TIMEOUT', '')
            continue
        except xrdinfo.XrdInfoError as err:
            if str(err) == 'SoapFault: Service is a REST service and does not have a WSDL':
                # This is specific to X-Road 6.21 (partial and
                # deprecated support for REST). We do not want to spam
                # INFO messages about REST services
                LOGGER.debug('SOAP: %s: %s', method_name, err)
            else:
                LOGGER.info('SOAP: %s: %s', method_name, err)
            method_index[method_name] = method_item(method, 'ERROR', '')
            continue

        try:
            wsdl_name, hashes = save_wsdl(wsdl_path, hashes, wsdl, params['wsdl_replaces'], params)
        except OSError as err:
            LOGGER.warning('SOAP: %s: %s', method_name, err)
            method_index[method_name] = method_item(method, 'ERROR', '')
            continue

        txt = 'SOAP: {}'.format(wsdl_name)
        try:
            for wsdl_method in xrdinfo.wsdl_methods(wsdl):
                wsdl_method_name = identifier_path(subsystem + wsdl_method)
                # We can find other methods in a method WSDL
                method_index[wsdl_method_name] = method_item(
                    subsystem + wsdl_method, 'OK', urlparse.quote(
                        '{}/{}'.format(doc_path, wsdl_name)))
                txt = txt + '\n    {}'.format(wsdl_method_name)
        except xrdinfo.XrdInfoError as err:
            txt = txt + '\nWSDL parsing failed: {}'.format(err)
            method_index[method_name] = method_item(method, 'ERROR', '')
        LOGGER.info(txt)

        if method_name not in method_index:
            LOGGER.warning(
                'SOAP: %s - Method was not found in returned WSDL!', method_name)
            method_index[method_name] = method_item(method, 'ERROR', '')

    save_hashes(wsdl_path, hashes, 'wsdl', params)

    return method_index


def process_services(subsystem, params, doc_path):
    """Function that finds REST services of a subsystem"""
    openapi_path = '{}{}/'.format(params['minio_path'], doc_path)
    if not params['minio']:
        openapi_path = '{}/{}'.format(params['path'], doc_path)
    try:
        if not params['minio']:
            make_dirs(openapi_path)
        hashes = get_openapi_hashes(openapi_path, params)
    except OSError as err:
        LOGGER.warning('REST: %s: %s', identifier_path(subsystem), err)
        return None

    results = []
    skip_services = False

    try:
        # Converting iterator to list to properly capture exceptions
        services = list(xrdinfo.methods_rest(
            addr=params['url'], client=params['client'], producer=subsystem,
            method='listMethods', timeout=params['timeout'], verify=params['verify'],
            cert=params['cert']))
    except xrdinfo.XrdInfoError as err:
        LOGGER.info('REST: %s: %s', identifier_path(subsystem), err)
        return None

    for service in sorted(services):
        service_name = identifier_path(service)

        if skip_services:
            # Skipping, because previous getOpenAPI request timed out
            LOGGER.info('REST: %s - SKIPPING', service_name)
            results.append(service_item(service, 'SKIPPED', '', []))
            continue

        try:
            openapi = xrdinfo.openapi(
                addr=params['url'], client=params['client'], service=service,
                timeout=params['timeout'], verify=params['verify'], cert=params['cert'])
        except xrdinfo.RequestTimeoutError:
            # Skipping all following requests to that subsystem
            skip_services = True
            LOGGER.info('REST: %s - TIMEOUT', service_name)
            results.append(service_item(service, 'TIMEOUT', '', []))
            continue
        except xrdinfo.NotOpenapiServiceError:
            results.append(service_item(service, 'OK', '', []))
            continue
        except xrdinfo.XrdInfoError as err:
            LOGGER.info('REST: %s: %s', service_name, err)
            results.append(service_item(service, 'ERROR', '', []))
            continue

        try:
            _, openapi_type = xrdinfo.load_openapi(openapi)
            endpoints = xrdinfo.openapi_endpoints(openapi)
        except xrdinfo.XrdInfoError as err:
            LOGGER.info('REST: %s: %s', service_name, err)
            results.append(service_item(service, 'ERROR', '', []))
            continue

        try:
            openapi_name, hashes = save_openapi(
                openapi_path, hashes, openapi, service[4], openapi_type, params)
        except OSError as err:
            LOGGER.warning('REST: %s: %s', service_name, err)
            results.append(service_item(service, 'ERROR', '', []))
            continue

        results.append(
            service_item(service, 'OK', urlparse.quote(
                '{}/{}'.format(doc_path, openapi_name)), endpoints))

    save_hashes(openapi_path, hashes, 'openapi', params)

    return results


def worker(params):
    """Main function for worker threads"""
    while True:
        # Checking periodically if it is the time to gracefully shut down
        # the worker.
        try:
            subsystem = params['work_queue'].get(True, 0.1)
            LOGGER.info('Start processing %s', identifier_path(subsystem))
        except queue.Empty:
            if params['shutdown'].is_set():
                return
            continue
        subsystem_path = ''
        try:
            subsystem_path = identifier_path(subsystem)
            methods_result = process_methods(subsystem, params, subsystem_path)
            services_result = process_services(subsystem, params, subsystem_path)

            with params['results_lock']:
                params['results'][subsystem_path] = subsystem_item(
                    subsystem, methods_result, services_result)
        # Using broad exception to avoid unexpected exits of workers
        except Exception as err:
            with params['results_lock']:
                params['results'][subsystem_path] = subsystem_item(subsystem, None, None)
            LOGGER.warning('Unexpected exception: %s: %s', type(err).__name__, err)
        finally:
            params['work_queue'].task_done()


def hour_start(src_time):
    """Return the beginning of the hour of the specified datetime"""
    return datetime(src_time.year, src_time.month, src_time.day, src_time.hour)


def day_start(src_time):
    """Return the beginning of the day of the specified datetime"""
    return datetime(src_time.year, src_time.month, src_time.day)


def month_start(src_time):
    """Return the beginning of the month of the specified datetime"""
    return datetime(src_time.year, src_time.month, 1)


def year_start(src_time):
    """Return the beginning of the year of the specified datetime"""
    return datetime(src_time.year, 1, 1)


def add_months(src_time, amount):
    """Adds specified amount of months to datetime value.
    Specifying negative amount will result in subtraction of months.
    """
    return src_time.replace(
        # To find the year correction we convert the month from 1..12 to
        # 0..11 value, add amount of months and find the integer
        # part of division by 12.
        year=src_time.year + (src_time.month - 1 + amount) // 12,
        # To find the new month we convert the month from 1..12 to
        # 0..11 value, add amount of months, find the remainder
        # part after division by 12 and convert the month back
        # to the 1..12 form.
        month=(src_time.month - 1 + amount) % 12 + 1)


def shift_current_hour(offset):
    """Shifts current hour by a specified offset"""
    start = hour_start(datetime.today())
    return start + timedelta(hours=offset)


def shift_current_day(offset):
    """Shifts current hour by a specified offset"""
    start = day_start(datetime.today())
    return start + timedelta(days=offset)


def shift_current_month(offset):
    """Shifts current hour by a specified offset"""
    start = month_start(datetime.today())
    return add_months(start, offset)


def add_filtered(filtered, item_key, report_time, history_item, min_time):
    """Add report to the list of filtered reports"""
    if min_time is None or item_key >= min_time:
        if item_key not in filtered or report_time < filtered[item_key]['time']:
            filtered[item_key] = {'time': report_time, 'item': history_item}


def sort_by_report_time(item):
    """A helper function for sorting, indicates which field to use"""
    return item['reportTime']


def all_results_failed(subsystems):
    """Check if all results have failed status"""
    for subsystem in subsystems.values():
        if subsystem['subsystemStatus'] == 'OK':
            # Found non-failed subsystem
            return False
    # All results failed
    return True


def write_json(file_name, json_data, params):
    """Write data to JSON file"""
    if params['minio']:
        json_binary = json.dumps(json_data, indent=2, ensure_ascii=False).encode()
        params['minio_client'].put_object(
            params['minio_bucket'], file_name,
            BytesIO(json_binary), len(json_binary), content_type='application/json')
    else:
        with open(file_name, 'w') as json_file:
            json.dump(json_data, json_file, indent=2, ensure_ascii=False)


def filtered_history(json_history, params):
    """Get filtered reports history"""
    min_hour = shift_current_hour(-params['filtered_hours'])
    min_day = shift_current_day(-params['filtered_days'])
    min_month = shift_current_month(-params['filtered_months'])
    filtered_items = {}
    for history_item in json_history:
        report_time = datetime.strptime(history_item['reportTime'], '%Y-%m-%d %H:%M:%S')

        item_key = hour_start(report_time)
        add_filtered(filtered_items, item_key, report_time, history_item, min_hour)

        item_key = day_start(report_time)
        add_filtered(filtered_items, item_key, report_time, history_item, min_day)

        item_key = month_start(report_time)
        add_filtered(filtered_items, item_key, report_time, history_item, min_month)

        # Adding all available years
        item_key = year_start(report_time)
        add_filtered(filtered_items, item_key, report_time, history_item, None)

    # Latest report is always added to filtered history
    latest = json_history[0]
    unique_items = {latest['reportTime']: latest}
    for val in filtered_items.values():
        item = val['item']
        unique_items[item['reportTime']] = item

    json_filtered_history = list(unique_items.values())
    json_filtered_history.sort(key=sort_by_report_time, reverse=True)

    return json_filtered_history


def add_report_file(file_name, reports, history=False):
    """Add report to reports list if filename matches"""
    search_res = re.search(
        '^index_(\\d{4})(\\d{2})(\\d{2})(\\d{2})(\\d{2})(\\d{2})\\.json$', file_name)
    if search_res and history:
        reports.append({
            'reportTime': '{}-{}-{} {}:{}:{}'.format(
                search_res.group(1), search_res.group(2),
                search_res.group(3), search_res.group(4),
                search_res.group(5), search_res.group(6)),
            'reportPath': file_name})
    elif search_res:
        reports.append({
            'reportTime': datetime(
                int(search_res.group(1)), int(search_res.group(2)),
                int(search_res.group(3)), int(search_res.group(4)),
                int(search_res.group(5)), int(search_res.group(6))),
            'reportPath': file_name})


def get_catalogue_reports(params, history=False):
    """Get list of reports"""
    reports = []
    if params['minio']:
        for obj in params['minio_client'].list_objects(
                params['minio_bucket'], prefix=params['minio_path'], recursive=False):
            file_name = obj.object_name[len(params['minio_path']):]
            add_report_file(file_name, reports, history=history)
    else:
        for file_name in os.listdir(params['path']):
            add_report_file(file_name, reports, history=history)
    reports.sort(key=sort_by_report_time, reverse=True)
    return reports


def get_reports_to_keep(reports, fresh_time):
    """Get reports that must not be removed during cleanup"""
    # Latest report is never deleted
    unique_paths = {reports[0]['reportTime']: reports[0]['reportPath']}

    filtered_items = {}
    for report in reports:
        if report['reportTime'] >= fresh_time:
            # Keeping all fresh reports
            unique_paths[report['reportTime']] = report['reportPath']
        else:
            # Searching for the first report in a day
            item_key = datetime(
                report['reportTime'].year, report['reportTime'].month, report['reportTime'].day)
            if item_key not in filtered_items \
                    or report['reportTime'] < filtered_items[item_key]['reportTime']:
                filtered_items[item_key] = {
                    'reportTime': report['reportTime'], 'reportPath': report['reportPath']}

    # Adding first report of the day
    for item in filtered_items.values():
        unique_paths[item['reportTime']] = item['reportPath']

    paths_to_keep = list(unique_paths.values())
    paths_to_keep.sort()

    return paths_to_keep


def get_old_reports(params):
    """Get old reports that need to be removed"""
    old_reports = []
    all_reports = get_catalogue_reports(params)
    cur_time = datetime.today()
    fresh_time = datetime(cur_time.year, cur_time.month, cur_time.day) - timedelta(
        days=params['days_to_keep'])
    paths_to_keep = get_reports_to_keep(all_reports, fresh_time)

    for report in all_reports:
        if not report['reportPath'] in paths_to_keep:
            old_reports.append(report['reportPath'])

    old_reports.sort()
    return old_reports


def get_reports_set(params):
    """Get set of reports"""
    reports = set()
    if params['minio']:
        for obj in params['minio_client'].list_objects(
                params['minio_bucket'], prefix=params['minio_path'], recursive=False):
            file_name = obj.object_name[len(params['minio_path']):]
            search_res = re.search(
                '^index_(\\d{4})(\\d{2})(\\d{2})(\\d{2})(\\d{2})(\\d{2})\\.json$',
                file_name)
            if search_res:
                reports.add(file_name)
    else:
        for file_name in os.listdir(params['path']):
            search_res = re.search(
                '^index_(\\d{4})(\\d{2})(\\d{2})(\\d{2})(\\d{2})(\\d{2})\\.json$',
                file_name)
            if search_res:
                reports.add(file_name)
    return reports


def get_docs_in_report(params, report_file):
    if params['minio']:
        obj = params['minio_client'].get_object(
            params['minio_bucket'], '{}{}'.format(params['minio_path'], report_file))
        report_data = json.loads(obj.data.decode('utf-8'))
    else:
        with open('{}/{}'.format(params['path'], report_file), 'r') as json_file:
            report_data = json.load(json_file)

    used_docs = set()
    for system in report_data:
        for method in system['methods']:
            if method['wsdl']:
                if params['minio']:
                    used_docs.add('{}{}'.format(params['minio_path'], method['wsdl']))
                else:
                    used_docs.add('{}/{}'.format(params['path'], method['wsdl']))
        if 'services' in system:
            for service in system['services']:
                if service['openapi']:
                    if params['minio']:
                        used_docs.add('{}{}'.format(params['minio_path'], service['openapi']))
                    else:
                        used_docs.add('{}/{}'.format(params['path'], service['openapi']))
    return used_docs


def add_doc_file(file_name, path, docs):
    search_res = re.search('^\\d+\\.wsdl$', file_name)
    if search_res:
        docs.add(os.path.join(path, file_name))
    search_res = re.search('^.+_(\\d+)\\.(yaml|json)$', file_name)
    if search_res:
        docs.add(os.path.join(path, file_name))


def get_available_docs(params):
    available_docs = set()
    if params['minio']:
        for obj in params['minio_client'].list_objects(
                params['minio_bucket'],
                prefix=os.path.join(params['minio_path'], params['instance']),
                recursive=True):
            add_doc_file(
                os.path.basename(obj.object_name), os.path.dirname(obj.object_name), available_docs)
    else:
        for root, _, files in os.walk(os.path.join(params['path'], params['instance'])):
            for file_name in files:
                add_doc_file(file_name, root, available_docs)
    return available_docs


def get_unused_docs(params):
    reports = get_reports_set(params)
    if not reports:
        LOGGER.warning('Did not find any reports!')
        return set()

    used_docs = set()
    for report_file in reports:
        used_docs = used_docs.union(get_docs_in_report(params, report_file))
    if not used_docs:
        LOGGER.info('Did not find any documents in reports. This is might be an error.')
        return set()

    available_docs = get_available_docs(params)
    return available_docs - used_docs


def start_cleanup(params):
    """Start cleanup of old reports and documents"""
    last_cleanup = None
    if params['minio']:
        try:
            json_file = params['minio_client'].get_object(
                params['minio_bucket'], '{}cleanup_status.json'.format(params['minio_path']))
            cleanup_status = json.loads(json_file.data.decode('utf-8'))
            last_cleanup = datetime.strptime(cleanup_status['lastCleanup'], '%Y-%m-%d %H:%M:%S')
        except (S3Error, ValueError):
            LOGGER.info('Cleanup status not found')
    else:
        try:
            with open('{}/cleanup_status.json'.format(params['path']), 'r') as json_file:
                cleanup_status = json.load(json_file)
                last_cleanup = datetime.strptime(cleanup_status['lastCleanup'], '%Y-%m-%d %H:%M:%S')
        except (IOError, ValueError):
            LOGGER.info('Cleanup status not found')

    if last_cleanup:
        if datetime.today() - timedelta(days=params['cleanup_interval']) < day_start(last_cleanup):
            LOGGER.info('Cleanup interval is not passed yet')
            return

    LOGGER.info('Starting cleanup')

    # Cleanup reports
    old_reports = get_old_reports(params)
    if len(old_reports):
        LOGGER.info('Removing %s old JSON reports:', len(old_reports))
        for report_path in old_reports:
            if params['minio']:
                LOGGER.info('Removing %s%s', params['minio_path'], report_path)
                params['minio_client'].remove_object(
                    params['minio_bucket'], '{}{}'.format(params['minio_path'], report_path))
            else:
                LOGGER.info('Removing %s/%s', params['path'], report_path)
                os.remove('{}/{}'.format(params['path'], report_path))

        # Recreating history.json
        reports = get_catalogue_reports(params, history=True)
        if len(reports):
            LOGGER.info('Writing %s reports to history.json', len(reports))
            if params['minio']:
                write_json('{}history.json'.format(params['minio_path']), reports, params)
            else:
                write_json('{}/history.json'.format(params['path']), reports, params)
    else:
        LOGGER.info('No old JSON reports found in directory: %s', params['path'])

    # Cleanup documents
    unused_docs = get_unused_docs(params)
    changed_dirs = set()
    if unused_docs:
        LOGGER.info('Removing {} unused document(s):'.format(len(unused_docs)))
        for doc_path in unused_docs:
            LOGGER.info('Removing %s', doc_path)
            if params['minio']:
                params['minio_client'].remove_object(params['minio_bucket'], doc_path)
            else:
                os.remove(doc_path)
            changed_dirs.add(os.path.dirname(doc_path))
    else:
        LOGGER.info('No unused documents found')

    # Recreating document hashes cache
    for doc_path in changed_dirs:
        LOGGER.info('Recreating WSDL hashes cache for %s', doc_path)
        hashes = get_wsdl_hashes(doc_path, params)
        save_hashes(doc_path, hashes, 'wsdl', params)
        LOGGER.info('Recreating OpenAPI hashes cache for %s', doc_path)
        hashes = get_openapi_hashes(doc_path, params)
        save_hashes(doc_path, hashes, 'openapi', params)

    # Updating status
    cleanup_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
    json_status = {'lastCleanup': cleanup_time}
    if params['minio']:
        write_json('{}cleanup_status.json'.format(params['minio_path']), json_status, params)
    else:
        write_json('{}/cleanup_status.json'.format(params['path']), json_status, params)


def process_results(params):
    """Process results collected by worker threads"""
    results = params['results']

    if all_results_failed(results):
        # Skipping this version
        LOGGER.error('All subsystems failed, skipping this catalogue version!')
        sys.exit(1)

    json_data = []
    for subsystem_key in sorted(results.keys()):
        json_data.append(results[subsystem_key])

    report_time = time.localtime(time.time())
    formatted_time = time.strftime('%Y-%m-%d %H:%M:%S', report_time)
    suffix = time.strftime('%Y%m%d%H%M%S', report_time)

    if params['minio']:
        write_json('{}index_{}.json'.format(params['minio_path'], suffix), json_data, params)
    else:
        write_json('{}/index_{}.json'.format(params['path'], suffix), json_data, params)

    json_history = []
    if params['minio']:
        try:
            json_history_file = params['minio_client'].get_object(
                params['minio_bucket'], '{}history.json'.format(params['minio_path']))
            json_history = json.loads(json_history_file.data.decode('utf-8'))
        except S3Error:
            LOGGER.info('History file history.json not found')
    else:
        try:
            with open('{}/history.json'.format(params['path']), 'r') as json_file:
                json_history = json.load(json_file)
        except IOError:
            LOGGER.info('History file history.json not found')

    json_history.append({'reportTime': formatted_time, 'reportPath': 'index_{}.json'.format(
        suffix)})
    json_history.sort(key=sort_by_report_time, reverse=True)

    if params['minio']:
        write_json('{}history.json'.format(params['minio_path']), json_history, params)
        write_json('{}filtered_history.json'.format(params['minio_path']), filtered_history(
            json_history, params), params)
    else:
        write_json('{}/history.json'.format(params['path']), json_history, params)
        write_json('{}/filtered_history.json'.format(params['path']), filtered_history(
            json_history, params), params)

    # Replace index.json with latest report
    if params['minio']:
        params['minio_client'].copy_object(
            params['minio_bucket'], '{}index.json'.format(params['minio_path']),
            '/{}/{}index_{}.json'.format(params['minio_bucket'], params['minio_path'], suffix))
    else:
        shutil.copy('{}/index_{}.json'.format(
            params['path'], suffix), '{}/index.json'.format(params['path']))

    # Updating status
    json_status = {'lastReport': formatted_time}
    if params['minio']:
        write_json('{}status.json'.format(params['minio_path']), json_status, params)
    else:
        write_json('{}/status.json'.format(params['path']), json_status, params)

    start_cleanup(params)


def main():
    """Main function"""
    logging.config.dictConfig(DEFAULT_LOGGER)

    parser = argparse.ArgumentParser(
        description='Collect WSDL service descriptions from X-Road members.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='By default peer TLS certificate is not validated.'
    )
    parser.add_argument(
        'config', metavar='CONFIG_FILE',
        help='Configuration file')
    args = parser.parse_args()

    config = load_config(args.config)
    if config is None:
        sys.exit(1)

    configure_logging(config)

    params = set_params(config)
    if params is None:
        sys.exit(1)

    if not params['minio']:
        if not make_dirs(params['path']):
            sys.exit(1)

    if params['minio']:
        prepare_minio_client(params)

    try:
        shared_params = xrdinfo.shared_params_ss(
            addr=params['url'], instance=params['instance'], timeout=params['timeout'],
            verify=params['verify'], cert=params['cert'])
    except xrdinfo.XrdInfoError as err:
        LOGGER.error('Cannot download Global Configuration: %s', err)
        sys.exit(1)

    # Create and start new threads
    threads = []
    for _ in range(params['thread_cnt']):
        thread = Thread(target=worker, args=(params,))
        thread.daemon = True
        thread.start()
        threads.append(thread)

    # Populate the queue
    try:
        for subsystem in xrdinfo.registered_subsystems(shared_params):
            if subsystem[2] in params['excluded_member_codes']:
                LOGGER.info('Skipping excluded member %s', identifier_path(subsystem))
                continue
            if [subsystem[2], subsystem[3]] in params['excluded_subsystem_codes']:
                LOGGER.info('Skipping excluded subsystem %s', identifier_path(subsystem))
                continue
            params['work_queue'].put(subsystem)
    except xrdinfo.XrdInfoError as err:
        LOGGER.error('Cannot process Global Configuration: %s', err)
        sys.exit(1)

    # Block until all tasks in queue are done
    params['work_queue'].join()

    # Set shutdown event and wait until all daemon processes finish
    params['shutdown'].set()
    for thread in threads:
        thread.join()

    process_results(params)


if __name__ == '__main__':
    main()
