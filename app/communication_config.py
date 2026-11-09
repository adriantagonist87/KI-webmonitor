"""Validate saved notification settings, with environment defaults for missing fields."""
import re
from email.utils import parseaddr
import configuration as base

FIELDS = {
    'smtp_enabled': 'KIWM_SMTP_ENABLED', 'smtp_host': 'KIWM_SMTP_HOST',
    'smtp_port': 'KIWM_SMTP_PORT', 'smtp_tls': 'KIWM_SMTP_TLS',
    'smtp_user': 'KIWM_SMTP_USER', 'smtp_from': 'KIWM_SMTP_FROM', 'smtp_to': 'KIWM_SMTP_TO',
    'telegram_mode': 'KIWM_TELEGRAM_MODE', 'telegram_chat_id': 'KIWM_TELEGRAM_CHAT_ID',
}
SECRETS = {'smtp_password': 'KIWM_SMTP_PASSWORD', 'telegram_token': 'KIWM_TELEGRAM_BOT_TOKEN'}
ENVIRONMENT_NAMES = tuple(FIELDS.values()) + tuple(SECRETS.values())


def validate(field, value):
    error = base.ConfigurationError
    if field=='smtp_password':
        if not isinstance(value,str) or len(value)>4096 or any(ord(c)<32 or ord(c)==127 for c in value):
            raise error('SMTP-Passwort enthält unzulässige Zeichen oder ist zu lang.')
        return value
    if field == 'smtp_enabled':
        if type(value) is bool: return value
        if isinstance(value, str) and value.lower() in ('true','false','1','0','on','off'):
            return value.lower() in ('true','1','on')
        raise error('SMTP aktiv: true oder false angeben.')
    if field == 'smtp_port':
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal() or not 1 <= int(value) <= 65535:
            raise error('SMTP-Port muss zwischen 1 und 65535 liegen.')
        return int(value)
    value = base._text(value, field, 4096 if field in SECRETS else 2000)
    if field == 'telegram_token':
        if value and not re.fullmatch(r'[0-9]{4,20}:[A-Za-z0-9_-]{20,200}', value):
            raise error('Bot-Token hat ein ungültiges Format.')
    elif field == 'telegram_chat_id':
        if value and not re.fullmatch(r'-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,63}', value):
            raise error('Gültige Telegram-Chat-ID oder @Kanalname angeben.')
    elif field == 'telegram_mode' and value not in ('off','projects','all'):
        raise error('Telegram-Versand: Aus, Projektvorgaben oder Alle Projekte wählen.')
    elif field == 'smtp_tls' and value not in ('ssl','starttls'):
        raise error('SMTP benötigt STARTTLS oder TLS / SSL.')
    elif field == 'smtp_host' and value:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', value):
            raise error('SMTP-Server als Hostname oder IPv4-Adresse ohne URL angeben.')
    elif field in ('smtp_from','smtp_to') and value:
        if parseaddr(value)[1] != value or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value):
            raise error('Eine gültige E-Mail-Adresse für Absender und Empfänger angeben.')
    return value


def resolve(saved, fields, environ=None, *, strict=True):
    result = dict(saved); env = base._environment(environ)
    for field, variable in fields.items():
        try:
            if field not in result:
                fallback = base._environment_value(env, variable)
                if fallback is None:
                    continue
                result[field] = env[variable] if field == 'smtp_password' else fallback
            result[field] = validate(field, result[field])
        except base.ConfigurationError as exc:
            if strict:
                raise
            result.setdefault('_communication_errors', {})[field] = str(exc)
            result.pop(field, None)
            if field == 'smtp_enabled':
                result[field] = False
            if field == 'telegram_mode':
                result[field] = 'off'
    return result


def sources(environ=None, *, settings=None, credentials=None):
    env = base._environment(environ)
    result = {}
    for field, variable in {**FIELDS, **SECRETS}.items():
        saved = (credentials if field in SECRETS else settings) or {}
        value = env.get(variable)
        fallback = field not in saved and isinstance(value, str) and bool(value.strip())
        result[field] = {'source': 'environment' if fallback else 'ui',
                         'environment': variable if fallback else None, 'readonly': False}
    return result
