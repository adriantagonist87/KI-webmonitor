"""Portable mode settings; reuses the isolated transfer fixture only."""
import unittest
import browser_mode
import store
import transfer
import transfer_checks


class ModeTransfer(unittest.TestCase):
    def setUp(self):
        self.fixture=transfer_checks.TransferTests()
        self.fixture.setUp()
        self.project=store.one("SELECT * FROM projects WHERE id='alpha'")
        self.value={'mode':'visual','entry_url':'https://example.org'}

    def tearDown(self):
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def test_roundtrip_keeps_opt_in_and_other_projects_default(self):
        browser_mode.save(self.project,self.value)
        path,payload=self.fixture.package(False)
        self.fixture.select('target')
        transfer.import_package(path,payload)
        self.assertEqual(browser_mode.get(self.project),self.value)
        self.assertEqual(browser_mode.get({**self.project,'id':'other'})['mode'],'standard')

    def test_old_export_keeps_existing_mode(self):
        path,payload=self.fixture.package(False)
        del payload['browser_modes']
        browser_mode.save(self.project,self.value)
        transfer.import_package(path,payload)
        self.assertEqual(browser_mode.get(self.project),self.value)

    def test_invalid_host_rejected_before_changes(self):
        path,payload=self.fixture.package(False)
        payload['browser_modes']['alpha']={'mode':'visual','entry_url':'https://evil.invalid'}
        with self.assertRaises(transfer.Invalid):transfer.import_package(path,payload)
        self.assertEqual(browser_mode.get(self.project)['mode'],'standard')


if __name__=='__main__':unittest.main()
