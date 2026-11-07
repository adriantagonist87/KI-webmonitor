"""Pure collection-index checks; no database, network, browser or model."""
import copy
import json
import unittest

from collection_index import (MAX_RECORDS, MAX_TEXT, canonical, canonical_text,
                              index_html, normalize_records, plan_update,
                              scope_fingerprint)


def row(name='a', *, observed_date='2026-10-01', title=None, text='Technischer Inhalt 10'):
    return {'url': 'https://example.org/norm/' + name,
            'title': title or 'Norm ' + name,
            'date': observed_date, 'text': text}


def baseline(records):
    return plan_update(None, records, complete=True)['next_state']


class CollectionIndexTests(unittest.TestCase):
    def test_01_first_complete_scan_is_baseline_not_news(self):
        records = [row('b'), row('a')]
        result = plan_update(None, records, complete=True)
        self.assertTrue(result['commit']); self.assertTrue(result['baseline'])
        self.assertEqual(result['added'], []); self.assertEqual(result['updated'], [])
        self.assertTrue(result['next_state']['baseline_ready'])
        self.assertEqual([r['url'] for r in result['next_state']['records']], sorted(r['url'] for r in records))
        json.dumps(result, allow_nan=False)

    def test_02_new_prefix_entry_is_added_once_and_old_tail_retained(self):
        old = baseline([row('b'), row('c')])
        result = plan_update(old, [row('a'), row('b')], True)
        self.assertEqual(result['added'], [row('a')]); self.assertEqual(result['retained'], [row('c')])
        self.assertEqual(result['removed'], []); self.assertEqual(result['total_count'], 3)
        again = plan_update(result['next_state'], [row('a'), row('b')], True)
        self.assertEqual(again['added'], []); self.assertEqual(again['updated'], [])

    def test_03_single_digit_date_and_title_corrections_are_explicit(self):
        before = row('a')
        after = row('a', observed_date='2026-10-02', title='Norm a – Korrektur', text='Technischer Inhalt 11')
        result = plan_update(baseline([before]), [after], True)
        self.assertEqual(result['added'], [])
        self.assertEqual(result['updated'], [{'url': before['url'], 'before': before, 'after': after,
                                              'fields': ['title', 'date', 'text']}])
        self.assertIn('Technischer Inhalt 11', canonical_text(result['next_state']['records']))

    def test_04_reorder_and_identical_duplicates_are_not_changes(self):
        a, b = row('a'), row('b')
        result = plan_update(baseline([a, b]), [b, a, a], True)
        self.assertEqual(result['added'], []); self.assertEqual(result['updated'], [])
        self.assertEqual(canonical_text([a, b]), canonical_text([b, a, a]))

    def test_05_shorter_or_empty_prefix_never_removes_unvisited_records(self):
        old = baseline([row('a'), row('b'), row('c')])
        result = plan_update(old, [row('a')], True)
        self.assertEqual(result['next_state'], old); self.assertEqual(result['removed'], [])
        self.assertEqual(len(result['retained']), 2)
        empty = plan_update(old, [], True)
        self.assertEqual(empty['next_state'], old); self.assertEqual(len(empty['retained']), 3)

    def test_06_new_year_adds_ids_without_resetting_previous_year(self):
        old = baseline([row('old', observed_date='2026-12')])
        new = row('new', observed_date='2027-01')
        result = plan_update(old, [new], True)
        self.assertFalse(result['baseline']); self.assertEqual(result['added'], [new])
        self.assertEqual(result['retained'], old['records'])
        self.assertEqual(result['total_count'], 2)

    def test_07_incomplete_scan_cannot_commit_or_claim_additions(self):
        old = baseline([row('a')]); before = copy.deepcopy(old)
        result = plan_update(old, [row('new')], False)
        self.assertFalse(result['commit']); self.assertIsNone(result['next_state'])
        self.assertEqual(result['added'], []); self.assertEqual(result['updated'], [])
        self.assertEqual(result['verified_urls'], []); self.assertEqual(result['retained'], old['records'])
        self.assertEqual(old, before)
        first = plan_update(None, [row('a')], False)
        self.assertFalse(first['baseline']); self.assertIsNone(first['next_state'])

    def test_08_dates_validate_calendar_and_preserve_month_precision(self):
        for value in ('2026-13', '2026-00', '2026-02-29', '2026-04-31', '2026', '01.10.2026', '', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_records([row(observed_date=value)])
        self.assertEqual(normalize_records([row(observed_date='2024-02-29')])[0]['date'], '2024-02-29')
        self.assertEqual(normalize_records([row(observed_date=' 2026-10 ')])[0]['date'], '2026-10')

    def test_09_title_year_is_never_used_as_missing_publication_date(self):
        source = row(title='DIN VDE 0000:2026-12', observed_date='')
        with self.assertRaises(ValueError): normalize_records([source])
        source['date'] = '2025-04'
        self.assertEqual(normalize_records([source])[0]['date'], '2025-04')

    def test_10_conflicting_duplicate_url_is_rejected(self):
        with self.assertRaises(ValueError): normalize_records([row('a'), row('a', text='Anderer Inhalt')])
        with self.assertRaises(ValueError): plan_update(baseline([row('old')]), [row('a'), row('a', observed_date='2026-11')], True)

    def test_11_credential_urls_non_https_controls_and_overlong_text_rejected(self):
        for value in ('http://example.org/a', 'https://u:p@example.org/a', 'https://u@example.org/a',
                      'javascript:alert(1)', 'https://example.org/a\nq', 'https://example.org:bad/a',
                      'https://example.org/a?password=private', 'https://example.org/app#access_token=private',
                      'https://example.org/app#/norm/100?api_key=private'):
            item = row(); item['url'] = value
            with self.subTest(value=value), self.assertRaises(ValueError): normalize_records([item])
        with self.assertRaises(ValueError): normalize_records([row(text='x' * (MAX_TEXT + 1))])
        bad = row(); bad['password'] = 'not-a-record-field'
        with self.assertRaises(ValueError): normalize_records([bad])

    def test_12_observed_query_and_spa_fragment_are_preserved(self):
        item = row(); item['url'] = 'https://example.org/app?version=2#norm/100'
        self.assertEqual(normalize_records([item])[0]['url'], item['url'])

    def test_13_input_and_union_record_limits(self):
        with self.assertRaises(ValueError): normalize_records([row()] * (MAX_RECORDS + 1))
        # An existing full collection may still be rechecked in full without
        # counting every overlap twice toward the union limit.
        records = [row(str(i), text='') for i in range(MAX_RECORDS)]
        self.assertEqual(len(canonical(records, records)), MAX_RECORDS)
        with self.assertRaises(ValueError): canonical(records, [row('additional')])

    def test_14_scope_uses_exact_filter_and_structural_selectors(self):
        selectors = {'items': '.result-card', 'date': '.publication-date', 'scroll': '#results'}
        same = dict(reversed(list(selectors.items())))
        a = scope_fingerprint(selectors, 'Normen')
        self.assertEqual(len(a), 64)
        self.assertEqual(a, scope_fingerprint(same, 'Normen'))
        self.assertNotEqual(a, scope_fingerprint(selectors, 'IEC Normen'))
        self.assertNotEqual(a, scope_fingerprint({**selectors, 'date': '.issue-date'}, 'Normen'))
        # A calendar rollover changes records, not these structural inputs.
        for year in ('2026-12', '2027-01'):
            canonical_text([row(observed_date=year)])
            self.assertEqual(a, scope_fingerprint(selectors, 'Normen'))

    def test_15_canonical_html_is_inert_escaped_and_scope_marked(self):
        item = row(title='<img src=x onerror=evil()>', text='<script>evil()</script>')
        item['url'] = 'https://example.org/a?x=1&y=2'
        html = index_html([item], 'collection-index:123')
        self.assertIn('data-kiwm-scope="collection-index:123"', html)
        self.assertNotIn('<img', html); self.assertNotIn('<script', html)
        self.assertIn('&lt;script&gt;', html); self.assertIn('x=1&amp;y=2', html)
        self.assertEqual(html, index_html([item, item], 'collection-index:123'))

    def test_16_bad_states_flags_and_input_mutation(self):
        for state in ({}, {'version': 1, 'baseline_ready': 1, 'records': []},
                      {'version': True, 'baseline_ready': False, 'records': []}):
            with self.assertRaises(ValueError): plan_update(state, [], True)
        with self.assertRaises(ValueError): plan_update(None, [], 1)
        with self.assertRaises(ValueError): canonical([], [], first=1)
        records = [row('b'), row('a')]; before = copy.deepcopy(records)
        state = baseline(records); old = copy.deepcopy(state)
        plan_update(state, [row('a', text='Korrektur 11')], True)
        self.assertEqual(records, before); self.assertEqual(state, old)


if __name__ == '__main__': unittest.main(verbosity=2)
