"""Narrow exception for observed read-only VdS storefront GraphQL requests.

The browser's normal POST guard stays in force for every other request. This
module neither sends requests nor logs request bodies, variables, or cookies.
"""
import json
import math
from urllib.parse import urlsplit

from graphql import parse
from graphql.language import OperationType
from graphql.language.ast import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
)


# Extend only after the corresponding catalog query was actually observed.
CATALOG_ROOT_FIELDS = {
    'urls', 'heroSlides', 'user', 'collections', 'basket', 'categories', 'branches',
    'publications', 'recentPublicationsFireprotection', 'recentPublicationsSecurity', 'publication',
}
MAX_BODY_BYTES = 256 * 1024
MAX_AST_TOKENS = 5000
MAX_DEPTH = 32
MAX_EXPANDED_SELECTIONS = 5000
_ORIGIN = ('https', 'shop.vds.de', 443)


def _origin(url):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        return (parts.scheme, parts.hostname, 443 if port is None else port)
    except (TypeError, ValueError):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Non-JSON number')


def _sane_variables(variables):
    """Bound nesting and expanded JSON values independently of the byte cap."""
    if not isinstance(variables, dict):
        return False
    stack = [(variables, 0)]
    remaining = MAX_EXPANDED_SELECTIONS
    while stack:
        value, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            return False
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                return False
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                return False
        elif value is not None and not isinstance(value, (str, int, bool)):
            return False
    return True


def _readonly_document(query, operation_name=None):
    """Resolve root fragments; aliases never grant permission to a field."""
    if not isinstance(query, str) or not query.strip():
        return False
    try:
        document = parse(query, no_location=True, max_tokens=MAX_AST_TOKENS)
        operations = []
        fragments = {}
        for definition in document.definitions:
            if isinstance(definition, OperationDefinitionNode):
                operations.append(definition)
            elif isinstance(definition, FragmentDefinitionNode):
                name = definition.name.value
                if name in fragments:
                    return False
                fragments[name] = definition
            else:
                return False
        if len(operations) != 1 or operations[0].operation != OperationType.QUERY:
            return False
        operation = operations[0]
        if operation_name is not None:
            if (not isinstance(operation_name, str) or not operation_name
                    or operation.name is None or operation.name.value != operation_name):
                return False
        remaining = [MAX_EXPANDED_SELECTIONS]
        used_fragments = set()

        def walk(selection_set, root, trail=(), depth=0):
            if depth > MAX_DEPTH:
                return False
            for selection in selection_set.selections:
                remaining[0] -= 1
                if remaining[0] < 0:
                    return False
                if isinstance(selection, FieldNode):
                    if root and selection.name.value not in CATALOG_ROOT_FIELDS:
                        return False
                    if selection.selection_set is not None:
                        if not walk(selection.selection_set, False, trail, depth + 1):
                            return False
                elif isinstance(selection, InlineFragmentNode):
                    if not walk(selection.selection_set, root, trail, depth + 1):
                        return False
                elif isinstance(selection, FragmentSpreadNode):
                    name = selection.name.value
                    if name not in fragments or name in trail:
                        return False
                    used_fragments.add(name)
                    if not walk(fragments[name].selection_set, root, trail + (name,), depth + 1):
                        return False
                else:
                    return False
            return True

        return (walk(operation.selection_set, True)
                and used_fragments == set(fragments))
    except Exception:
        # Parser messages may contain source snippets; never return or log them.
        return False


def _readonly_payload(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BODY_BYTES:
        return False
    try:
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
        if not isinstance(payload, dict):
            return False
        # Reject batch, persisted-only queries, and server-specific overrides.
        if set(payload) - {'query', 'operationName', 'variables'}:
            return False
        if 'operationName' in payload and not isinstance(payload['operationName'], str):
            return False
        if not _sane_variables(payload.get('variables', {})):
            return False
        return _readonly_document(payload.get('query'), payload.get('operationName'))
    except Exception:
        return False


async def permits_readonly_graphql(request, page, project_url):
    """Return whether this exact browser POST is the observed read-only case."""
    try:
        if _origin(project_url) != _ORIGIN or _origin(request.url) != _ORIGIN:
            return False
        endpoint = urlsplit(request.url)
        if endpoint.path != '/graphql' or endpoint.query or endpoint.fragment:
            return False
        if (request.method != 'POST' or request.resource_type not in ('fetch', 'xhr')
                or request.is_navigation_request()):
            return False
        frame = request.frame
        if (frame is not page.main_frame or frame.page is not page
                or frame.parent_frame is not None or _origin(frame.url) != _ORIGIN):
            return False
        return _readonly_payload(request.post_data_buffer)
    except Exception:
        # Detached frames and unavailable request bodies fail closed.
        return False


__all__ = ['CATALOG_ROOT_FIELDS', 'permits_readonly_graphql']
