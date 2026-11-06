import pytest

from finanzas.models.database import Database
from finanzas.models.repository import Repository
from finanzas.models.security import SecurityManager


@pytest.fixture
def db(tmp_path):
    return Database.create(tmp_path / "gastos.db", nombre="Prueba")


@pytest.fixture
def repo(db):
    return Repository(db)


@pytest.fixture
def security(db):
    # Costo mínimo de bcrypt para que la suite sea rápida.
    manager = SecurityManager(db, rounds=4)
    manager.set_initial_credential("4827")
    return manager


@pytest.fixture
def session(security):
    return security.unlock("4827")
