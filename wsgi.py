# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

# IMPORTANTE: eventlet monkey_patch deve ser executado antes de qualquer
# outro import. Necessário para Flask-SocketIO com gunicorn -k eventlet.
import os
import sys

# Only apply eventlet.monkey_patch() when running as a WSGI server (gunicorn).
# Skip it when running Flask CLI commands (flask db, flask setup, etc.) to avoid
# "Working outside of application context" errors from eventlet's patcher
# interfering with werkzeug.local.LocalProxy objects used by Flask-Migrate/Alembic.
#
# Detection:
#   - gunicorn: argv[0] contains "gunicorn" or program name is "gunicorn"
#   - flask cli: FLASK_APP or argv contains "flask" or WERKZEUG_RUN_MAIN is set
if "gunicorn" in sys.argv[0] or "gunicorn" in sys.modules:
    import eventlet
    eventlet.monkey_patch()

from app import create_app  # noqa: E402

app = create_app("production")
