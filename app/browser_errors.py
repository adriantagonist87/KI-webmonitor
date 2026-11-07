"""Describe browser failures without recording URLs, credentials or provider bodies."""
import traceback
from pathlib import Path
from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout


def describe(error):
    if not isinstance(error, BrowserError):
        return None
    message = str(error).lower()
    if isinstance(error, BrowserTimeout):
        return 'Die Webseite oder eine Browseraktion hat nicht rechtzeitig geantwortet.'
    if 'execution context was destroyed' in message or 'cannot find context' in message:
        return 'Die Webseite hat während der Prüfung weitergeleitet; die Seitenansicht war noch nicht stabil.'
    if 'err_aborted' in message or 'interrupted by another navigation' in message:
        return 'Der Seitenaufruf wurde durch eine weitere Navigation oder eine blockierte Weiterleitung unterbrochen.'
    if 'err_http_response_code_failure' in message:
        return 'Der Anbieter hat den Seitenaufruf mit einer HTTP-Fehlerantwort abgewiesen.'
    if any(value in message for value in ('err_connection', 'err_name_not_resolved', 'err_proxy', 'err_timed_out')):
        return 'Die Webseite konnte über die Browserverbindung nicht erreicht werden.'
    if 'target page, context or browser has been closed' in message:
        return 'Die Browseransicht wurde während der Prüfung geschlossen.'
    return 'Eine Browseraktion ist fehlgeschlagen. Die technische Fehlerstelle ist im Ablaufprotokoll hinterlegt.'


def location(error):
    """Only our module and function names; never exception arguments or source lines."""
    frames = [f for f in traceback.extract_tb(error.__traceback__)
              if str(Path(f.filename).parent) == '/app']
    return ' → '.join(f'{Path(f.filename).name}:{f.lineno} ({f.name})' for f in frames[-4:])
