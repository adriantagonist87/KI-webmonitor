"""Bounded format classification checks; no document parsing or network calls."""
import io
import unittest
from unittest.mock import patch
import zipfile

from document_inventory import collect_documents
from document_types import DOC_EXT, DOC_MEDIA, EXTENSIONS, MEDIA, HTMLDocumentError, document_suffix


NEW_EXTENSIONS = {'.doc', '.dot', '.xls', '.xlt', '.xlsm', '.xltx', '.xltm', '.ppt',
                  '.pps', '.ppsx', '.rtf', '.odt', '.ods', '.odp', '.zip'}


class DocumentTypesTests(unittest.TestCase):
    def test_01_shared_sets_cover_known_new_formats_and_inert_binary_storage(self):
        self.assertTrue(NEW_EXTENSIONS.issubset(DOC_EXT))
        self.assertEqual(set(DOC_EXT), EXTENSIONS-{'.bin'})
        self.assertEqual(EXTENSIONS, frozenset(MEDIA))
        self.assertNotIn('.bin', DOC_EXT)

    def test_02_every_known_suffix_survives_generic_mime_and_url_case_query_fragment(self):
        for extension in DOC_EXT:
            with self.subTest(extension=extension):
                url='https://files.test/Original'+extension.upper()+'?version=2#download'
                self.assertEqual(document_suffix(url,'application/octet-stream',b'Original file bytes'),extension)
                self.assertEqual(document_suffix(url,'',b'Original file bytes'),extension)

    def test_03_extensionless_endpoints_use_known_mime_without_reading_the_document(self):
        for media, extension in DOC_MEDIA.items():
            with self.subTest(media=media):
                self.assertEqual(document_suffix('https://files.test/download/123',media.upper()+'; charset=binary',
                                                 b'Opaque original bytes'),extension)

    def test_04_ambiguous_office_mime_preserves_the_exact_known_url_suffix(self):
        for extension in ('.doc','.dot','.xls','.xlt','.ppt','.pps'):
            with self.subTest(extension=extension):
                self.assertEqual(document_suffix('https://files.test/template'+extension,MEDIA[extension],b'Opaque'),extension)

    def test_05_mime_aliases_and_existing_pdf_magic_remain_supported(self):
        for mime, extension in [('application/x-zip-compressed','.zip'),('text/rtf','.rtf'),('text/xml','.xml')]:
            self.assertEqual(document_suffix('https://files.test/download',mime,b'Opaque'),extension)
        self.assertEqual(document_suffix('https://files.test/download','application/octet-stream',b'%PDF-1.7\n'),'.pdf')

    def test_06_html_responses_are_never_archived_as_office_or_zip_files(self):
        for extension in NEW_EXTENSIONS:
            for mime, raw in [('text/html',b'An HTML error'),(MEDIA[extension],b'<!doctype html><html>Login</html>')]:
                with self.subTest(extension=extension),self.assertRaises(HTMLDocumentError):
                    document_suffix('https://files.test/file'+extension,mime,raw)

    def test_07_inline_images_scripts_and_unrecognized_suffixes_are_not_automatic_candidates(self):
        urls=['https://files.test/file'+extension for extension in sorted(NEW_EXTENSIONS)]
        urls+=['https://files.test/logo.png','https://files.test/preview.svg','https://files.test/script.js',
               'https://files.test/installer.exe','https://files.test/unknown.bin']
        captures={'page':{'metadata':{'links':[{'url':url,'label':'Download'} for url in urls]}}}
        documents=collect_documents(captures,lambda url:True,DOC_EXT)
        self.assertEqual(set(documents),set(urls[:len(NEW_EXTENSIONS)]))
        self.assertEqual(document_suffix('https://files.test/file.unknown','application/x-unknown',b'Opaque'),'.bin')

    def test_08_archives_and_macro_files_are_classified_without_unpacking_or_execution(self):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:archive.writestr('nested.exe',b'Never executed')
        with patch.object(zipfile,'ZipFile',side_effect=AssertionError('Classifier must never unpack archives')):
            self.assertEqual(document_suffix('https://files.test/files.zip','application/zip',output.getvalue()),'.zip')
            self.assertEqual(document_suffix('https://files.test/book.xlsm',MEDIA['.xlsm'],b'Macro workbook bytes'),'.xlsm')


if __name__=='__main__':unittest.main(verbosity=2)
