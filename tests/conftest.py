# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
pytest configuration and shared fixtures for SISTUR test suite.

Test isolation strategy
-----------------------
- All tests run against an **in-memory SQLite** database (TestingConfig).
- A fresh schema is created before every test function and torn down after.
- No production or development database is ever touched.
- Flask request context is available via the `client` fixture.
"""

import pytest

from app import create_app
from app.extensions import db as _db


# ---------------------------------------------------------------------------
# Application fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def app():
    """
    Create one Flask application per test *session*.

    The app is configured with TestingConfig (SQLite in-memory).
    Individual tests get isolated schemas via the `db` fixture below.
    """
    application = create_app("testing")
    with application.app_context():
        yield application


# ---------------------------------------------------------------------------
# Database fixture — fresh schema per test function
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def db(app):
    """
    Create all tables before each test and drop them after.

    Marked `autouse=True` so every test gets an isolated database
    without needing to request this fixture explicitly.
    """
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


# ---------------------------------------------------------------------------
# HTTP client fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def client(app):
    """Flask test client with request context support."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Authenticated client fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def authenticated_client(app, client, db):
    """
    Test client with an active session for funcionario_id=1.

    Use this for routes protected by @login_required.
    Assumes a Funcionario with id=1 has been created by the test.
    """
    with client.session_transaction() as sess:
        sess["funcionario_id"] = 1
    return client
