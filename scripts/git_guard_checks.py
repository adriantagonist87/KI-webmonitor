#!/usr/bin/env python3
"""Isolierte Regressionstests fuer den Git-Index-Guard; keine Produktivdaten."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("kiwm_git_guard", Path(__file__).with_name("git_guard.py"))
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


class GitGuardChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiwm-git-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_git("init", "-q")

    def run_git(self, *args, data=None):
        result = subprocess.run(
            ["git", "-C", str(self.root), *args], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return result.stdout

    def stage(self, path, content=b"safe source\n", mode="100644"):
        oid = self.run_git("hash-object", "-w", "--stdin", data=content).strip().decode("ascii")
        self.run_git("update-index", "--add", "--cacheinfo", mode, oid, path)

    def reasons(self):
        return [finding.reason for finding in GUARD.audit_index(self.root)[1]]

    def test_source_and_templates_pass(self):
        for path in ("app/main.py", "config/nginx.conf", "config/nginx.conf.template", ".env.example", "config/settings.example.json", "scripts/known_hosts.example"):
            self.stage(path)
        self.assertEqual(GUARD.audit_index(self.root), (6, []))

    def test_force_added_private_files_are_blocked(self):
        for path in ("data/monitor.db", "secrets/credentials.json", "config/settings.json", ".env", ".env.production", "scripts/known_hosts", "research/runtime.txt", "tests/version.txt", "backups/copy.tar.gz", "app/__pycache__/main.pyc"):
            with self.subTest(path=path):
                self.assertIsNotNone(GUARD.path_issue(path))
                self.stage(path)
        self.assertGreaterEqual(len(self.reasons()), 9)

    def test_checks_staged_blob_instead_of_changed_working_file(self):
        secret = b"omlx-" + b"canaryABC123456789"
        self.stage("app/settings.py", secret)
        (self.root / "app").mkdir()
        (self.root / "app/settings.py").write_text("already removed from working tree\n")
        self.assertTrue(any("oMLX" in reason for reason in self.reasons()))

    def test_diagnostics_never_print_detected_secret(self):
        secret = "omlx-" + "anotherCanary123456789"
        self.stage("safe.py", secret.encode())
        output = io.StringIO()
        with redirect_stderr(output), redirect_stdout(output):
            code = GUARD.main(["--repo", str(self.root)])
        self.assertEqual(code, 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("safe.py", output.getvalue())

    def test_private_key_and_url_credentials_are_blocked(self):
        self.stage("private.txt", b"-----BEGIN " + b"PRIVATE KEY-----\ncanary\n")
        self.stage("url.txt", b"https://" + b"example-user:example-password@example.invalid/")
        reasons = self.reasons()
        self.assertTrue(any("privater Schluessel" in reason for reason in reasons))
        self.assertTrue(any("Zugangsdaten in URL" in reason for reason in reasons))

    def test_only_reviewed_literal_url_placeholders_are_allowed(self):
        self.stage('app/selftest.py', b'https://' + b'user:password@example.org/private')
        self.stage('app/test_checks.py', b'https://' + b'u:p@shop.vds.de/graphql')
        self.assertEqual(self.reasons(), [])
        self.stage('app/another_checks.py', b'https://' + b'user:actualPassword@example.org/private')
        self.stage('app/yet_another_checks.py', b'https://' + b'u:p@real.example.com/path')
        self.assertEqual(sum('Zugangsdaten in URL' in reason for reason in self.reasons()), 2)

    def test_local_browser_url_placeholder_requires_exact_password_and_port(self):
        self.stage('app/usability_checks.py', b'http://' + b'name:password@localhost:8766')
        self.assertEqual(self.reasons(), [])
        self.stage('app/changed_password_checks.py', b'http://' + b'name:actualPassword@localhost:8766')
        self.stage('app/changed_port_checks.py', b'http://' + b'name:password@localhost:8767')
        self.stage('app/missing_port_checks.py', b'http://' + b'name:password@localhost')
        self.assertEqual(sum('Zugangsdaten in URL' in reason for reason in self.reasons()), 3)

    def test_symlink_and_submodule_are_blocked(self):
        self.stage("linked.py", b"../outside", mode="120000")
        # Fuer den Guard ist ein Gitlink kein normaler Blob; keine Fremdrepos noetig.
        oid = "1" * 40
        self.run_git("update-index", "--add", "--cacheinfo", "160000", oid, "external")
        self.assertEqual(sum("Symlinks" in reason for reason in self.reasons()), 2)

    def test_unmerged_entry_is_blocked(self):
        oid = self.run_git("hash-object", "-w", "--stdin", data=b"conflict").strip()
        self.run_git("update-index", "--index-info", data=b"100644 " + oid + b" 1\tconflict.py\n")
        self.assertIn("nicht aufgeloester Merge-Konflikt", self.reasons())

    def test_large_blob_is_blocked(self):
        self.stage("huge.txt", b"a" * (GUARD.MAX_BLOB_BYTES + 1))
        self.assertIn("Datei groesser als 8 MiB", self.reasons())

    def test_binary_source_blocked_but_icon_allowed(self):
        self.stage("app/source.py", b"a\0b")
        self.stage("app/static/favicon.ico", b"\0\0icon")
        findings = GUARD.audit_index(self.root)[1]
        self.assertEqual([(item.path, item.reason) for item in findings], [("app/source.py", "unerwartete Binaerdatei")])

    def test_no_repository_reports_clean_error(self):
        outside = self.root / "outside"
        outside.mkdir()
        # Verzeichnis ausserhalb des Test-Repositories verwenden.
        with tempfile.TemporaryDirectory(prefix="kiwm-no-git-") as temp:
            output = io.StringIO()
            with redirect_stderr(output):
                result = GUARD.main(["--repo", temp])
            self.assertEqual(result, 2)
            self.assertIn("nicht ausgefuehrt", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
