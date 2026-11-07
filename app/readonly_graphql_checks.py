"""Synthetic GraphQL request checks; no browser, network, or credentials."""
import asyncio
import contextlib
import io
import json
import unittest

from readonly_graphql import (
    MAX_BODY_BYTES, MAX_DEPTH, _readonly_document, _readonly_payload,
    permits_readonly_graphql,
)


PROJECT_URL = 'https://shop.vds.de/publikation/'
CONTEXT_QUERY = '''query getContext($language: String) {
    urls { home }
    heroSlides { id title }
    user { id }
    collections { id }
    basket { id }
    categories { id title }
    branches { id }
}'''


def body(query=CONTEXT_QUERY, **extra):
    return json.dumps({'query': query, **extra}).encode()


class FakeFrame:
    def __init__(self, page, url=PROJECT_URL, parent=None):
        self.page, self.url, self.parent_frame = page, url, parent


class FakePage:
    def __init__(self):
        self.main_frame = FakeFrame(self)


class FakeRequest:
    def __init__(self, page, **changes):
        self.url = 'https://shop.vds.de/graphql'
        self.method = 'POST'
        self.resource_type = 'fetch'
        self.frame = page.main_frame
        self.post_data_buffer = body(operationName='getContext', variables={})
        self.navigation = False
        for key, value in changes.items():
            setattr(self, key, value)
    def is_navigation_request(self):
        return self.navigation


