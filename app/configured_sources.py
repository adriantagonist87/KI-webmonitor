"""Extract bounded literal HTTP sources from a trusted project prompt only.

The caller supplies its existing URL policy. Never call this on website HTML,
source documents, tool results or model output to expand navigation authority.
"""
import re
from urllib.parse import urlsplit


MAX_PROMPT_CHARACTERS = 100000
MAX_URL_OCCURRENCES = 64
MAX_URL_CHARACTERS = 4096
_START = re.compile(r'(?<![\w@])https?://', re.I)
_CLOSING = {')': '(', ']': '[', '}': '{'}
_OPENING = set(_CLOSING.values())
_END = set('<>"\'`')
_TRAILING = '.,;:!?'


def extract_configured_sources(prompt, allowed):
    """Return permitted literal URLs, retaining fragments and original spelling.

    Markdown wrappers and sentence punctuation are delimiters, while balanced
    parentheses/brackets inside a URL are retained. The original candidate is
    checked before the caller may remove fragments or transient parameters.
    Limits apply to all literal occurrences, including denied/duplicate URLs.
    """
    if not isinstance(prompt, str):
        raise ValueError('Konfigurierte Quellen: Projektauftrag muss Text sein.')
    if len(prompt) > MAX_PROMPT_CHARACTERS:
        raise ValueError('Konfigurierte Quellen: Projektauftrag überschreitet 100000 Zeichen.')
    result = []
    seen = set()
    occurrences = 0
    cursor = 0
    while True:
        match = _START.search(prompt, cursor)
        if not match:
            break
        occurrences += 1
        if occurrences > MAX_URL_OCCURRENCES:
            raise ValueError('Konfigurierte Quellen: höchstens 64 wörtliche HTTP-Quelladressen im Projektauftrag erlaubt.')
        stack = []
        end = match.end()
        while end < len(prompt):
            char = prompt[end]
            if char.isspace() or ord(char) < 32 or char in _END:
                break
            if char in _OPENING:
                stack.append(char)
            elif char in _CLOSING:
                if not stack or stack[-1] != _CLOSING[char]:
                    break
                stack.pop()
            end += 1
            if end - match.start() > MAX_URL_CHARACTERS:
                raise ValueError('Konfigurierte Quellen: eine Quelladresse überschreitet 4096 Zeichen.')
        candidate = prompt[match.start():end].rstrip(_TRAILING)
        cursor = max(end, match.end())
        try:
            parsed = urlsplit(candidate)
            valid = (parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and '\\' not in candidate and not any(ord(char) < 32 or ord(char) == 127 for char in candidate))
            # Accessing port also rejects malformed/out-of-range ports without
            # rewriting the original candidate sent to the policy callback.
            parsed.port
        except (ValueError, TypeError):
            continue
        if valid and allowed(candidate) and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


__all__ = ['extract_configured_sources']
