"""SMTP report timing and durable, retry-safe occurrence identities."""
from datetime import datetime, timedelta, timezone
import json

import scheduling
import store


DEFAULT_SCHEDULE = {**scheduling.DEFAULT, 'mode': 'weekly', 'time': '08:00'}
DEFAULT_PERIOD = {'amount': 7, 'unit': 'days'}
PERIOD_LIMITS = {'hours': 8760, 'days': 365}
STATE_KEY = 'smtp_report_due'


def validate_schedule(value):
    if not isinstance(value, dict):
        raise ValueError('Ungültiger Berichtszeitplan.')
    schedule = scheduling.validate({**DEFAULT_SCHEDULE, **value})
    field = {'weekly': 'weekday', 'monthly': 'monthday', 'interval': 'interval'}.get(schedule['mode'])
    return {**DEFAULT_SCHEDULE, **{key: schedule[key] for key in ('mode', 'time')},
            **({field: schedule[field]} if field else {})}


def validate_period(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_PERIOD):
        raise ValueError('Ungültiger Berichtszeitraum.')
    amount, unit = value['amount'], value['unit']
    if not isinstance(unit, str) or unit not in PERIOD_LIMITS or type(amount) is not int or not 1 <= amount <= PERIOD_LIMITS[unit]:
        raise ValueError('Wähle 1 bis 8.760 Stunden oder 1 bis 365 Tage als Berichtszeitraum.')
    return dict(value)


def next_due(schedule, after, previous=None):
    if schedule['mode'] == 'interval' and previous is None:
        # The first occurrence uses the chosen Berlin clock time. Later
        # occurrences use elapsed hours anchored at that first occurrence.
        return scheduling.next_for({**schedule, 'mode': 'daily'}, after)
    return scheduling.next_for(schedule, after, previous)


def _timestamp(value):
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is not None:
            return date.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    return None


def _cursor(schedule, due):
    return json.dumps({'schedule': schedule, 'due': due}, sort_keys=True)


def current(after=None, *, persist=False):
    after = after or datetime.now(timezone.utc)
    settings = store.persisted_settings()
    try:
        schedule = validate_schedule(settings.get('report_schedule', DEFAULT_SCHEDULE))
    except ValueError:
        schedule = dict(DEFAULT_SCHEDULE)
    try:
        period = validate_period(settings.get('report_period', DEFAULT_PERIOD))
    except ValueError:
        period = dict(DEFAULT_PERIOD)
    row = store.one('SELECT value FROM state WHERE key=?', (STATE_KEY,))
    try:
        cursor = json.loads(row['value']) if row else {}
    except ValueError:
        cursor = {}
    due = _timestamp(cursor.get('due')) if isinstance(cursor, dict) and cursor.get('schedule') == schedule else None
    if not row and schedule == DEFAULT_SCHEDULE:
        legacy = store.one("SELECT value FROM state WHERE key='weekly_due'")
        due = _timestamp(legacy['value']) if legacy else None
    due = due or next_due(schedule, after)
    value = _cursor(schedule, due)
    if persist and (not row or row['value'] != value):
        store.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (STATE_KEY, value))
    return {'schedule': schedule, 'period': period, 'due': due}


def advance(plan, after=None):
    after = after or datetime.now(timezone.utc)
    schedule, due = plan['schedule'], plan['due']
    following = next_due(schedule, after, due)
    store.execute('UPDATE state SET value=? WHERE key=? AND value=?',
                  (_cursor(schedule, following), STATE_KEY, _cursor(schedule, due)))


def window_start(end, period):
    return end - timedelta(**{period['unit']: period['amount']})


def occurrence_key(plan, fallback):
    due = datetime.fromisoformat(plan['due']) if plan else fallback
    if not plan or plan['schedule'] == DEFAULT_SCHEDULE:
        # Match existing archives for the default weekly slot after an upgrade.
        return 'week-' + due.astimezone(scheduling.TZ).strftime('%G-%V')
    return 'report-' + due.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
