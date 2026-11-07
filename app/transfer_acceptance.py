"""Export current data read-only; reimport into isolated data/config folders."""
import json,os,sqlite3
from pathlib import Path
import store,transfer,web,engine
from fastapi.testclient import TestClient

assert not store.one("SELECT id FROM runs WHERE status IN ('running','queued')")
root=store.DATA/'.transfer-acceptance'/store.uid();root.mkdir(parents=True,mode=0o700)
original_counts={t:len(store.rows('SELECT * FROM '+t)) for t in ('projects',*transfer.TABLES)}
config=root/'configuration.json';archive=root/'complete.zip'
transfer.export_to(config)
transfer.export_to(archive,history=True)
payload=transfer.read_package(archive)
target=root/'restored';target.mkdir(mode=0o700)
store.DATA=web.DATA=engine.DATA=target
store.CONFIG=web.CONFIG=target/'settings.json'
store.SECRETS=web.SECRETS=target/'credentials.json'
store.init()
result=transfer.import_package(archive,payload)
actual={t:len(store.rows('SELECT * FROM '+t)) for t in original_counts}
assert actual==original_counts,(actual,original_counts)
expected_indexes={r['key']:r['value'] for r in payload.get('collection_indexes',[])}
assert len(expected_indexes)==len(payload.get('collection_indexes',[])),'Duplicate collection-index keys in export'
restored_indexes={r['key']:r['value'] for r in store.rows("SELECT key,value FROM state WHERE key LIKE 'collection_index:%'")}
assert restored_indexes==expected_indexes,'Restored collection indexes differ from the exported key/value records'
for relative,entry in payload['files'].items():assert transfer.file_hash(target/relative)==entry['sha256']
assert not store.rows('SELECT id FROM projects WHERE enabled=1')
assert not store.settings().get('smtp_enabled')
assert not store.credentials()
client=TestClient(web.app)
assert client.get('/').status_code==200
for p in payload['projects']:assert client.get('/projects/'+p['id']).status_code==200
for r in payload['history']['reports']:assert client.get('/reports/'+r['id']).status_code==200
for snap in payload['history']['snapshots']:
 if snap['kind']=='document':assert client.get('/documents/'+snap['id']).status_code==200
again=transfer.import_package(archive,payload)
assert all(n==0 for n in again['added'].values()) and again['files']==0
repeated_indexes={r['key']:r['value'] for r in store.rows("SELECT key,value FROM state WHERE key LIKE 'collection_index:%'")}
assert repeated_indexes==expected_indexes==restored_indexes,'Repeated import changed collection indexes'
with sqlite3.connect(target/'monitor.db') as c:assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
print(json.dumps({'configuration_export':'PASS','archive_export':'PASS','empty_instance_import':'PASS','repeat_without_duplicates':'PASS','source_file_hashes':'PASS','all_project_report_document_views':'PASS','projects_paused_and_smtp_disabled':'PASS','credentials_excluded':'PASS','collection_index_roundtrip':'PASS','collection_index_idempotence':'PASS','collection_indexes':len(expected_indexes),'counts':actual,'files':len(payload['files']),'archive_bytes':archive.stat().st_size,'isolated_directory':str(root)},indent=2))
