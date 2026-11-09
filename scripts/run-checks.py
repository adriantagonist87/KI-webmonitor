#!/usr/bin/env python3
"""Run the pinned application acceptance suites in an isolated Linux test container.

Example (runner is included in the image; use an isolated QA data volume):
  KIWM_TEST_CONTAINER=1 python /app/run-checks.py --output /data/qa/run-001
  KIWM_TEST_CONTAINER=1 BROWSER_CDP_URL=http://QA_BROWSER_IP:9223 \
    python /app/run-checks.py --browser --output /data/qa/run-002

No model calls are allowed. Browser integration is explicitly opt-in. Host
bootstrap/Git/archive tests are intentionally separate from these app checks.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from urllib.parse import urlsplit

# Baseline: work/retry-task-state.json regression.suite_counts, 387 tests.
SUITES = {
    'admin_auth_checks': 25,
    'lan_access_checks': 11,
    'public_access_checks': 9,
    'dashboard_control_checks': 20,
    'communication_checks': 37,
    'release_checks': 17,
    'site_safety_checks': 20,
    'site_safety_browser_checks': 7,
    'retention_checks': 23, 'document_inventory_checks': 24,
    'retention_integration_checks': 9, 'history_cleanup_checks': 8, 'document_files_checks': 22,
    'ui_checks': 17, 'transfer_checks': 24, 'agent_driver_checks': 14, 'navigation_macro_checks': 4,
    'configured_sources_checks': 9, 'document_metadata_checks': 25,
    # Current runtime checks replace the removed OCR/transcription suites.
    'configuration_checks': 15, 'configuration_ui_checks': 7, 'document_hash_checks': 18, 'document_types_checks': 8, 'document_checkpoint_checks': 5, 'selftest': 22, 'boundary_checks': 3, 'enhancement_checks': 24,
    'portal_monitor_checks': 1, 'retry_jobs_checks': 30,
    'retry_transfer_checks': 13, 'section_capture_checks': 16,
    'visible_content_checks': 8, 'disclosure_actions_checks': 27,
    'generic_access_checks': 9, 'browser_actions_checks': 11,
    'retry_ui_checks': 10, 'usability_checks': 17, 'gateway_endpoint_checks': 6,
    'management_checks': 20, 'ui_interaction_checks': 11,
    'document_files_ui_checks': 19,
    'file_icons_motion_checks': 8,
    'change_review_checks': 14, 'relevance_review_checks': 18,
    'comparison_validity_checks': 8, 'carousel_content_checks': 15,
    'report_source_checks': 11,
    'weekly_report_checks': 23,
    'distribution_checks': 36,
    'document_names_checks': 12,
    'github_releases_checks': 4,
    'obsidian_changelog_checks': 3,
    'dke_standards_checks': 6,
}

# Four older integration classes do not have their own skip guard and otherwise
# fall back to the old browser IP/alias. Never import-and-run them blindly.
BROWSER_CLASSES = {
    'file_icons_motion_checks': ('FileIconsMotionBrowser',),
    'dashboard_control_checks': ('DashboardBrowser',),
    'site_safety_browser_checks': ('SafetyBrowser',),
    'configured_sources_checks': ('ConfiguredSourcesMonitor',),
    'portal_monitor_checks': ('PortalMonitorTests',),
    'section_capture_checks': ('SectionMonitor',),
    'visible_content_checks': ('VisibleContentTests',),
    'disclosure_actions_checks': ('DisclosureActionsTests',),
    'generic_access_checks': ('GenericBrowserTests',),
    'browser_actions_checks': ('BrowserActions',),
    'retry_ui_checks': ('RetryBrowser',),
    'usability_checks': ('UsabilityBrowser',),
    'ui_interaction_checks': ('FrontendInteractions',),
    'carousel_content_checks': ('CarouselBrowserChecks',),
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def container_guard(browser):
    if (not sys.platform.startswith('linux') or not Path('/.dockerenv').exists()
            or not Path('/app').is_dir() or os.environ.get('KIWM_TEST_CONTAINER') != '1'):
        raise ValueError('Nur im isolierten Linux-Testcontainer mit KIWM_TEST_CONTAINER=1 und /app ausfuehren.')
    if not browser:
        return None
    value = os.environ.get('BROWSER_CDP_URL', '')
    try:
        url = urlsplit(value)
        valid = (url.scheme in ('http', 'https') and url.hostname and url.port
                 and not url.username and not url.password and url.path in ('', '/')
                 and not url.query and not url.fragment
                 and url.hostname not in ('browser', '172.29.133.20', 'localhost', '127.0.0.1', '::1'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('--browser verlangt BROWSER_CDP_URL eines isolierten QA-Browsers; bekannte Produktions-/Loopbackziele sind gesperrt.')
    return url


def output_directory(value, *, create=False):
    path = Path(value)
    base = Path('/data/qa')
    if not path.is_absolute() or path == base:
        raise ValueError('--output muss ein eigenes absolutes Verzeichnis unter /data/qa sein.')
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError('Symlinks sind fuer QA-Ausgaben nicht erlaubt.')
    path = path.resolve()
    if not path.is_relative_to(base.resolve()) or path == base.resolve():
        raise ValueError('--output muss unter /data/qa liegen.')
    if create:
        if path.exists():
            raise ValueError('Ausgabeverzeichnis existiert schon; fuer einen neuen Lauf einen neuen Namen waehlen.')
        path.mkdir(parents=True, mode=0o700)
    return path


@contextmanager
def network_guard(browser_url):
    """Python TCP stays offline or on the explicitly selected QA dependencies.

    Browser page networking is isolated by the QA Docker network and the tests'
    incognito route fixtures. This is not a substitute for that Docker isolation.
    The local inference gateway and external hosts are never whitelisted here.
    """
    allowed = set()
    if browser_url:
        allowed.update({('proxy', 3128), ('fixture', 8000), (browser_url.hostname, browser_url.port)})
    original_lookup, original_connect = socket.getaddrinfo, socket.socket.connect
    permitted_ips = set()

    def lookup(host, port, *args, **kwargs):
        name = host.decode('ascii') if isinstance(host, bytes) else host
        try:
            pair = (name, int(port))
        except (ValueError, TypeError):
            pair = None
        if pair not in allowed:
            raise RuntimeError('QA network guard: nicht freigegebenes Netzwerkziel')
        results = original_lookup(host, port, *args, **kwargs)
        permitted_ips.update((item[4][0], int(item[4][1])) for item in results)
        return results

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            pair = (address[0], int(address[1]))
            if pair not in allowed and pair not in permitted_ips:
                raise RuntimeError('QA network guard: nicht freigegebene TCP-Verbindung')
        return original_connect(sock, address)

    socket.getaddrinfo = lookup
    socket.socket.connect = connect
    try:
        yield
    finally:
        socket.getaddrinfo, socket.socket.connect = original_lookup, original_connect


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.skipped_cases = []

    def addSkip(self, test, reason):
        self.skipped_cases.append({'test': test.id(), 'reason': reason})
        super().addSkip(test, reason)


def worker(name, target, *, browser=False):
    started = time.monotonic()
    value = {'suite': name, 'tests': 0, 'skipped': 0, 'failures': 0, 'errors': 0,
             'unexpected_successes': 0, 'skipped_cases': [], 'failed_cases': [], 'status': 'failed'}
    try:
        url = container_guard(browser)
        if name not in SUITES:
            raise ValueError('Unbekannte Suite')
        if not browser:
            os.environ.pop('BROWSER_CDP_URL', None)
        sys.path.insert(0, '/app')
        Path(os.environ['DATA_DIR']).mkdir(parents=True, exist_ok=True)
        module_path = Path('/app') / (name + '.py')
        with network_guard(url):
            spec = importlib.util.spec_from_file_location(name, module_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            if not browser:
                for class_name in BROWSER_CLASSES.get(name, ()):
                    case = getattr(module, class_name)
                    case.__unittest_skip__ = True
                    case.__unittest_skip_why__ = 'Browserintegration ausgelassen: --browser und isoliertes BROWSER_CDP_URL erforderlich'
            tests = unittest.defaultTestLoader.loadTestsFromModule(module)
            result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(tests)
        value.update(tests=result.testsRun, skipped=len(result.skipped), failures=len(result.failures),
                     errors=len(result.errors), unexpected_successes=len(result.unexpectedSuccesses),
                     skipped_cases=result.skipped_cases,
                     failed_cases=[test.id() for test, _ in result.failures + result.errors]
                         + [test.id() for test in result.unexpectedSuccesses])
        enough = result.testsRun >= SUITES[name] and result.testsRun > 0
        value['status'] = 'passed' if result.wasSuccessful() and enough else 'failed'
        if not enough:
            value['runner_error'] = 'Weniger Tests als in der dokumentierten Baseline entdeckt.'
    except BaseException as error:
        # Interrupts also leave a machine-readable failure instead of a false pass.
        value['errors'] += 1
        value['runner_error'] = type(error).__name__ + ': ' + str(error)
        traceback.print_exc()
    value.update(duration_seconds=round(time.monotonic() - started, 3), expected_minimum=SUITES.get(name, 0))
    Path(target).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return 0 if value['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, help='Neues Verzeichnis unter /data/qa fuer summary.json und Einzellogs')
    parser.add_argument('--browser', action='store_true', help='Browserintegration ausdruecklich gegen isolierten QA-CDP ausfuehren')
    parser.add_argument('--suite', choices=tuple(SUITES), action='append', help=f'Nur die genannte Suite, mehrfach moeglich; Standard alle {len(SUITES)}')
    parser.add_argument('--timeout', type=int, default=600, help='Zeitlimit je Suite in Sekunden, Standard 600')
    parser.add_argument('--worker', choices=tuple(SUITES), help=argparse.SUPPRESS)
    parser.add_argument('--worker-result', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if args.worker_result is None:
            parser.error('Interner Worker braucht einen Ergebnispfad')
        output_directory(args.worker_result.parent)
        return worker(args.worker, args.worker_result, browser=args.browser)
    try:
        container_guard(args.browser)
        if args.output is None:
            raise ValueError('--output ist erforderlich; bestehende Ergebnisse werden nicht ueberschrieben.')
        if not 1 <= args.timeout <= 3600:
            raise ValueError('--timeout muss zwischen 1 und 3600 Sekunden liegen.')
        output = output_directory(args.output, create=True)
    except ValueError as error:
        parser.error(str(error))
    names = list(dict.fromkeys(args.suite or SUITES))
    summary = {'started': stamp(), 'finished': None, 'mode': 'qa-browser' if args.browser else 'offline',
               'expected_suites': len(names), 'expected_minimum_tests': sum(SUITES[name] for name in names),
               'full_application_selection': len(names) == len(SUITES),
               'suites': [], 'totals': {}, 'status': 'running'}
    destination = output / 'summary.json'

    def save():
        summary['totals'] = {
            'suites': len(summary['suites']), 'tests': sum(item['tests'] for item in summary['suites']),
            'skipped': sum(item['skipped'] for item in summary['suites']),
            'failures': sum(item['failures'] for item in summary['suites']),
            'errors': sum(item['errors'] for item in summary['suites']),
            'failed_suites': sum(item['status'] != 'passed' for item in summary['suites']),
        }
        summary['totals']['executed'] = summary['totals']['tests'] - summary['totals']['skipped']
        destination.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    save()
    # Exercise the real archive filesystem/reserve, not the smaller /tmp tmpfs.
    with tempfile.TemporaryDirectory(prefix='.sandbox-', dir=output) as data:
        for number, name in enumerate(names, 1):
            result_file, log = output / (name + '.json'), output / (name + '.log')
            environment = dict(os.environ, DATA_DIR=str(Path(data) / name / 'bootstrap'))
            for key in ('KIWM_OMLX_URL','KIWM_OMLX_API_KEY','KIWM_MODEL','KIWM_AGENT_MODEL','KIWM_ANALYSIS_MODEL','KIWM_VISION_MODEL','KIWM_PUBLIC_URL'):
                environment.pop(key,None)  # Each configuration test supplies its own synthetic overrides.
            if not args.browser:
                environment.pop('BROWSER_CDP_URL', None)
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', name, '--worker-result', str(result_file)]
            if args.browser:
                command.append('--browser')
            started = time.monotonic()
            returncode = None
            timed_out = False
            with log.open('w', encoding='utf-8') as stream:
                try:
                    completed = subprocess.run(command, cwd='/app', env=environment, stdout=stream, stderr=subprocess.STDOUT, timeout=args.timeout, check=False)
                    returncode = completed.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stream.write('\nRUNNER: Zeitlimit der Suite erreicht.\n')
            try:
                result = json.loads(result_file.read_text(encoding='utf-8'))
                if not isinstance(result, dict) or not all(key in result for key in ('tests', 'skipped', 'failures', 'errors', 'status')):
                    raise ValueError('Unvollstaendiges Worker-Ergebnis')
            except (OSError, ValueError):
                result = {'suite': name, 'tests': 0, 'skipped': 0, 'failures': 0, 'errors': 1,
                          'status': 'failed', 'skipped_cases': [], 'failed_cases': [],
                          'runner_error': 'Zeitlimit erreicht' if timed_out else 'Worker lieferte kein gueltiges Ergebnis'}
            result.update(returncode=returncode, timed_out=timed_out, log=log.name,
                          duration_seconds=round(time.monotonic() - started, 3))
            if returncode != 0 or timed_out:
                result['status'] = 'failed'
            summary['suites'].append(result)
            save()
            print(f"[{number}/{len(names)}] {name}: {result['status']} · {result['tests']} Tests · {result['skipped']} ausgelassen", flush=True)
    summary['finished'] = stamp()
    summary['status'] = 'failed' if summary['totals']['failed_suites'] else 'passed_with_skips' if summary['totals']['skipped'] else 'passed'
    save()
    print(json.dumps({'status': summary['status'], **summary['totals'], 'summary': str(destination)}, ensure_ascii=False), flush=True)
    return 1 if summary['totals']['failed_suites'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
