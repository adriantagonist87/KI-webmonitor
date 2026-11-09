"""Passphrase-protected credential section in portable JSON and ZIP exports."""
import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


FORMAT = 'scrypt-aes256-gcm-v1'
MAX_CIPHERTEXT = 16 * 1024**2
PASSPHRASE_ERROR = 'Die Export-Passphrase muss 12 bis 1.024 Zeichen umfassen.'


class CredentialError(ValueError):
    """A fixed message suitable for display in the import form."""


def _password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 1024 or not value.strip():
        raise CredentialError(PASSPHRASE_ERROR)
    try:
        return value.encode('utf-8')
    except UnicodeError:
        raise CredentialError(PASSPHRASE_ERROR) from None


def _cipher(password, salt):
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password)
    return AESGCM(key)


def _context(payload):
    # Keep account bindings and connection settings authenticated. The
    # archive writer fills the unrelated file manifest after sealing.
    context = {key: payload[key] for key in ('format', 'version', 'kind', 'created_at', 'settings', 'site_auth')}
    context['projects'] = sorted((project['id'], project['url']) for project in payload['projects'])
    raw = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return FORMAT.encode('ascii') + b'\x00' + hashlib.sha256(raw.encode('utf-8')).digest()


def _decode(value, maximum):
    if not isinstance(value, str) or len(value) > 4 * ((maximum + 2) // 3):
        raise ValueError()
    raw = base64.b64decode(value, validate=True)
    if len(raw) > maximum:
        raise ValueError()
    return raw


def validate(section):
    if (not isinstance(section, dict) or set(section) != {'format', 'salt', 'nonce', 'ciphertext'}
            or section['format'] != FORMAT):
        raise CredentialError('Der verschlüsselte Zugangsdatenblock ist ungültig.')
    try:
        salt = _decode(section['salt'], 16)
        nonce = _decode(section['nonce'], 12)
        ciphertext = _decode(section['ciphertext'], MAX_CIPHERTEXT)
        if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) < 16:
            raise ValueError()
    except ValueError:
        raise CredentialError('Der verschlüsselte Zugangsdatenblock ist ungültig.') from None
    return salt, nonce, ciphertext


def seal(payload, credentials, passphrase):
    password = _password(passphrase)
    salt, nonce = os.urandom(16), os.urandom(12)
    raw = json.dumps(credentials, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) + 16 > MAX_CIPHERTEXT:
        raise CredentialError('Der Zugangsdatenblock ist zu groß.')
    ciphertext = _cipher(password, salt).encrypt(nonce, raw, _context(payload))
    return {'format': FORMAT, 'salt': base64.b64encode(salt).decode('ascii'),
            'nonce': base64.b64encode(nonce).decode('ascii'),
            'ciphertext': base64.b64encode(ciphertext).decode('ascii')}


def unseal(payload, passphrase):
    password = _password(passphrase)
    salt, nonce, ciphertext = validate(payload['protected_credentials'])
    try:
        raw = _cipher(password, salt).decrypt(nonce, ciphertext, _context(payload))
        return json.loads(raw)
    except (InvalidTag, ValueError, UnicodeError):
        raise CredentialError('Die Passphrase ist falsch oder das Exportpaket wurde verändert.') from None
