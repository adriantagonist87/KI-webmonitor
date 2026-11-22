"""Category completeness, baseline and classification checks without network."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import store
import vds_category as vds


URL = vds.ORIGIN + '/de/kategorie/brandschutz/elektrische-anlagen'


def entry(slug, number, title, *status):
    return {'url': vds.ORIGIN + '/de/publikation/' + slug,
            'number': number, 'title': title, 'status': list(status)}


class VdSCategoryChecks(unittest.TestCase):
    def test_category_response_matches_read_field_across_operation_renames(self):
        endpoint = vds.ORIGIN + '/graphql'
        for query, name, result in (
            ('query ShopCategory { publicationsByCategory { slugName } }', 'ShopCategory', True),
            ('query getPublicationListByCategory { categories { id } }', 'getPublicationListByCategory', False),
            ('mutation ShopCategory { publicationsByCategory { slugName } }', 'ShopCategory', False),
        ):
            body = json.dumps({'query':query,'operationName':name}).encode()
            request = SimpleNamespace(url=endpoint,method='POST',post_data_buffer=body)
            response = SimpleNamespace(url=endpoint,request=request)
            with self.subTest(query=query):
                self.assertEqual(vds.category_response(response),result)

    def test_incomplete_and_mismatched_catalogs_do_not_compare(self):
        row = entry('vds-2006', 'VdS 2006', 'Blitzschutz', 'Zurückgezogen')
        expected = vds.expected_entries({'data': {'publicationsByCategory': [
            {'slugName': 'vds-2006', 'state': 'WITHDRAWN'},
        ]}})
        self.assertEqual(vds.verified_entries([row], expected), {row['url']: row})
        for rows in ([], [row, row], [{**row, 'status': []}]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                vds.verified_entries(rows, expected)
        with self.assertRaises(RuntimeError):
            vds.expected_entries({'data': {'publicationsByCategory': []}})
        with self.assertRaises(RuntimeError):
            vds.expected_entries({'errors': [{'message': 'unvollständig'}]})

    def test_baseline_and_three_distinct_change_types_with_titles(self):
        removed = entry('vds-6023en', 'VdS 6023en', 'Rooftop Photovoltaic Systems')
        retained = entry('vds-2006', 'VdS 2006', 'Blitzschutz')
        added = entry('vds-6159', 'VdS 6159', 'Lithium-Ionen-Akkus')
        with tempfile.TemporaryDirectory(dir='/data') as directory:
            root = Path(directory)
            with patch.multiple(store, DATA=root, CONFIG=root/'settings.json', SECRETS=root/'secrets.json'):
                store.init()
                store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
                    VALUES(?,?,?,?,?,?,?,?,?)''', ('vds','VdS',URL,'Katalog','shop.vds.de','0 6 * * *',1,store.now(),store.now()))
                project = {'id':'vds','url':URL}
                first = store.enqueue('vds')
                vds.record_run(project, first, {x['url']:x for x in (removed, retained)})
                self.assertEqual(store.rows('SELECT * FROM changes'), [])
                self.assertIn('Ausgangsstand', store.one('SELECT summary FROM runs WHERE id=?',(first,))['summary'])
                second = store.enqueue('vds')
                current = {x['url']:x for x in (added, {**retained, 'status': ['Zurückgezogen']})}
                vds.record_run(project, second, current)
                summary = store.one('SELECT summary FROM runs WHERE id=?',(second,))['summary']
                self.assertIn('Aus der Kategorie verschwunden', summary)
                self.assertIn('VdS 6023en „Rooftop Photovoltaic Systems“', summary)
                self.assertIn('Als zurückgezogen gekennzeichnet', summary)
                self.assertIn('Neu in der Kategorie', summary)
                proof = {row['url']:json.loads(row['evidence']) for row in store.rows('SELECT * FROM changes')}
                self.assertEqual(proof[removed['url']]['links_removed'], [removed['url']])
                third = store.enqueue('vds')
                vds.record_run(project, third, current)
                self.assertEqual(len(store.rows('SELECT * FROM changes')), 3)
