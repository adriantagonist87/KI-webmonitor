"""Friendly schedules; calendar times use Berlin, intervals use elapsed UTC hours."""
import calendar
import json
import re
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
import store

TZ = ZoneInfo('Europe/Berlin')
DEFAULT = {'mode': 'daily', 'time': '06:00', 'weekday': 0, 'monthday': 1, 'interval': 6}
DAYS = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag']

def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Ungültiger Prüfzeitplan.')
    result = {**DEFAULT, **value}
    if result['mode'] not in ('daily', 'weekly', 'monthly', 'weekdays', 'interval'):
        raise ValueError('Bitte eine gültige Wiederholung wählen.')
    if not isinstance(result['time'], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', result['time']):
        raise ValueError('Bitte eine Uhrzeit im Format HH:MM wählen.')
    for key, low, high in [('weekday', 0, 6), ('monthday', 1, 31), ('interval', 1, 168)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Ungültiger Wochentag, Monatstag oder Stundenabstand.')
    return result

def from_form(form):
    try:
        return validate({'mode': str(form.get('schedule_mode', 'daily')),
                         'time': str(form.get('schedule_time', '06:00')),
                         'weekday': int(form.get('schedule_weekday') or 0),
                         'monthday': int(form.get('schedule_monthday') or 1),
                         'interval': int(form.get('schedule_interval') or 6)})
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc) if isinstance(exc, ValueError) and not str(exc).startswith('invalid literal') else 'Ungültiger Prüfzeitplan.') from None

def to_cron(value):
    s = validate(value); hour, minute = map(int, s['time'].split(':'))
    return {'daily': f'{minute} {hour} * * *',
            'weekly': f'{minute} {hour} * * {(s["weekday"] + 1) % 7}',
            'weekdays': f'{minute} {hour} * * 1-5',
            'monthly': f'{minute} {hour} {s["monthday"]} * *',
            'interval': f'0 */{s["interval"]} * * *' if s['interval'] <= 23 else '0 * * * *'}[s['mode']]

def from_cron(value):
    try:
        minute, hour, day, month, weekday = value.split()
        if not minute.isdigit() or not hour.isdigit() or month != '*':
            return {'mode': 'custom', 'label': 'Bisheriger Zeitplan'}
        s = {**DEFAULT, 'time': f'{int(hour):02}:{int(minute):02}'}
        if day == '*' and weekday == '*': s['mode'] = 'daily'
        elif day == '*' and weekday == '1-5': s['mode'] = 'weekdays'
        elif day == '*' and weekday.isdigit() and 0 <= int(weekday) <= 7:
            s.update(mode='weekly', weekday=(int(weekday) - 1) % 7)
        elif day.isdigit() and weekday == '*': s.update(mode='monthly', monthday=int(day))
        else: return {'mode': 'custom', 'label': 'Bisheriger Zeitplan'}
        return validate(s)
    except (TypeError, ValueError):
        return {'mode': 'custom', 'label': 'Bisheriger Zeitplan'}

def global_schedule():
    try: return validate(store.settings().get('global_schedule', DEFAULT))
    except ValueError: return dict(DEFAULT)

def project_schedule(project):
    if project.get('use_global_schedule'): return global_schedule()
    try:
        value = json.loads(project.get('schedule_config') or '{}')
        if value: return validate(value)
    except (ValueError, TypeError): pass
    return from_cron(project['cron'])

def next_for(value, after=None, anchor=None):
    s = validate(value); after = after or datetime.now(timezone.utc)
    if after.tzinfo is None: raise ValueError('Zeitpunkt benötigt eine Zeitzone.')
    after = after.astimezone(timezone.utc)
    if s['mode'] == 'interval':
        period = timedelta(hours=s['interval'])
        if anchor:
            anchor = datetime.fromisoformat(anchor) if isinstance(anchor, str) else anchor
            if anchor.tzinfo is None: raise ValueError('Zeitanker benötigt eine Zeitzone.')
            anchor = anchor.astimezone(timezone.utc)
            if anchor > after: return anchor.isoformat()
            return (anchor + period * (int((after-anchor) // period) + 1)).isoformat()
        return (after + period).isoformat()
    local = after.astimezone(TZ); hour, minute = map(int, s['time'].split(':'))
    for offset in range(370):
        day = local.date() + timedelta(days=offset)
        if s['mode'] == 'weekly' and day.weekday() != s['weekday']: continue
        if s['mode'] == 'weekdays' and day.weekday() >= 5: continue
        if s['mode'] == 'monthly' and day.day != min(s['monthday'], calendar.monthrange(day.year, day.month)[1]): continue
        # Round-trip resolves a spring clock gap forward. Fold 0 ensures a
        # repeated autumn clock time runs once, rather than twice that day.
        candidate = datetime.combine(day, time(hour, minute), TZ).astimezone(timezone.utc)
        if candidate > after: return candidate.isoformat()
    raise ValueError('Kein nächster Prüftermin gefunden.')

def next_project_due(project, after=None, advancing=False):
    s = project_schedule(project)
    if s['mode'] == 'custom': return store.next_due(project['cron'], after)
    # Existing cron-only projects keep their exact historical semantics until
    # explicitly edited; new friendly schedules carry their own configuration.
    if not project.get('use_global_schedule') and not json.loads(project.get('schedule_config') or '{}'):
        return store.next_due(project['cron'], after)
    return next_for(s, after, project.get('next_due') if advancing else None)

def label(value):
    if isinstance(value, str): value = from_cron(value)
    if value.get('mode') == 'custom': return 'Bisheriger Zeitplan'
    s = validate(value); at = s['time'] + ' Uhr'
    return {'daily': 'Täglich um ' + at,
            'weekdays': 'Montag–Freitag um ' + at,
            'weekly': DAYS[s['weekday']] + ', ' + at,
            'monthly': f'Monatlich am {s["monthday"]}., {at}',
            'interval': f'Alle {s["interval"]} Stunden'}[s['mode']]
