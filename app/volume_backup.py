"""Offline volume backup/restore. Stop app and browser before invoking this tool.

Runs without Docker socket/network/root. Archive travels only on stdin/stdout;
restoration validates all members and hashes before writing into empty volumes.
"""
import io
import json
import os
import shutil
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
import tempfile

from backup_archive import (BackupError, FORMAT, MANIFEST, MAX_MEMBERS,
                            TRANSIENT, digest, relative_name, validated_members)

ROOTS=('data','config','secrets','browser')
MAX_INPUT=900*1024**2


def create(base, stream):
    base=Path(base);files=[]
    for name in ROOTS:
        directory=base/name
        if directory.is_symlink() or not directory.is_dir():
            raise BackupError('Datentraeger fehlt oder ist ein Link.')
        for parent, dirs, names in os.walk(directory,followlinks=False):
            if name=='data' and Path(parent)==directory:
                dirs[:]=[d for d in dirs if d not in ('browser','mailpit')]
            for d in dirs:
                if (Path(parent)/d).is_symlink():
                    raise BackupError('Verzeichnislink im Datenbestand.')
            for f in sorted(names):
                if f in TRANSIENT:continue
                p=Path(parent)/f
                logical=relative_name(p.relative_to(base).as_posix())
                if p.is_symlink() or not p.is_file():raise BackupError('Spezialdatei im Datenbestand.')
                files.append((p,logical))
                if len(files)>MAX_MEMBERS-1:raise BackupError('Zu viele Dateien.')
    manifest={}
    with tarfile.open(fileobj=stream,mode='w|gz') as tar:
        for p,name in sorted(files):
            with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW),'rb') as source:
                before=os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):raise BackupError('Quelldatei ersetzt.')
                manifest[name]=digest(source);source.seek(0)
                entry=tarfile.TarInfo(name);entry.size=before.st_size;entry.mode=0o600;entry.mtime=int(before.st_mtime)
                tar.addfile(entry,source)
                after=os.fstat(source.fileno())
                if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
                    raise BackupError('Datei waehrend Sicherung veraendert; App und Browser zuerst stoppen.')
        raw=json.dumps({'format':FORMAT,'files':manifest},sort_keys=True).encode()
        entry=tarfile.TarInfo(MANIFEST);entry.size=len(raw);entry.mode=0o600
        tar.addfile(entry,io.BytesIO(raw))
    return len(manifest)


def restore(base, stream):
    base=Path(base)
    # Never merge into a previous or running installation.
    for name in ROOTS:
        p=base/name
        if p.is_symlink() or not p.is_dir() or any(p.iterdir()):
            raise BackupError('Wiederherstellung nur in vier leere Daten-Volumes erlaubt.')
    with tempfile.TemporaryFile() as temporary:
        total=0
        while block:=stream.read(1024**2):
            total+=len(block)
            if total>MAX_INPUT:raise BackupError('Komprimierte Sicherung ist groesser als 900 MiB.')
            temporary.write(block)
        temporary.seek(0)
        with tarfile.open(fileobj=temporary,mode='r:gz') as tar:
            members,manifest,_=validated_members(tar)
            required={}
            for name,entry in members:
                if name==MANIFEST:continue
                root=PurePosixPath(name).parts[0]
                if root not in ROOTS:
                    raise BackupError('Archiv gehoert nicht zur Volume-Sicherung.')
                directory=base/root;device=directory.stat().st_dev
                previous=required.get(device,(directory,0))
                required[device]=(directory,previous[1]+entry.size)
            if any(shutil.disk_usage(directory).free<size+256*1024**2 for directory,size in required.values()):
                raise BackupError('Nicht genug freier Platz fuer die Wiederherstellung; keine Zieldateien geschrieben.')
            if 'data/monitor.db' not in manifest:
                raise BackupError('Anwendungsdatenbank fehlt im Archiv.')
            for name,entry in members:
                if name==MANIFEST:continue
                target=base/name
                if entry.isdir():target.mkdir(parents=True,exist_ok=True,mode=0o700);continue
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with tar.extractfile(entry) as src,target.open('xb') as dst:
                    while block:=src.read(1024**2):dst.write(block)
                target.chmod(0o600)
    return len(manifest)


if __name__=='__main__':
    os.umask(0o077)
    try:
        if sys.argv[1:] == ['create']:create('/source',sys.stdout.buffer)
        elif sys.argv[1:] == ['restore']:restore('/source',sys.stdin.buffer)
        else:raise BackupError('Aufruf: volume_backup.py create|restore')
    except (BackupError,OSError,tarfile.TarError,ValueError) as error:
        print('Sicherung abgebrochen: '+str(error),file=sys.stderr)
        raise SystemExit(2)
