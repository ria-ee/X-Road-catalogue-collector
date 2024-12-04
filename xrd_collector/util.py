"""Common classes and functions"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import os
import re
from typing import Any, Sequence

DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


@dataclass
class Method:
    """Dataclass for WSDL method objects"""
    service_code: str
    service_version: str
    status: str
    wsdl: str
    # "hash" can be used by custom plugins
    hash: str


@dataclass
class Service:
    """Dataclass for OpenAPI service objects"""
    service_code: str
    status: str
    openapi: str
    # "hash" can be used by custom plugins
    hash: str
    endpoints: Sequence[dict[str, str]]


@dataclass
class Subsystem:
    """Dataclass for X-Road subsystem objects"""
    path: str
    x_road_instance: str
    member_class: str
    member_code: str
    subsystem_code: str
    methods_status: str
    services_status: str
    methods: Sequence[Method]
    services: Sequence[Service]


def export_method(method: Method, path: str = '') -> dict[str, Any]:
    """Export Method object to a form serializable by "json.dump" method"""
    return {
        'serviceCode': method.service_code,
        'serviceVersion': method.service_version,
        'methodStatus': method.status,
        'wsdl': os.path.join(path, method.wsdl) if method.wsdl else ''
    }


def export_service(service: Service, path: str = '') -> dict[str, Any]:
    """Export Service object to a form serializable by "json.dump" method"""
    return {
        'serviceCode': service.service_code,
        'status': service.status,
        'openapi': os.path.join(path, service.openapi) if service.openapi else '',
        'endpoints': service.endpoints
    }


def export_subsystem(subsystem: Subsystem) -> dict[str, Any]:
    """Export Subsystem object to a form serializable by "json.dump" method"""
    return {
        'xRoadInstance': subsystem.x_road_instance,
        'memberClass': subsystem.member_class,
        'memberCode': subsystem.member_code,
        'subsystemCode': subsystem.subsystem_code,
        # Methods status indicated subsystem status before X-Road REST support was created
        # Exported name was not changed for backwards compatibility
        # Currently Catalogue Web UI does not support 'TIMEOUT' status
        'subsystemStatus': 'OK' if subsystem.methods_status == 'OK' else 'ERROR',
        'servicesStatus': 'OK' if subsystem.services_status == 'OK' else 'ERROR',
        'methods': [export_method(method, subsystem.path) for method in subsystem.methods],
        'services': [export_service(service, subsystem.path) for service in subsystem.services]
    }


def sort_by_report_time(item: dict[str, Any]) -> Any:
    """A helper function for sorting, indicates which field to use"""
    return item['reportTime']


def hour_start(src_time: datetime) -> datetime:
    """Return the beginning of the hour of the specified datetime"""
    return datetime(src_time.year, src_time.month, src_time.day, src_time.hour)


def day_start(src_time: datetime) -> datetime:
    """Return the beginning of the day of the specified datetime"""
    return datetime(src_time.year, src_time.month, src_time.day)


def month_start(src_time: datetime) -> datetime:
    """Return the beginning of the month of the specified datetime"""
    return datetime(src_time.year, src_time.month, 1)


def year_start(src_time: datetime) -> datetime:
    """Return the beginning of the year of the specified datetime"""
    return datetime(src_time.year, 1, 1)


def add_months(src_time: datetime, amount: int) -> datetime:
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


def shift_current_hour(offset: int) -> datetime:
    """Shifts current hour by a specified offset"""
    start = hour_start(datetime.today())
    return start + timedelta(hours=offset)


def shift_current_day(offset: int) -> datetime:
    """Shifts current hour by a specified offset"""
    start = day_start(datetime.today())
    return start + timedelta(days=offset)


def shift_current_month(offset: int) -> datetime:
    """Shifts current hour by a specified offset"""
    start = month_start(datetime.today())
    return add_months(start, offset)


def add_filtered(
        filtered: dict[Any, Any], item_key: datetime, report_time: datetime,
        history_item: dict[str, str], min_time: datetime | None) -> None:
    """Add report to the list of filtered reports"""
    if min_time is None or item_key >= min_time:
        if item_key not in filtered or report_time < filtered[item_key]['time']:
            filtered[item_key] = {'time': report_time, 'item': history_item}


def filtered_history(
        json_history: list[dict[str, str]], filtered_hours: int, filtered_days: int,
        filtered_months: int) -> list[dict[str, str]]:
    """Get filtered reports history"""
    filtered_items: dict[Any, Any] = {}
    for history_item in json_history:
        report_time = datetime.strptime(history_item['reportTime'], '%Y-%m-%d %H:%M:%S')

        add_filtered(
            filtered_items, hour_start(report_time), report_time, history_item,
            shift_current_hour(-filtered_hours))

        add_filtered(
            filtered_items, day_start(report_time), report_time, history_item,
            shift_current_day(-filtered_days))

        add_filtered(
            filtered_items, month_start(report_time), report_time, history_item,
            shift_current_month(-filtered_months))

        # Adding all available years
        add_filtered(filtered_items, year_start(report_time), report_time, history_item, None)

    # Latest report is always added to filtered history
    latest = json_history[0]
    unique_items = {latest['reportTime']: latest}
    for val in filtered_items.values():
        item = val['item']
        unique_items[item['reportTime']] = item

    json_filtered_history = list(unique_items.values())
    json_filtered_history.sort(key=sort_by_report_time, reverse=True)

    return json_filtered_history


def add_report_file(file_name: str, reports: list[dict[str, Any]], history: bool = False) -> None:
    """Add report to reports list if filename matches"""
    search_res = re.search(
        '^index_(\\d{4})(\\d{2})(\\d{2})(\\d{2})(\\d{2})(\\d{2})\\.json$', file_name)
    if search_res and history:
        reports.append({
            'reportTime': f'{search_res.group(1)}-{search_res.group(2)}-{search_res.group(3)}'
                          f' {search_res.group(4)}:{search_res.group(5)}:{search_res.group(6)}',
            'reportPath': file_name})
    elif search_res:
        reports.append({
            'reportTime': datetime(
                int(search_res.group(1)), int(search_res.group(2)),
                int(search_res.group(3)), int(search_res.group(4)),
                int(search_res.group(5)), int(search_res.group(6))),
            'reportPath': file_name})


def get_reports_to_keep(reports: list[dict[str, Any]], fresh_time: datetime) -> list[str]:
    """Get reports that must not be removed during cleanup"""
    # Latest report is never deleted
    unique_paths: dict[datetime, str] = {reports[0]['reportTime']: reports[0]['reportPath']}

    filtered_items: dict[datetime, dict[str, Any]] = {}
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


def add_doc_file(file_name: str, path: str, docs: set[str]) -> None:
    """Add document to document list if file name matches document name template"""
    search_res = re.search('^\\d+\\.wsdl$', file_name)
    if search_res:
        docs.add(os.path.join(path, file_name))
    search_res = re.search('^.+_(\\d+)\\.(yaml|json)$', file_name)
    if search_res:
        docs.add(os.path.join(path, file_name))
