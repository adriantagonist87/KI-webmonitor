"""Bounded source-date metadata checks; no network, inference or production data.

Run in the application environment: python -X utf8 -m document_metadata_checks
Real PDF metadata is read through pypdf, including public empty-password files.
"""
from datetime import datetime, timezone
import copy
import io
import json
import unittest

from pypdf import PdfWriter

from document_metadata import extract_document_metadata, compact_document_metadata


ABSOLUTE_FIELDS = ('iso8601', 'utc', 'europe_berlin')
HIDDEN = 'NEVER_EXPORT_THIS_SOURCE_VALUE'


def pdf(metadata=None, password=None):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    if metadata:
        writer.add_metadata(metadata)
    if password is not None:
        writer.encrypt(password, owner_password='fixture-owner-only')
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class DocumentMetadata(unittest.TestCase):
    def extract(self, date=None, headers=None, creation=None):
        values = {}
        if date is not None:
            values['/ModDate'] = date
        if creation is not None:
            values['/CreationDate'] = creation
        return extract_document_metadata(pdf(values), '.pdf', headers or {})

    def no_absolute(self, value):
        for key in ABSOLUTE_FIELDS:
            self.assertNotIn(key, value)

    def test_01_pdf_summer_offset_keeps_exact_instant_and_local_time(self):
        value = self.extract("D:20260615143439+02'00'")['pdf']['modification_date']
        self.assertEqual(value['raw'], "D:20260615143439+02'00'")
        self.assertEqual(value['status'], 'exact')
        self.assertEqual(value['precision'], 'second')
        self.assertTrue(value['timezone_explicit'])
        self.assertEqual(instant(value['utc']), datetime(2026, 6, 15, 12, 34, 39, tzinfo=timezone.utc))
        self.assertEqual(instant(value['europe_berlin']).isoformat(), '2026-06-15T14:34:39+02:00')
        self.assertEqual(instant(value['iso8601']), instant(value['utc']))

    def test_02_pdf_winter_uses_berlin_winter_offset(self):
        value = self.extract('D:20260115133439Z')['pdf']['modification_date']
        self.assertEqual(value['status'], 'exact')
        self.assertEqual(instant(value['europe_berlin']).isoformat(), '2026-01-15T14:34:39+01:00')

    def test_03_pdf_numeric_offset_without_apostrophes(self):
        value = self.extract('D:20260615133439+0530')['pdf']['modification_date']
        self.assertEqual(value['status'], 'exact')
        self.assertEqual(instant(value['utc']).isoformat(), '2026-06-15T08:04:39+00:00')

    def test_04_pdf_offset_can_change_the_utc_calendar_date(self):
        value = self.extract("D:20260101001500+02'00'")['pdf']['modification_date']
        self.assertEqual(value['status'], 'exact')
        self.assertEqual(instant(value['utc']).isoformat(), '2025-12-31T22:15:00+00:00')

    def test_05_full_naive_pdf_date_has_no_claimed_absolute_instant(self):
        value = self.extract('D:20260615133439')['pdf']['modification_date']
        self.assertEqual(value['status'], 'timezone_missing')
        self.assertEqual(value['precision'], 'second')
        self.assertFalse(value['timezone_explicit'])
        self.no_absolute(value)

    def test_06_partial_pdf_dates_preserve_precision_without_filling_fields(self):
        for raw, precision in [('D:2026', 'year'), ('D:202606', 'month'), ('D:20260615', 'day'),
                               ('D:2026061513', 'hour'), ('D:202606151334', 'minute')]:
            with self.subTest(raw=raw):
                value = self.extract(raw)['pdf']['modification_date']
                self.assertEqual(value['raw'], raw)
                self.assertEqual(value['status'], 'partial')
                self.assertEqual(value['precision'], precision)
                self.no_absolute(value)

    def test_07_partial_date_with_explicit_zone_is_still_not_an_instant(self):
        value = self.extract('D:20260615Z')['pdf']['modification_date']
        self.assertEqual(value['status'], 'partial')
        self.no_absolute(value)

    def test_08_invalid_pdf_calendar_and_offsets_are_warnings(self):
        for raw in ['D:20260230133439Z', 'D:20261315133439Z', "D:20260615133439+24'00'", 'not-a-date']:
            with self.subTest(raw=raw):
                result = self.extract(raw)
                value = result['pdf']['modification_date']
                self.assertEqual(value['status'], 'invalid')
                self.no_absolute(value)
                self.assertTrue(result['warnings'])

    def test_09_creation_and_modification_dates_remain_separate(self):
        result = self.extract('D:20260615133439Z', creation='D:20200102030405Z')
        self.assertEqual(instant(result['pdf']['creation_date']['utc']).year, 2020)
        self.assertEqual(instant(result['pdf']['modification_date']['utc']).year, 2026)

    def test_10_missing_pdf_and_http_dates_are_explicitly_missing(self):
        result = self.extract()
        for value in [result['pdf']['creation_date'], result['pdf']['modification_date'], result['http']['last_modified']]:
            self.assertEqual(value['status'], 'missing')
            self.assertIsNone(value['raw'])
            self.no_absolute(value)

    def test_11_http_numeric_offset_is_a_distinct_server_timestamp(self):
        result = self.extract("D:20260615143439+02'00'", {'Last-Modified':'Mon, 15 Jun 2026 15:12:38 +0200'})
        value = result['http']['last_modified']
        self.assertEqual(value['status'], 'exact')
        self.assertEqual(value['precision'], 'second')
        self.assertTrue(value['timezone_explicit'])
        self.assertEqual(instant(value['utc']).isoformat(), '2026-06-15T13:12:38+00:00')
        self.assertEqual(instant(value['europe_berlin']).isoformat(), '2026-06-15T15:12:38+02:00')
        self.assertNotEqual(value['utc'], result['pdf']['modification_date']['utc'])

    def test_12_http_headers_are_case_insensitive_but_whitelisted(self):
        result = self.extract(headers={'lAsT-mOdIfIeD':'Mon, 15 Jun 2026 11:34:39 GMT',
                                       'eTaG':'W/"fixture-v1"', 'CONTENT-TYPE':'application/pdf', 'content-length':'123',
                                       'Authorization':HIDDEN, 'Cookie':HIDDEN, 'Set-Cookie':HIDDEN,
                                       'X-Api-Key':HIDDEN, 'Location':HIDDEN})
        self.assertEqual(set(result['http']), {'last_modified','etag','content_type','content_length'})
        self.assertEqual(result['http']['etag'], 'W/"fixture-v1"')
        self.assertEqual(result['http']['content_type'], 'application/pdf')
        self.assertEqual(result['http']['content_length'], 123)
        self.assertEqual(result['http']['last_modified']['status'], 'exact')
        self.assertNotIn(HIDDEN, json.dumps(result))

    def test_13_http_full_naive_timestamp_has_no_implicit_gmt(self):
        value = self.extract(headers={'Last-Modified':'Mon, 15 Jun 2026 11:34:39'})['http']['last_modified']
        self.assertEqual(value['status'], 'timezone_missing')
        self.assertFalse(value['timezone_explicit'])
        self.no_absolute(value)

    def test_14_http_partial_timestamp_is_not_completed_with_zero_seconds(self):
        value = self.extract(headers={'Last-Modified':'Mon, 15 Jun 2026 11:34 GMT'})['http']['last_modified']
        self.assertEqual(value['status'], 'partial')
        self.no_absolute(value)

    def test_15_http_unknown_named_zone_is_not_guessed(self):
        result = self.extract(headers={'Last-Modified':'Mon, 15 Jun 2026 13:34:39 CET'})
        value = result['http']['last_modified']
        self.assertIn(value['status'], ('unsupported_timezone', 'invalid', 'timezone_missing'))
        self.no_absolute(value)
        self.assertTrue(result['warnings'])

    def test_16_invalid_http_date_is_fail_soft(self):
        result = self.extract(headers={'Last-Modified':'not-a-date'})
        self.assertEqual(result['http']['last_modified']['status'], 'invalid')
        self.no_absolute(result['http']['last_modified'])
        self.assertTrue(result['warnings'])

    def test_17_content_length_is_only_nonnegative_ascii_integer(self):
        for raw in ['-1', '1.5', '١٢٣', '1' * 21]:
            with self.subTest(raw=raw):
                result = self.extract(headers={'Content-Length':raw})
                self.assertIsNone(result['http']['content_length'])
                self.assertTrue(result['warnings'])
        self.assertEqual(self.extract(headers={'Content-Length':'0'})['http']['content_length'], 0)

    def test_18_source_pdf_title_and_author_are_not_collected(self):
        raw = pdf({'/Title':HIDDEN, '/Author':HIDDEN, '/Subject':HIDDEN, '/Keywords':HIDDEN,
                   '/ModDate':'D:20260615133439Z'})
        result = extract_document_metadata(raw, '.pdf', {})
        self.assertEqual(set(result['pdf']), {'creation_date','modification_date'})
        self.assertNotIn(HIDDEN, json.dumps(result))

    def test_19_metadata_and_warning_sizes_are_bounded(self):
        result = self.extract('D:' + '9' * 2000, {'Last-Modified':'X' * 2000,
                              'ETag':'E' * 2000, 'Content-Type':'T' * 2000, 'Content-Length':'9' * 2000})
        for value in [result['pdf']['modification_date'], result['http']['last_modified']]:
            self.assertEqual(value['status'], 'too_long')
            self.assertLessEqual(len(value['raw']), 96)
            self.no_absolute(value)
        for key, maximum in [('etag',256),('content_type',160)]:
            if result['http'][key] is not None:
                self.assertLessEqual(len(result['http'][key]), maximum)
        self.assertLessEqual(len(result['warnings']), 8)
        self.assertTrue(all(isinstance(w, str) and len(w) <= 160 for w in result['warnings']))

    def test_20_control_characters_do_not_smuggle_unapproved_headers(self):
        result = self.extract(headers={'ETag':'ok\r\nSet-Cookie: ' + HIDDEN,
                                       'Content-Type':'application/pdf\x00' + HIDDEN})
        self.assertNotIn(HIDDEN, json.dumps(result))
        self.assertTrue(result['warnings'])

    def test_21_malformed_pdf_keeps_safe_http_metadata_and_does_not_raise(self):
        result = extract_document_metadata(b'%PDF-1.7\nnot-a-readable-pdf', '.pdf',
                                          {'Last-Modified':'Mon, 15 Jun 2026 11:34:39 GMT', 'Content-Type':'application/pdf'})
        self.assertEqual(result['version'], 1)
        self.assertEqual(result['http']['last_modified']['status'], 'exact')
        self.assertTrue(result['warnings'])

    def test_22_non_pdf_keeps_http_metadata_without_claiming_pdf_dates(self):
        result = extract_document_metadata(b'not a PDF original', '.txt', {'ETag':'"fixture"'})
        self.assertEqual(result['pdf'], {})
        self.assertEqual(result['http']['etag'], '"fixture"')

    def test_23_public_empty_password_pdf_metadata_can_be_read(self):
        raw = pdf({'/ModDate':'D:20260615133439Z'}, password='')
        result = extract_document_metadata(raw, '.pdf', {})
        self.assertEqual(result['pdf']['modification_date']['status'], 'exact')

    def test_24_nonempty_reading_password_is_not_guessed(self):
        raw = pdf({'/ModDate':'D:20260615133439Z'}, password='private-fixture-password')
        result = extract_document_metadata(raw, '.pdf', {})
        self.assertNotEqual(result['pdf']['modification_date']['status'], 'exact')
        self.assertTrue(result['warnings'])
        self.assertNotIn('private-fixture-password', json.dumps(result))

    def test_25_compact_metadata_preserves_dates_and_excludes_unknown_fields(self):
        result = self.extract("D:20260615133439+02'00'", {'Last-Modified':'Mon, 15 Jun 2026 13:12:38 GMT', 'ETag':'"v1"'})
        contaminated = copy.deepcopy(result)
        contaminated['authorization'] = HIDDEN
        contaminated['http']['set_cookie'] = HIDDEN
        contaminated['pdf']['password'] = HIDDEN
        contaminated['pdf']['modification_date']['api_key'] = HIDDEN
        compact = compact_document_metadata(contaminated)
        self.assertNotIn(HIDDEN, json.dumps(compact))
        self.assertEqual(compact['version'], 1)
        self.assertEqual(compact['http']['etag'], '"v1"')
        for section, key in [('pdf','modification_date'),('http','last_modified')]:
            for field in ('raw','status','precision','timezone_explicit',*ABSOLUTE_FIELDS):
                self.assertEqual(compact[section][key][field], result[section][key][field])
        self.assertIn('authorization', contaminated)  # Compaction must not edit canonical evidence.


if __name__ == '__main__':
    unittest.main(verbosity=2)
