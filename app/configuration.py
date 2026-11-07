"""Resolve local-inference deployment overrides without persisting or logging them.

Explicit, nonempty environment values win over saved UI values. A role-specific
model wins over KIWM_MODEL; without either, that role keeps its saved model.
This module performs syntax validation only, never DNS or network requests.
"""
from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from urllib.parse import urlsplit


SETTING_ENVIRONMENT = {
    'omlx_url': 'KIWM_OMLX_URL',
    'agent_model': 'KIWM_AGENT_MODEL',
    'analysis_model': 'KIWM_ANALYSIS_MODEL',
    'vision_model': 'KIWM_VISION_MODEL',
}
SECRET_ENVIRONMENT = {'omlx_key': 'KIWM_OMLX_API_KEY'}
MODEL_FIELDS = ('agent_model', 'analysis_model', 'vision_model')
ENVIRONMENT_NAMES = ('KIWM_OMLX_URL', 'KIWM_OMLX_API_KEY', 'KIWM_MODEL',
                     'KIWM_AGENT_MODEL', 'KIWM_ANALYSIS_MODEL', 'KIWM_VISION_MODEL')
_PRIVATE_SETTINGS = frozenset({'omlx_key', 'omlx_api_key', 'api_key', 'smtp_password',
                               'telegram_token', 'KIWM_TELEGRAM_BOT_TOKEN', 'KIWM_SMTP_PASSWORD',
                               'site_logins', 'credentials', 'secrets', 'KIWM_OMLX_API_KEY'})


class ConfigurationError(ValueError):
    """A safe field-specific error; supplied configuration values are never echoed."""


def _object(value, label):
    if not isinstance(value, Mapping):
        raise ConfigurationError(label+' muss ein JSON-Objekt sein.')
    return copy.deepcopy(dict(value))


def public_settings(value):
    """Copy a saved public configuration, refusing misplaced private fields."""
    result = _object(value, 'Einstellungen')
    if _PRIVATE_SETTINGS.intersection(result):
        raise ConfigurationError('Zugangsdaten gehören in die getrennte Zugangsdatenablage, nicht in Einstellungen.')
    return result


def private_credentials(value):
    return _object(value, 'Zugangsdaten')


def _environment(environ):
    result = os.environ if environ is None else environ
    if not isinstance(result, Mapping):
        raise ConfigurationError('Umgebungsvariablen müssen eine Zuordnung sein.')
    return result


def _environment_value(environ, name):
    value = environ.get(name)
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ConfigurationError(name+' muss Text sein.')
    if not value.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(name+' enthält unzulässige Steuerzeichen.')
    return value.strip()


def _text(value, field, limit):
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ConfigurationError(field+' muss gültiger Text ohne Steuerzeichen sein (höchstens '+str(limit)+' Zeichen).')
    if value != value.strip():
        raise ConfigurationError(field+' darf keine führenden oder nachgestellten Leerzeichen enthalten.')
    return value


def validate_url(value, field='omlx_url'):
    value = _text(value, field, 2000)
    if not value:
        return value  # An unconfigured UI is allowed; the gateway rejects use.
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and not any(character.isspace() for character in value)
                 and '\\' not in value and not parsed.netloc.endswith(':')
                 and (port is None or 1 <= port <= 65535))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ConfigurationError(field+' benötigt eine HTTP(S)-Adresse ohne Zugangsdaten, Query oder Fragment und mit gültigem Port.')
    return value


def validate_model(value, field='Modell'):
    return _text(value, field, 512)


def validate_api_key(value, field='omlx_key'):
    value = _text(value, field, 4096)
    if any(not 33 <= ord(character) <= 126 for character in value):
        raise ConfigurationError(field+' muss ein ASCII-Token ohne Leerzeichen sein.')
    return value


def resolve_settings(saved, environ=None):
    """Return public effective settings; no secret is read or added here."""
    result = public_settings(saved)
    environment = _environment(environ)
    common = _environment_value(environment, 'KIWM_MODEL')
    if common is not None:
        validate_model(common, 'KIWM_MODEL')
    for field, variable in SETTING_ENVIRONMENT.items():
        override = _environment_value(environment, variable)
        if override is None and field in MODEL_FIELDS:
            override = common
        if override is not None:
            result[field] = override
        if field in result:
            label = variable if _environment_value(environment, variable) is not None else (
                'KIWM_MODEL' if field in MODEL_FIELDS and common is not None else field)
            (validate_url if field == 'omlx_url' else validate_model)(result[field], label)
    return result


def resolve_credentials(saved, environ=None):
    """Resolve the API token only in the private credentials object."""
    result = private_credentials(saved)
    environment = _environment(environ)
    override = _environment_value(environment, SECRET_ENVIRONMENT['omlx_key'])
    if override is not None:
        result['omlx_key'] = override
    if 'omlx_key' in result:
        validate_api_key(result['omlx_key'], 'KIWM_OMLX_API_KEY' if override is not None else 'omlx_key')
    return result


def sources(environ=None):
    """Public UI metadata only: origin/lock status, never a token or other value."""
    environment = _environment(environ)
    result = {}
    common = _environment_value(environment, 'KIWM_MODEL')
    for field, variable in {**SETTING_ENVIRONMENT, **SECRET_ENVIRONMENT}.items():
        active = variable if _environment_value(environment, variable) is not None else (
            'KIWM_MODEL' if field in MODEL_FIELDS and common is not None else None)
        result[field] = {'source': 'environment' if active else 'ui',
                         'environment': active, 'readonly': active is not None}
    return result
