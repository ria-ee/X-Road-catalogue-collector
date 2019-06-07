#!/usr/bin/env python3

import queue
from threading import Thread, Event, Lock, current_thread
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import xrdinfo

# Default timeout for HTTP requests
DEFAULT_TIMEOUT = 5.0

# Do not use threading by default
DEFAULT_THREAD_COUNT = 1

METHODS_HTML_TEMPL = u"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>All subsystems with methods and WSDL descriptions for instance "{instance}"</title>
  <link rel="stylesheet"
    href="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0/css/bootstrap.min.css">
  <script src="https://ajax.googleapis.com/ajax/libs/jquery/3.3.1/jquery.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.12.9/umd/popper.min.js"></script>
  <script src="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0/js/bootstrap.min.js"></script>
</head>
<body>
<div class="container">
<h1>All subsystems with methods and WSDL descriptions for instance "{instance}"</h1>
<p>Report time: {report_time}</p>
<p><a href="history.html">History</a></p>
<p>Latest data in <a href="index.json">JSON</a> form.</p>
<p>This report in <a href="index_{suffix}.json">JSON</a> form.</p>
<p>NB! Expanding all subsystems is slow operation.</p>
<button type="button" class="btn" onClick="$('#accordion .collapse').collapse('show');">
Expand all subsystems
</button>
<button type="button" class="btn" onClick="$('#accordion .collapse').collapse('hide');">
Collapse all subsystems
</button>
<div id="accordion">
{body}</div>
</div>
</body>
</html>
"""

HISTORY_HTML_TEMPL = u"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>History</title>
<link rel="stylesheet"
  href="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0/css/bootstrap.min.css">
</head>
<body>
<div class="container">
<h1>History</h1>
{body}</div>
</body>
</html>
"""

HISTORY_HEADER = u'<h1>History</h1>\n'

WSDL_REPLACES = [
    # [Pattern, Replacement]
    # "Genereerimise aeg: 22.03.2019 08:00:30"
    [
        'Genereerimise aeg: \\d{2}\\.\\d{2}\\.\\d{4} \\d{2}:\\d{2}:\\d{2}',
        'Genereerimise aeg: DELETED'
    ]
]


def safe_print(content):
    """Thread safe and unicode safe debug printer."""
    content = u'{}\n'.format(content)
    sys.stdout.write(content)


def makedirs(path):
    try:
        os.makedirs(path)
    except OSError:
        pass
    if not os.path.exists(path):
        safe_print(u'Cannot create directory "{}"'.format(path))
        exit(0)


def hash_wsdls(path):
    hashes = {}
    for file_name in os.listdir(path):
        s = re.search('^(\\d+)\\.wsdl$', file_name)
        if s:
            # Reading as bytes to avoid line ending conversion
            with open(u'{}/{}'.format(path, file_name), 'rb') as fh:
                wsdl = fh.read()
            hashes[file_name] = hashlib.md5(wsdl).hexdigest()
    return hashes


def save_wsdl(path, hashes, wsdl):
    # Replacing dynamically generated comments in WSDL to avoid new WSDL
    # creation because of comments.
    for wsdl_replace in WSDL_REPLACES:
        wsdl = re.sub(wsdl_replace[0], wsdl_replace[1], wsdl)
    wsdl_hash = hashlib.md5(wsdl.encode('utf-8')).hexdigest()
    max_wsdl = -1
    for file_name in hashes.keys():
        if wsdl_hash == hashes[file_name]:
            # Matching WSDL found
            return file_name, hashes
        s = re.search('^(\\d+)\\.wsdl$', file_name)
        if s:
            if int(s.group(1)) > max_wsdl:
                max_wsdl = int(s.group(1))
    # Creating new file
    new_file = u'{}.wsdl'.format(int(max_wsdl) + 1)
    # Writing as bytes to avoid line ending conversion
    with open(u'{}/{}'.format(path, new_file), 'wb') as f:
        f.write(wsdl.encode('utf-8'))
    hashes[new_file] = wsdl_hash
    return new_file, hashes


