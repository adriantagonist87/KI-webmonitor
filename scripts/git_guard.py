#!/usr/bin/env python3
"""Prueft den vollstaendigen Git-Index, ohne gefundene Geheimnisse auszugeben."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
PRIVATE_ROOTS = {
    "data", "secrets", "backups", "research", "work", "outputs", "logs", "cache", "tests",
    "browser-profile", "browser-profiles", "downloads", "screenshots",
}
BINARY_ASSETS = {".png", ".ico", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf"}
PRIVATE_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jwt", ".db", ".sqlite", ".sqlite3",
    ".zip", ".tar", ".tar.gz", ".tgz", ".7z", ".log", ".pyc", ".pyo",
)
TOKEN_PATTERNS = (
    ("privater Schluessel", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("oMLX-Token", re.compile(rb"\bomlx-[A-Za-z0-9]{8,}\b")),
    ("API-Token", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub-Token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("AWS-Zugriffsschluessel", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)
URL_CREDENTIALS = re.compile(rb"https?://([^\s/:<>\"']+):([^\s/@<>\"']+)@([A-Za-z0-9.-]+)(?::([0-9]+))?", re.I)
# Ausschliesslich diese bereits ueberprueften, oeffentlichen Negativtestwerte.
# Kein Dateiausschluss: Andere Zugangsdaten in denselben Tests bleiben gesperrt.
PUBLIC_URL_PLACEHOLDERS = {
    (b'user', b'password', b'example.org'), (b'u', b'p', b'example.org'),
    (b'user', b'secret', b'sources.test'), (b'u', b'p', b'actions.test'),
    (b'u', b'p', b'shop.vds.de'),
}
PUBLIC_URL_PORT_PLACEHOLDERS = {(b'name', b'password', b'localhost', b'8766')}


@dataclass(frozen=True)
class Finding:
    path: str
    reason: str


class GuardError(Exception):
    pass


def git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError as exc:
        raise GuardError("Git konnte nicht gestartet werden.") from exc
    if result.returncode:
        # Git-Ausgaben koennen Pfade oder Konfigurationswerte enthalten.
        raise GuardError("Git-Abfrage fehlgeschlagen; Repository und Index pruefen.")
    return result.stdout


def path_issue(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    low_parts = tuple(part.casefold() for part in parts)
    name = low_parts[-1] if low_parts else ""
    if not parts or path.startswith(("/", "\\")) or "\\" in path or ":" in path:
        return "ungueltiger Repository-Pfad"
    if any(ord(char) < 32 or ord(char) == 127 for char in path) or ".." in parts:
        return "ungueltiger Repository-Pfad"
    if low_parts[0] in PRIVATE_ROOTS:
        return "privates Laufzeitverzeichnis"
    if any(part in {"__pycache__", ".venv", "venv", "node_modules"} for part in low_parts):
        return "generierte Laufzeitdatei"
    if name == "backup-manifest.json" or name in {"credentials.json", "known_hosts", "known_hosts.old"}:
        return "installationsbezogene oder private Datei"
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "private Umgebungsdatei; nur .env.example ist erlaubt"
    if name in {"compose.override.yaml", "compose.override.yml"}:
        return "lokale Compose-Konfiguration"
    if name.startswith(("id_rsa", "id_ed25519")) or name.endswith(PRIVATE_SUFFIXES):
        return "Schluessel, Archiv, Datenbank oder generierte Datei"
    if re.search(r"\.(?:db|sqlite3?)-(?:wal|shm|journal)$", name):
        return "Datenbank-Begleitdatei"
    if low_parts[0] == "config":
        is_template = ".example" in name or (len(low_parts) > 1 and low_parts[1] == "templates")
        if path.casefold() not in {"config/nginx.conf", "config/nginx.conf.template"} and not is_template:
            return "private Konfiguration; nur nginx.conf und Vorlagen gehoeren in Git"
    return None


def audit_index(repo: Path) -> tuple[int, list[Finding]]:
    git(repo, "rev-parse", "--show-toplevel")
    raw_entries = git(repo, "ls-files", "--stage", "-z")
    findings: list[Finding] = []
    entries: list[tuple[str, str]] = []
    for raw_entry in raw_entries.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8", "surrogateescape")
        except (ValueError, UnicodeError) as exc:
            raise GuardError("Git-Index konnte nicht sicher gelesen werden.") from exc
        if stage != "0":
            findings.append(Finding(path, "nicht aufgeloester Merge-Konflikt"))
            continue
        if mode not in {"100644", "100755"}:
            findings.append(Finding(path, "Symlinks und Submodule sind nicht zugelassen"))
            continue
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", oid):
            raise GuardError("Ungueltige Objektkennung im Git-Index.")
        issue = path_issue(path)
        if issue:
            findings.append(Finding(path, issue))
        entries.append((path, oid))

    if not entries:
        return 0, findings
    unique_oids = list(dict.fromkeys(oid for _, oid in entries))
    request = ("\n".join(unique_oids) + "\n").encode("ascii")
    checked = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)", data=request)
    sizes: dict[str, int] = {}
    for line in checked.splitlines():
        try:
            oid, kind, size = line.decode("ascii").split(" ")
            if kind != "blob":
                raise ValueError("expected blob")
            sizes[oid] = int(size)
        except (ValueError, UnicodeError) as exc:
            raise GuardError("Git-Objekt konnte nicht sicher gelesen werden.") from exc
    if set(sizes) != set(unique_oids) or any(size < 0 for size in sizes.values()):
        raise GuardError("Unvollstaendige Git-Objektliste.")
    if sum(size for size in sizes.values() if size <= MAX_BLOB_BYTES) > MAX_SCAN_BYTES:
        raise GuardError("Quellbestand groesser als 64 MiB; grosse Dateien vor dem Commit gesondert pruefen.")
    small_oids = [oid for oid in unique_oids if sizes[oid] <= MAX_BLOB_BYTES]
    blobs: dict[str, bytes] = {}
    if small_oids:
        blob_request = ("\n".join(small_oids) + "\n").encode("ascii")
        stream = git(repo, "cat-file", "--batch", data=blob_request)
        cursor = 0
        for oid in small_oids:
            end = stream.find(b"\n", cursor)
            expected = f"{oid} blob {sizes[oid]}".encode("ascii")
            if end < 0 or stream[cursor:end] != expected:
                raise GuardError("Git-Objektinhalt unvollstaendig.")
            cursor = end + 1
            finish = cursor + sizes[oid]
            if finish >= len(stream) or stream[finish:finish + 1] != b"\n":
                raise GuardError("Git-Objektinhalt unvollstaendig.")
            blobs[oid] = stream[cursor:finish]
            cursor = finish + 1
        if cursor != len(stream):
            raise GuardError("Unerwartete Daten nach Git-Objektinhalt.")
    for path, oid in entries:
        if sizes[oid] > MAX_BLOB_BYTES:
            findings.append(Finding(path, "Datei groesser als 8 MiB"))
            continue
        content = blobs[oid]
        for label, pattern in TOKEN_PATTERNS:
            if pattern.search(content):
                findings.append(Finding(path, f"verdaechtiger Inhalt: {label}"))
        if any(not ((match.groups()[:3] in PUBLIC_URL_PLACEHOLDERS and match[4] is None)
                    or match.groups() in PUBLIC_URL_PORT_PLACEHOLDERS)
               for match in URL_CREDENTIALS.finditer(content)):
            findings.append(Finding(path, 'verdaechtiger Inhalt: Zugangsdaten in URL'))
        if b"\0" in content and PurePosixPath(path).suffix.casefold() not in BINARY_ASSETS:
            findings.append(Finding(path, "unerwartete Binaerdatei"))
    return len(entries), findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository-Verzeichnis")
    args = parser.parse_args(argv)
    try:
        count, findings = audit_index(args.repo)
    except GuardError as exc:
        print(f"Git-Pruefung nicht ausgefuehrt: {exc}", file=sys.stderr)
        return 2
    if findings:
        print("Commit angehalten. Fundstellen ohne Offenlegung gefundener Werte:", file=sys.stderr)
        for finding in findings:
            print(f"- {json.dumps(finding.path, ensure_ascii=True)}: {finding.reason}", file=sys.stderr)
        return 1
    print(f"Git-Index geprueft: {count} Dateien, keine Treffer der eingebauten Regeln.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
