"""Shared fixtures. Integration tests need a real MongoDB (MONGO_URI, default mongodb://localhost:27017)
and run in an isolated, throw-away database that is dropped at the end of the session."""
import os
import uuid

import pytest

from dotenv import load_dotenv

load_dotenv()  # read .env first (real environment variables still win), so MONGO_URI from .env is honoured
TEST_DB = f"hrone_test_{uuid.uuid4().hex[:8]}"
os.environ["MONGO_DB"] = TEST_DB  # always the isolated test database, even if .env sets MONGO_DB
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")  # must be set BEFORE app.main is imported

from app import main  # noqa: E402


@pytest.fixture(scope="session")
def mongo():
    try:
        main.client.admin.command("ping")
    except Exception as exc:  # no MongoDB reachable: integration tests are skipped, not failed
        pytest.skip(f"MongoDB not reachable at MONGO_URI ({exc.__class__.__name__})")
    yield main.db
    try:
        main.client.drop_database(TEST_DB)
    except Exception:  # e.g. an Atlas user that may not run dropDatabase: empty the collections instead
        for name in main.db.list_collection_names():
            main.db[name].drop()


@pytest.fixture(scope="session")
def api(mongo):
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:  # entering the context runs startup -> creates indexes
        yield client


@pytest.fixture()
def clean(mongo):
    """Empty both collections before a test (indexes are kept)."""
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})
    yield mongo


    