def worker(params):
    while True:
        # Checking periodically if it is the time to gracefully shutdown
        # the worker.
        try:
            subsystem = params['work_queue'].get(True, 0.1)
        except queue.Empty:
            if params['shutdown'].is_set():
                return
            else:
                continue
        wsdl_rel_path = ''
        try:
            wsdl_rel_path = xrdinfo.stringify(subsystem)
            wsdl_path = u'{}/{}'.format(params['path'], wsdl_rel_path)
            makedirs(wsdl_path)
            hashes = hash_wsdls(wsdl_path)

            method_index = {}
            skip_methods = False
            for method in sorted(xrdinfo.methods(
                    addr=params['url'], client=params['client'], producer=subsystem,
                    method='listMethods', timeout=params['timeout'], verify=params['verify'],
                    cert=params['cert'])):
                if xrdinfo.stringify(method) in method_index:
                    # Method already found in previous WSDL's
                    continue

                if skip_methods:
                    # Skipping, because previous getWsdl request timed
                    # out
                    if params['verbose']:
                        safe_print(u'{}: {} - SKIPPING\n'.format(
                            current_thread().getName(), xrdinfo.stringify(method)))
                    method_index[xrdinfo.stringify(method)] = 'SKIPPED'
                    continue

                try:
                    wsdl = xrdinfo.wsdl(
                        addr=params['url'], client=params['client'], service=method,
                        timeout=params['timeout'], verify=params['verify'], cert=params['cert'])
                except xrdinfo.RequestTimeoutError:
                    # Skipping all following requests to that subsystem
                    skip_methods = True
                    if params['verbose']:
                        safe_print(u'{}: {} - TIMEOUT\n'.format(
                            current_thread().getName(), xrdinfo.stringify(method)))
                    method_index[xrdinfo.stringify(method)] = 'TIMEOUT'
                    continue
                except xrdinfo.XrdInfoError as e:
                    if params['verbose']:
                        safe_print(u'{}: {} - ERROR:\n{}\n'.format(
                            current_thread().getName(), xrdinfo.stringify(method), e))
                    method_index[xrdinfo.stringify(method)] = ''
                    continue

                wsdl_name, hashes = save_wsdl(wsdl_path, hashes, wsdl)
                txt = u'{}: {}\n'.format(current_thread().getName(), wsdl_name)
                try:
                    for wsdl_method in xrdinfo.wsdl_methods(wsdl):
                        method_full_name = xrdinfo.stringify(subsystem + wsdl_method)
                        method_index[method_full_name] = u'{}/{}'.format(wsdl_rel_path, wsdl_name)
                        txt = txt + u'    {}\n'.format(method_full_name)
                except xrdinfo.XrdInfoError as e:
                    txt = txt + u'WSDL parsing failed: {}\n'.format(e)
                    method_index[xrdinfo.stringify(method)] = ''
                if params['verbose']:
                    safe_print(txt)

                if xrdinfo.stringify(method) not in method_index:
                    # if params['verbose']:
                    safe_print(u'{}: {} - Method was not found in returned WSDL!'.format(
                        current_thread().getName(), xrdinfo.stringify(method)))
                    method_index[xrdinfo.stringify(method)] = ''

            with params['results_lock']:
                params['results'][wsdl_rel_path] = {
                    'methods': method_index,
                    'ok': True}
        except xrdinfo.XrdInfoError as e:
            with params['results_lock']:
                params['results'][wsdl_rel_path] = {
                    'methods': {},
                    'ok': False}
            if params['verbose']:
                safe_print(u'{}: {} - ERROR:\n{}\n'.format(
                    current_thread().getName(), xrdinfo.stringify(subsystem), e))
        except Exception as e:
            with params['results_lock']:
                params['results'][wsdl_rel_path] = {
                    'methods': {},
                    'ok': False}
            safe_print(u'{}: {}: {}\n'.format(current_thread().getName(), type(e).__name__, e))
        finally:
            params['work_queue'].task_done()


def sort_by_time(item):
    return item['reportTime']