class ReadonlyGraphQLTests(unittest.TestCase):
    def permits(self, request, page, project_url=PROJECT_URL):
        return asyncio.run(permits_readonly_graphql(request, page, project_url))

    def test_01_normal_observed_get_context(self):
        page = FakePage()
        self.assertTrue(self.permits(FakeRequest(page), page))
        self.assertTrue(self.permits(FakeRequest(page, resource_type='xhr'), page))

    def test_02_mutations_subscriptions_and_multiple_operations_rejected(self):
        queries = [
            'mutation getContext { basket { id } }',
            'subscription getContext { categories { id } }',
            'query getContext { urls } mutation buy { basket { id } }',
            'query getContext { urls } query other { categories { id } }',
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertFalse(_readonly_document(query, 'getContext'))

    def test_03_alias_checks_real_field_name(self):
        self.assertTrue(_readonly_document('{ mutation: categories { id } }'))
        self.assertFalse(_readonly_document('{ categories: deleteBasket { id } }'))

    def test_04_root_and_nested_fragments_are_resolved(self):
        query = '''query getContext { ...Root }
            fragment Root on Query { ... on Query { urls } categories { ...Item } }
            fragment Item on Category { id title }'''
        self.assertTrue(_readonly_document(query, 'getContext'))
        self.assertFalse(_readonly_document('query { ...F } fragment F on Query { deleteBasket }'))

    def test_05_cycles_missing_duplicate_and_unused_fragments_rejected(self):
        queries = [
            'query { ...F } fragment F on Query { urls ...F }',
            'query { ...A } fragment A on Query { ...B } fragment B on Query { ...A }',
            'query { ...Missing }',
            'query { ...F } fragment F on Query { urls } fragment F on Query { categories { id } }',
            'query { urls } fragment F on Query { ...F }',
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertFalse(_readonly_document(query))

    def test_06_comments_and_mutation_word_in_literal_are_not_operations(self):
        query = '# mutation deleteBasket\nquery getContext { urls(note: "mutation deleteBasket") }'
        self.assertTrue(_readonly_document(query, 'getContext'))

    def test_07_operation_name_must_match_exactly(self):
        self.assertTrue(_readonly_document('{ urls }'))
        for name in ('other', '', 1):
            with self.subTest(name=name):
                self.assertFalse(_readonly_document(CONTEXT_QUERY, name))
        self.assertFalse(_readonly_document('{ urls }', 'getContext'))
        self.assertFalse(_readonly_payload(body(operationName=None)))

    def test_08_unknown_root_and_schema_definitions_rejected(self):
        self.assertFalse(_readonly_document('{ products { id } }'))
        self.assertFalse(_readonly_document('{ __schema { types { name } } }'))
        self.assertFalse(_readonly_document('query { urls } type Foo { bar: String }'))

    def test_09_batch_persisted_only_and_extension_payloads_rejected(self):
        payloads = [
            [{'query': '{ urls }'}],
            {'operationName': 'getContext', 'variables': {}},
            {'extensions': {'persistedQuery': {'version': 1, 'sha256Hash': 'fake'}}},
            {'query': '{ urls }', 'extensions': {'persistedQuery': {}}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertFalse(_readonly_payload(json.dumps(payload).encode()))

    def test_10_endpoint_origin_and_transport_are_exact(self):
        page = FakePage()
        changes = [
            {'url': 'http://shop.vds.de/graphql'},
            {'url': 'https://shop.vds.de:444/graphql'},
            {'url': 'https://shop.vds.de:0/graphql'},
            {'url': 'https://shop.vds.de.evil.example/graphql'},
            {'url': 'https://other.vds.de/graphql'},
            {'url': 'https://u:p@shop.vds.de/graphql'},
            {'url': 'https://shop.vds.de/graphql/'},
            {'url': 'https://shop.vds.de/GraphQL'},
            {'url': 'https://shop.vds.de/graphql?operationName=getContext'},
            {'url': 'https://shop.vds.de/graphql#fragment'},
            {'method': 'GET'}, {'resource_type': 'document'}, {'navigation': True},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(self.permits(FakeRequest(page, **change), page))
        self.assertTrue(self.permits(FakeRequest(page, url='https://shop.vds.de:443/graphql'), page))
        self.assertFalse(self.permits(FakeRequest(page), page, 'https://example.org/'))

    def test_11_other_page_child_and_foreign_origin_frames_rejected(self):
        page, other = FakePage(), FakePage()
        self.assertFalse(self.permits(FakeRequest(page, frame=other.main_frame), page))
        self.assertFalse(self.permits(FakeRequest(page, frame=FakeFrame(page, parent=page.main_frame)), page))
        for url in ('https://evil.example/', 'http://shop.vds.de/', 'about:blank'):
            with self.subTest(url=url):
                page.main_frame.url = url
                self.assertFalse(self.permits(FakeRequest(page), page))

    def test_12_body_cap_and_bad_json_fail_closed(self):
        valid = body('{ urls }')
        self.assertTrue(_readonly_payload(valid + b' ' * (MAX_BODY_BYTES - len(valid))))
        for raw in (valid + b' ' * MAX_BODY_BYTES, b'', b'{', b'\xff', None, '{"query":"{ urls }"}'):
            with self.subTest(kind=type(raw).__name__):
                self.assertFalse(_readonly_payload(raw))
        self.assertFalse(_readonly_payload(b'{"query":"{ urls }","query":"{ basket { id } }"}'))

    def test_13_variables_are_json_object_with_bounded_nesting(self):
        self.assertTrue(_readonly_payload(body(variables={'language': 'de', 'filter': {'ids': [1, True, None]}})))
        for variables in (None, [], 'de', 1, {'bad': float('nan')}):
            with self.subTest(variables=variables):
                self.assertFalse(_readonly_payload(body(variables=variables)))
        nested = {}
        for _ in range(MAX_DEPTH + 2):
            nested = {'next': nested}
        self.assertFalse(_readonly_payload(body(variables=nested)))
        self.assertFalse(_readonly_payload(body(variables={'many': [0] * 6000})))

    def test_14_ast_depth_token_and_expansion_budgets(self):
        self.assertFalse(_readonly_document('{ categories { ' + 'child { ' * (MAX_DEPTH + 1) + 'id' + ' }' * (MAX_DEPTH + 1) + ' } }'))
        self.assertFalse(_readonly_document('{ ' + 'urls ' * 6000 + '}'))
        fragments = ['fragment F0 on Query { urls }']
        for index in range(1, 14):
            fragments.append(f'fragment F{index} on Query {{ ...F{index - 1} ...F{index - 1} }}')
        self.assertFalse(_readonly_document('query { ...F13 } ' + ' '.join(fragments)))

    def test_15_invalid_source_or_request_never_prints_body(self):
        page = FakePage()
        request = FakeRequest(page, post_data_buffer=body('mutation { fakeCredentialCanary }'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertFalse(self.permits(request, page))
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
