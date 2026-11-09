"""Run with .venv/bin/python run.py; private loopback only."""
import uvicorn

if __name__ == '__main__':
    uvicorn.run('app.server:app', host='127.0.0.1', port=8765, access_log=False)
