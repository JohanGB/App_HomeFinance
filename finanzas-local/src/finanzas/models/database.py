"""Acceso a un archivo SQLite de un solo entorno.

No hay conexiones persistentes: cada operación abre y cierra la suya con un
context manager. Así ningún dato de un entorno queda vivo al cambiar a otro y el
archivo se puede reemplazar con seguridad después de una descarga.

La lectura usa ``mode=ro``: el modo de solo lectura lo impone SQLite, no la UI.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Iterator, Protocol

SCHEMA_VERSION = 2
# Cada salto de versión es un archivo SQL. El esquema base (schema.sql) es la v1.
MIGRATIONS = {2: "002_categoria.sql"}
MIN_SQLITE = (3, 24, 0)
BUSY_TIMEOUT_MS = 5_000


class DatabaseError(Exception):
    """Error base de la capa de datos."""


class ReadOnlyError(DatabaseError):
    """Se intentó escribir sin una sesión de edición válida."""


class EnvironmentClosedError(DatabaseError):
    """Se usó un entorno que ya fue cerrado, por ejemplo tras cambiar de .db."""


class SchemaTooNewError(DatabaseError):
    """El archivo fue creado por una versión más reciente de la aplicación."""


class CorruptDatabaseError(DatabaseError):
    """El archivo no pasó las verificaciones de integridad."""


class WriteAuthorization(Protocol):
    """Lo que `Database` exige para escribir. `EditSession` lo implementa."""

    def is_valid_for(self, entorno_id: str) -> bool: ...


def now_local() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _read_schema() -> str:
    return resources.files(__package__).joinpath("schema.sql").read_text("utf-8")


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


class Database:
    """Puerta de entrada a un archivo .db."""

    def __init__(self, path: Path | str) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            required = ".".join(map(str, MIN_SQLITE))
            raise DatabaseError(f"Se requiere SQLite {required} o superior.")
        self.path = Path(path)
        self._closed = False
        self._entorno_id: str | None = None

    # ------------------------------------------------------------------ ciclo
    @classmethod
    def create(cls, path: Path | str, *, nombre: str, moneda: str = "COP") -> "Database":
        """Crea un entorno nuevo. Falla si el archivo ya existe."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(f"{cls._uri(path)}?mode=rwc", uri=True, isolation_level=None)
        try:
            if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                raise DatabaseError(f"Ya existe una base de datos en {path}.")
            # Un solo archivo: sin -wal ni -shm que puedan quedar por fuera de una copia.
            conn.execute("PRAGMA journal_mode = DELETE")
            cls._apply_schema(conn)
            cls._apply_migrations(conn, 1)
            conn.execute(
                "INSERT INTO meta (clave, valor) VALUES "
                "('entorno_id', ?), ('nombre', ?), ('moneda', ?), "
                "('captura_requiere_pin', '0'), ('creado_en', ?)",
                (uuid.uuid4().hex, nombre, moneda, now_local()),
            )
        finally:
            conn.close()
        return cls(path)

    @classmethod
    def open(cls, path: Path | str) -> "Database":
        """Abre un entorno existente y aplica las migraciones pendientes."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        db = cls(path)
        with db.read() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"{path.name} usa el esquema v{version}; esta aplicación llega hasta "
                f"v{SCHEMA_VERSION}. Actualiza la aplicación."
            )
        if version < SCHEMA_VERSION:
            conn = sqlite3.connect(f"{cls._uri(path)}?mode=rw", uri=True, isolation_level=None)
            try:
                cls._apply_migrations(conn, version)
            finally:
                conn.close()
        return db

    def close(self) -> None:
        """Invalida el objeto. Las tareas en curso de este entorno ya no podrán usarlo."""
        self._closed = True
        self._entorno_id = None

    @staticmethod
    def _uri(path: Path) -> str:
        return path.resolve().as_uri()  # file:///C:/... en Windows, con todo escapado

    @staticmethod
    def _apply_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{_read_schema()}\nPRAGMA user_version = 1;\nCOMMIT;"
        )

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection, version: int) -> None:
        """Cada salto va en su propia transacción: si uno falla, el archivo queda en
        la última versión completa y no a medio migrar."""
        if version < 1:
            raise DatabaseError(f"Versión de esquema desconocida: v{version}.")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            script = resources.files(__package__).joinpath(
                "migrations", MIGRATIONS[target]
            ).read_text("utf-8")
            try:
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;"
                )
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise EnvironmentClosedError(f"El entorno {self.path.name} ya fue cerrado.")

    # -------------------------------------------------------------- conexiones
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Conexión que SQLite no deja escribir."""
        self._ensure_open()
        conn = sqlite3.connect(f"{self._uri(self.path)}?mode=ro", uri=True, isolation_level=None)
        try:
            _configure(conn)
            conn.execute("PRAGMA query_only = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._ensure_open()
        conn = sqlite3.connect(f"{self._uri(self.path)}?mode=rw", uri=True, isolation_level=None)
        try:
            _configure(conn)
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def write(self, session: WriteAuthorization) -> Iterator[sqlite3.Connection]:
        """Transacción protegida por una sesión de edición de ESTE entorno."""
        if session is None or not session.is_valid_for(self.entorno_id):
            raise ReadOnlyError("Se requiere desbloquear la edición con el PIN o la contraseña.")
        with self._transaction() as conn:
            yield conn

    @contextmanager
    def capture(self, session: WriteAuthorization | None = None) -> Iterator[sqlite3.Connection]:
        """Transacción para registrar un gasto nuevo.

        Solo exige sesión cuando el entorno tiene `captura_requiere_pin = 1`.
        """
        if self.meta("captura_requiere_pin") == "1":
            with self.write(session) as conn:  # type: ignore[arg-type]
                yield conn
        else:
            with self._transaction() as conn:
                yield conn

    @contextmanager
    def bootstrap_write(self) -> Iterator[sqlite3.Connection]:
        """Solo para definir la primera credencial; no hay PIN contra el cual validar."""
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM credencial").fetchone():
                raise ReadOnlyError("Este entorno ya tiene una credencial.")
            yield conn

    # ---------------------------------------------------------------- metadatos
    def meta(self, clave: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT valor FROM meta WHERE clave = ?", (clave,)).fetchone()
        return row["valor"] if row else None

    @property
    def entorno_id(self) -> str:
        if self._entorno_id is None:
            value = self.meta("entorno_id")
            if not value:
                raise CorruptDatabaseError(f"{self.path.name} no tiene entorno_id.")
            self._entorno_id = value
        return self._entorno_id

    # ------------------------------------------------------------- copia segura
    def snapshot(self, dest: Path | str) -> Path:
        """Copia consistente con la API de backup de SQLite.

        Dos copias de la misma base producen los mismos bytes, de modo que su hash
        sirve para saber si hubo cambios desde la última sincronización.
        """
        dest = Path(dest)
        with self.read() as source:
            target = sqlite3.connect(dest)
            try:
                with target:
                    source.backup(target)
            finally:
                target.close()
        return dest


def verify_file(path: Path | str) -> str:
    """Valida un archivo externo sin migrarlo y devuelve su entorno_id."""
    path = Path(path)
    uri = Database._uri(path)
    try:
        conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - sqlite abre archivos de forma perezosa
        raise CorruptDatabaseError(str(exc)) from exc
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CorruptDatabaseError(f"{path.name}: {integrity}")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise CorruptDatabaseError(f"{path.name}: hay llaves foráneas huérfanas.")
        row = conn.execute("SELECT valor FROM meta WHERE clave = 'entorno_id'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise CorruptDatabaseError(f"{path.name}: {exc}") from exc
    finally:
        conn.close()
    if not row:
        raise CorruptDatabaseError(f"{path.name} no tiene entorno_id.")
    return row[0]
