import pytest
from tinydb import TinyDB, where
from tinydb.storages import MemoryStorage

import db.db_tinydb as db
from .config import generate_requests


@pytest.fixture(autouse=True)
def init_db():
    """Give every test a fresh, isolated TinyDB.

    Previously each test file declared its own autouse fixture that called
    ``db.clinic.drop_tables()`` on the shared file-backed TinyDB. That left
    stale rows behind across test files in a single pytest session (see
    CHANGES.md for the full write-up), causing the second test to run to see
    data written by the first.

    Instead we rebind the module-level ``db.clinic`` / ``db.patient_requests``
    / ``db.tasks`` globals to a fresh in-memory TinyDB per test. All services
    access these via ``import db.db_tinydb as db`` + attribute lookup, so they
    pick up the rebinding naturally.
    """
    db.clinic = TinyDB(storage=MemoryStorage)
    db.patient_requests = db.clinic.table('PatientRequest')
    db.tasks = db.clinic.table('Tasks')

    if generate_requests:
        db.init_db()

        closed_pat_requests = db.patient_requests.search(
            where('status') == "Closed")
        assert len(closed_pat_requests) == 100, "Db init with 100 closed worked"

        open_pat_requests = db.patient_requests.search(
            where('status') == "Open")
        assert len(
            open_pat_requests) == 0, "DB has open request before processing"