def main():
    parser = argparse.ArgumentParser(
        description='X-Road getWsdl request to all members.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='By default peer TLS certificate is not validated.'
    )
    parser.add_argument(
        'url', metavar='SERVER_URL',
        help='URL of local Security Server accepting X-Road requests.')
    parser.add_argument(
        'client', metavar='CLIENT',
        help='slash separated Client identifier (e.g. '
             '"INSTANCE/MEMBER_CLASS/MEMBER_CODE/SUBSYSTEM_CODE" '
             'or "INSTANCE/MEMBER_CLASS/MEMBER_CODE").')
    parser.add_argument('path', metavar='PATH', help='path for storing results.')
    parser.add_argument('-v', help='verbose output', action='store_true')
    parser.add_argument('-t', metavar='TIMEOUT', help='timeout for HTTP query', type=float)
    parser.add_argument(
        '--threads', metavar='THREADS', help='amount of threads to use', type=int, default=0)
    parser.add_argument(
        '--verify', metavar='CERT_PATH',
        help='validate peer TLS certificate using CA certificate file.')
    parser.add_argument(
        '--cert', metavar='CERT_PATH', help='use TLS certificate for HTTPS requests.')
    parser.add_argument('--key', metavar='KEY_PATH', help='private key for TLS certificate.')
    parser.add_argument(
        '--instance', metavar='INSTANCE',
        help='use this instance instead of local X-Road instance.')
    parser.add_argument('--no-html', action='store_true', help='disable HTML generation.')
    args = parser.parse_args()

    params = {
        'verbose': False,
        'path': args.path,
        'url': args.url,
        'client': args.client,
        'instance': None,
        'timeout': DEFAULT_TIMEOUT,
        'verify': False,
        'cert': None,
        'thread_cnt': DEFAULT_THREAD_COUNT,
        'work_queue': queue.Queue(),
        'results': {},
        'results_lock': Lock(),
        'shutdown': Event(),
        'html': True
    }

    if args.v:
        params['verbose'] = True

    makedirs(params['path'])

    params['client'] = params['client'].split('/')
    if not (len(params['client']) in (3, 4)):
        safe_print(u'Client name is incorrect: "{}"'.format(args.client))
        exit(1)

    if args.instance:
        params['instance'] = args.instance

    if args.t:
        params['timeout'] = args.t

    if args.verify:
        params['verify'] = args.verify

    if args.cert and args.key:
        params['cert'] = (args.cert, args.key)

    if args.threads and args.threads > 0:
        params['thread_cnt'] = args.threads

    if args.no_html:
        params['html'] = False

    shared_params = None
    try:
        shared_params = xrdinfo.shared_params_ss(
            addr=args.url, instance=params['instance'], timeout=params['timeout'],
            verify=params['verify'], cert=params['cert'])
    except xrdinfo.XrdInfoError as e:
        safe_print(u'Cannot download Global Configuration: {}'.format(e))
        exit(1)

    # Create and start new threads
    threads = []
    for _ in range(params['thread_cnt']):
        t = Thread(target=worker, args=(params,))
        t.daemon = True
        t.start()
        threads.append(t)

    # Populate the queue
    try:
        for subsystem in xrdinfo.registered_subsystems(shared_params):
            params['work_queue'].put(subsystem)
    except xrdinfo.XrdInfoError as e:
        safe_print(u'Cannot process Global Configuration: {}'.format(e))
        exit(1)

    # Block until all tasks in queue are done
    params['work_queue'].join()

    # Set shutdown event and wait until all daemon processes finish
    params['shutdown'].set()
    for t in threads:
        t.join()

    results = params['results']

    body = ''
    card_nr = 0
    json_data = []
    for subsystem_key in sorted(results.keys()):
        card_nr += 1
        subsystem_result = results[subsystem_key]
        methods = subsystem_result['methods']
        if subsystem_result['ok'] and len(methods) > 0:
            subsystem_status = 'ok'
            subsystem_badge = u''
        elif subsystem_result['ok']:
            subsystem_status = 'empty'
            subsystem_badge = u' <span class="badge badge-secondary">Empty</span>'
        else:
            subsystem_status = 'error'
            subsystem_badge = u' <span class="badge badge-danger">Error</span>'
        body += u'<div class="card">\n' \
                u'<div class="card-header">\n' \
                u'<a class="card-link" data-toggle="collapse" href="#collapse{}">\n' \
                u'{}{}\n' \
                u'</a>\n' \
                u'</div>\n'.format(card_nr, subsystem_key, subsystem_badge)
        body += u'<div id="collapse{}" class="collapse">\n' \
                u'<div class="card-body">\n'.format(card_nr)
        if subsystem_status == 'empty':
            body += u'<p>No services found</p>'
        elif subsystem_status == 'error':
            body += u'<p>Error while getting list of services</p>'
        subsystem = subsystem_key.split('/')
        json_subsystem = {
            'xRoadInstance': subsystem[0],
            'memberClass': subsystem[1],
            'memberCode': subsystem[2],
            'subsystemCode': subsystem[3],
            'subsystemStatus': 'ERROR' if subsystem_status == 'error' else 'OK',
            'methods': []
        }
        for method_key in sorted(methods.keys()):
            method = method_key.split('/')
            json_method = {
                'serviceCode': method[4],
                'serviceVersion': method[5],
            }
            if methods[method_key] == 'SKIPPED':
                body += u'<p>{} <span class="badge badge-warning">WSDL skipped due to ' \
                        u'previous Timeout</span></p>\n'.format(method_key)
                json_method['methodStatus'] = 'SKIPPED'
                json_method['wsdl'] = ''
            elif methods[method_key] == 'TIMEOUT':
                body += u'<p>{} <span class="badge badge-danger">WSDL query timed out' \
                        u'</span></p>\n'.format(method_key)
                json_method['methodStatus'] = 'TIMEOUT'
                json_method['wsdl'] = ''
            elif methods[method_key]:
                body += u'<p>{}: <a href="{}" class="badge badge-success">WSDL</a></p>\n'.format(
                    method_key, methods[method_key])
                json_method['methodStatus'] = 'OK'
                json_method['wsdl'] = methods[method_key]
            else:
                body += u'<p>{} <span class="badge badge-danger">Error while downloading ' \
                        u'or parsing of WSDL</span></p>\n'.format(method_key)
                json_method['methodStatus'] = 'ERROR'
                json_method['wsdl'] = ''

            json_subsystem['methods'].append(json_method)
        # Closing: card-body, collapseX, card
        body += u'</div>\n' \
                u'</div>\n' \
                u'</div>\n'
        json_data.append(json_subsystem)

    report_time = time.localtime(time.time())
    formatted_time = time.strftime('%Y-%m-%d %H:%M:%S', report_time)
    suffix = time.strftime('%Y%m%d%H%M%S', report_time)

    s = re.search('<instanceIdentifier>(.+?)</instanceIdentifier>', shared_params)
    if s and s.group(1):
        instance = s.group(1)
    else:
        instance = u'???'

    # JSON output
    with open(u'{}/index_{}.json'.format(args.path, suffix), 'w') as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)

    json_history = []
    try:
        with open(u'{}/history.json'.format(args.path), 'r') as f:
            json_history = json.load(f)
    except IOError:
        # Cannot open history.html
        pass

    json_history.append({'reportTime': formatted_time, 'reportPath': u'index_{}.json'.format(
        suffix)})
    json_history.sort(key=sort_by_time, reverse=True)

    with open(u'{}/history.json'.format(args.path), 'w') as f:
        json.dump(json_history, f, indent=2, ensure_ascii=False)

    # Replace index.json with latest report
    shutil.copy(u'{}/index_{}.json'.format(args.path, suffix), u'{}/index.json'.format(args.path))

    # HTML output
    if params['html']:
        html = METHODS_HTML_TEMPL.format(
            instance=instance, report_time=formatted_time, suffix=suffix, body=body)
        with open(u'{}/index_{}.html'.format(args.path, suffix), 'w') as f:
            f.write(html)

        history_item = u'<p><a href="{}">{}</a></p>\n'.format(
            u'index_{}.html'.format(suffix), formatted_time)
        try:
            html = u''
            with open(u'{}/history.html'.format(args.path), 'r') as f:
                for line in f:
                    if line == HISTORY_HEADER:
                        line = line + history_item
                    html = html + line
        except IOError:
            # Cannot open history.html
            html = HISTORY_HTML_TEMPL.format(body=history_item)

        with open(u'{}/history.html'.format(args.path), 'w') as f:
            f.write(html)

        # Replace index.html with latest report
        shutil.copy(u'{}/index_{}.html'.format(args.path, suffix), u'{}/index.html'.format(
            args.path))


if __name__ == '__main__':
    main()
