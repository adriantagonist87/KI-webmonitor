import os
os.umask(0o077)
import uvicorn

role = os.environ.get('ROLE', 'app')
if role == 'gateway':
    from gateway import app
elif role == 'fixture':
    from fixture import app
else:
    from web import app

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000, access_log=False, log_level='warning')
