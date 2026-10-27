"""
Gunicorn configuration for the scholar search API.

    gunicorn -c src/server/gunicorn.conf.py api:app      (./start.sh does this)

Run it from the repo root: `pythonpath` below adds src/server to sys.path so
`api:app` resolves, and the default data paths in api.py are anchored at the
repo root anyway.

Environment:
    HOST      bind address      default 0.0.0.0
    PORT      port              default 8000
    WORKERS   worker processes  default 16
    SCHOLAR_THREADS  request threads per worker, default 40 (read by api.py; 8-40 measured equal)

Each worker is a uvicorn (ASGI) worker running its own copy of the app, i.e.
its own JVM and Lucene searchers (~1.3 GB RSS, ~15 s startup). Requests inside
a worker are handled concurrently: the endpoints run in a thread pool, so one
slow document read does not block the others. Lucene calls release the GIL,
so the per-worker ceiling is the Python work per request (~30 ms: record
parsing and passage extraction). Measured with 100 concurrent clients on a
32-core box after JIT warm-up, /search does 161 req/s (p50 0.58 s) with 8
workers and 225 req/s (p50 0.42 s) with 16, so 16 is the default; each
worker costs ~1.3 GB of RAM.
"""

import multiprocessing
import os

# Directory of api.py, so `api:app` imports from any working directory.
pythonpath = os.path.dirname(os.path.abspath(__file__))

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WORKERS", "16"))
worker_class = "uvicorn_worker.UvicornWorker"

# The JVM must be started inside each worker, never in the master before
# fork, so the app is imported per worker.
preload_app = False

# Worker boot (JVM + index open + warm-up) takes ~15 s; a cold query on the
# 247 GB index can take a couple of seconds. Generous timeouts avoid gunicorn
# killing healthy workers.
timeout = 300
graceful_timeout = 30
keepalive = 5

# Master pid, for `kill -HUP` (reload) / `kill -TERM` (stop); see start.sh.
pidfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gunicorn-{os.environ.get('PORT', '8000')}.pid")

# Logs to stdout/stderr so `./start.sh` (or systemd) captures them.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'

# Never spawn more workers than cores; each one is CPU-heavy at startup.
workers = max(1, min(workers, multiprocessing.cpu_count()))
