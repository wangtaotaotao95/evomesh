import os
import uuid

import pytest

from evomesh.store import Store


@pytest.fixture
def store(tmp_path):
    url = os.getenv("EVOMESH_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'ledger.db'}")
    db = Store(url)
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def project():
    return "test-" + uuid.uuid4().hex
