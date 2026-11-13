"""Sincronización selectiva: al abrir, al cambiar de entorno y al cerrar.

Sincroniza el archivo completo. Compara dos hechos desde la última vez que este
equipo y Drive coincidieron:

    ¿cambió lo local?   huella del snapshot   != base_fingerprint
    ¿cambió Drive?      headRevisionId        != base_revision

    local  Drive   acción
    no     no      nada
    sí     no      subir
    no     sí      bajar
    sí     sí      conflicto: no se pisa nada y decide el usuario

Un archivo SQLite no se puede fusionar byte a byte, así que un conflicto nunca se
resuelve solo. La aplicación tampoco escribe sobre `entornos/<id>.db` hasta que el
archivo nuevo esté descargado, verificado y listo para un reemplazo atómico.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Iterator

from platformdirs import user_data_path

from ..models.database import Database, verify_file
from ..models.repository import Repository
from ..models.security import SecurityManager
from .drive_gateway import (
    ENV_PROPERTY,
    ChecksumError,
    DriveAccessError,
    DriveError,
    DriveGateway,
    OfflineError,
    RemoteFile,
    RemoteNotFoundError,
    md5_of,
)
from .local_state import LocalState, SyncEntry


# Fallas que no dañan nada: los datos quedan locales y se reintenta después.
_RETRY_LATER = (OfflineError, DriveAccessError)


class SyncStatus(Enum):
    UP_TO_DATE = auto()
    UPLOADED = auto()
    DOWNLOADED = auto()
    CONFLICT = auto()
    OFFLINE = auto()


class Resolution(Enum):
    KEEP_LOCAL = auto()      # Drive queda con la versión de este equipo
    KEEP_REMOTE = auto()     # este equipo adopta Drive; lo local queda en .bak
    KEEP_BOTH = auto()       # lo local se sube como un entorno aparte y se adopta Drive


class EnvironmentMismatchError(DriveError):
    """El archivo local y el de Drive pertenecen a entornos distintos."""


@dataclass(frozen=True)
class SyncResult:
    status: SyncStatus
    detail: str = ""


@dataclass
class ActiveEnvironment:
    """Todo lo que pertenece a un entorno abierto.

    La UI debe guardar cualquier dato cargado aquí y comparar `generation` antes de
    pintar el resultado de una tarea de fondo. Al cambiar de entorno este objeto se
    descarta entero y `db.close()` hace fallar las consultas tardías.
    """

    generation: int
    file_id: str
    nombre: str
    db: Database
    repo: Repository
    security: SecurityManager
    sync: SyncResult


def default_data_dir() -> Path:
    # Carpeta interna con el nombre anterior: contiene las copias locales y el estado.
    return user_data_path("FinanzasLocal", appauthor=False)


class SyncEngine:
    def __init__(self, gateway: DriveGateway, data_dir: Path | None = None) -> None:
        self.gateway = gateway
        self.data_dir = Path(data_dir or default_data_dir())
        self.env_dir = self.data_dir / "entornos"
        self.temp_dir = self.data_dir / "tmp"
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.state = LocalState(self.data_dir / "estado.json")
        self.active: ActiveEnvironment | None = None
        self._generation = 0
        self._folder_ready = False

    # ---------------------------------------------------------------- catálogo
    def folder(self) -> str:
        # Se consulta una vez por ejecución (no solo la primera vez en el equipo) para
        # que el cambio de nombre de la carpeta llegue también a equipos ya configurados.
        if not self._folder_ready:
            self.state.folder_id = self.gateway.ensure_app_folder()
            self._folder_ready = True
        return self.state.folder_id

    def list_environments(self) -> list[RemoteFile]:
        """Contenido del dropdown. Pide metadatos, nunca contenido."""
        return self.gateway.list_databases(self.folder())

    def local_path(self, file_id: str) -> Path:
        # Por id y no por nombre: dos .db con el mismo nombre no pueden pisarse.
        return self.env_dir / f"{file_id}.db"

    def create_environment(self, nombre: str, *, moneda: str = "COP") -> RemoteFile:
        """Crea el archivo aquí y luego en Drive; solo entonces toma su ruta final."""
        if not nombre.lower().endswith(".db"):
            nombre += ".db"
        draft = self.temp_dir / f"nuevo-{uuid.uuid4().hex}.db"
        db = Database.create(draft, nombre=nombre, moneda=moneda)
        try:
            with self._snapshot(db) as snapshot:
                remote = self.gateway.create(
                    snapshot, nombre, self.folder(), db.entorno_id
                )
                self._check_upload(snapshot, remote)
                fingerprint = _sha256(snapshot)
            entorno_id = db.entorno_id
            db.close()
            os.replace(draft, self.local_path(remote.id))
        finally:
            draft.unlink(missing_ok=True)
        self.state.save_entry(
            SyncEntry(remote.id, nombre, entorno_id, remote.head_revision, fingerprint)
        )
        return remote

    def link_picked_file(self, file_id: str) -> RemoteFile:
        """Adopta un .db elegido en el selector de Google.

        Lo descarga, lo valida como entorno de esta aplicación y le pone la etiqueta
        de identidad. Rechaza cualquier archivo que no lo sea.
        """
        remote = self.gateway.get(file_id)
        with self._scratch() as scratch:
            incoming = scratch / "vinculado.db"
            self._fetch(remote, incoming)
            entorno_id = verify_file(incoming)
            if remote.entorno_id and remote.entorno_id != entorno_id:
                raise EnvironmentMismatchError(f"{remote.name}: la etiqueta no coincide.")
            remote = self.gateway.tag_environment(file_id, entorno_id)
            self._install(incoming, file_id)
            self._record_base(remote, entorno_id)
        return remote

    # ---------------------------------------------------------- cambio de entorno
    def activate(self, remote: RemoteFile) -> ActiveEnvironment:
        """Cierra el entorno actual por completo y luego abre el nuevo."""
        self.close_active()
        result = self._pull_for_open(remote)
        db = Database.open(self.local_path(remote.id))
        self._generation += 1
        self.active = ActiveEnvironment(
            generation=self._generation,
            file_id=remote.id,
            nombre=remote.name,
            db=db,
            repo=Repository(db),
            security=SecurityManager(db, self.state),
            sync=result,
        )
        return self.active

    def close_active(self) -> SyncResult | None:
        """Punto de sincronización para "al cambiar de DB" y "al cerrar la app"."""
        active, self.active = self.active, None
        if active is None:
            return None
        try:
            # Sin descarga: al salir de un entorno bajarlo sería gastar ancho de banda.
            # Si Drive tiene algo nuevo, lo trae el próximo `activate`.
            return self.sync(active.file_id, pull=False)
        finally:
            active.db.close()

    # ------------------------------------------------------------ sincronización
    def sync(self, file_id: str, *, pull: bool = True) -> SyncResult:
        """`pull=False` es el modo "al cerrar": sin cambios locales no toca la red."""
        entry = self._require_entry(file_id)
        db = Database(self.local_path(file_id))
        with self._snapshot(db) as snapshot:
            local_changed = _sha256(snapshot) != entry.base_fingerprint
            if not local_changed and not pull and not entry.conflicto:
                return SyncResult(SyncStatus.UP_TO_DATE, "Sin cambios locales.")
            try:
                remote = self.gateway.get(file_id)
            except RemoteNotFoundError:
                return self._flag_conflict(entry, "El archivo ya no existe en Drive.")
            except _RETRY_LATER as exc:
                return SyncResult(SyncStatus.OFFLINE, str(exc))
            if remote.trashed:
                return self._flag_conflict(entry, "El archivo está en la papelera de Drive.")

            remote_changed = remote.head_revision != entry.base_revision
            if entry.conflicto or (local_changed and remote_changed):
                return self._flag_conflict(entry, "Hay cambios aquí y en Drive.")
            if not local_changed and not remote_changed:
                return SyncResult(SyncStatus.UP_TO_DATE)
            if local_changed:
                try:
                    self._push(entry, remote, snapshot)
                except _RETRY_LATER as exc:
                    return SyncResult(SyncStatus.OFFLINE, str(exc))
                return SyncResult(SyncStatus.UPLOADED)

        is_open = self.active is not None and self.active.file_id == file_id
        if is_open or not pull:
            # Nunca se reemplaza el archivo debajo de un entorno abierto.
            return SyncResult(SyncStatus.UP_TO_DATE, "Hay una versión nueva en Drive.")
        try:
            self._pull(entry, remote)
        except _RETRY_LATER as exc:
            return SyncResult(SyncStatus.OFFLINE, str(exc))
        return SyncResult(SyncStatus.DOWNLOADED)

    def has_local_changes(self, file_id: str) -> bool:
        """Sin red: ¿hay algo que subir?"""
        entry = self._require_entry(file_id)
        with self._snapshot(Database(self.local_path(file_id))) as snapshot:
            return _sha256(snapshot) != entry.base_fingerprint

    def known_environments(self) -> list[RemoteFile]:
        """Entornos con copia en este equipo. Sirve para trabajar sin conexión."""
        known = []
        for entry in self.state.entries():
            if self.local_path(entry.file_id).is_file():
                known.append(
                    RemoteFile(
                        id=entry.file_id,
                        name=entry.nombre,
                        head_revision=entry.base_revision,
                        md5="",
                        app_properties={ENV_PROPERTY: entry.entorno_id},
                    )
                )
        return sorted(known, key=lambda r: r.name.lower())

    def resolve_conflict(self, file_id: str, resolution: Resolution) -> SyncResult:
        """Requiere el entorno cerrado: dos de las opciones reemplazan el archivo."""
        if self.active is not None and self.active.file_id == file_id:
            raise DriveError("Cierra el entorno antes de resolver el conflicto.")
        entry = self._require_entry(file_id)
        db = Database(self.local_path(file_id))
        try:
            remote = self.gateway.get(file_id)
        except RemoteNotFoundError:
            remote = None
        if remote is None or remote.trashed:
            if resolution is Resolution.KEEP_REMOTE:
                raise DriveError("Drive ya no tiene una versión que adoptar.")
            self._republish(db, entry)
            return SyncResult(SyncStatus.UPLOADED)

        if resolution is Resolution.KEEP_LOCAL:
            with self._snapshot(db) as snapshot:
                self._push(entry, remote, snapshot)
            return SyncResult(SyncStatus.UPLOADED)

        if resolution is Resolution.KEEP_BOTH:
            self._upload_as_copy(db, entry)

        self._pull(entry, remote)
        return SyncResult(SyncStatus.DOWNLOADED)

    # ------------------------------------------------------------------- interno
    def _pull_for_open(self, remote: RemoteFile) -> SyncResult:
        entry = self.state.entry(remote.id)
        if entry is None or not self.local_path(remote.id).is_file():
            # La fila del dropdown puede estar vieja: su md5 no serviría para validar.
            remote = self.gateway.get(remote.id)
            with self._scratch() as scratch:
                incoming = scratch / "entrante.db"
                self._fetch(remote, incoming)
                entorno_id = verify_file(incoming)
                self._assert_same_environment(remote, entorno_id)
                self._install(incoming, remote.id)
                self._record_base(remote, entorno_id)
            return SyncResult(SyncStatus.DOWNLOADED)
        return self.sync(remote.id)

    def _push(self, entry: SyncEntry, remote: RemoteFile, snapshot: Path) -> None:
        local_id = verify_file(snapshot)
        self._assert_same_environment(remote, local_id)
        if local_id != entry.entorno_id:
            raise EnvironmentMismatchError(f"{entry.nombre}: identidad local inesperada.")
        uploaded = self.gateway.update(entry.file_id, snapshot)
        self._check_upload(snapshot, uploaded)
        entry.base_revision = uploaded.head_revision
        entry.base_fingerprint = _sha256(snapshot)
        entry.conflicto = False
        self.state.save_entry(entry)

    def _pull(self, entry: SyncEntry, remote: RemoteFile) -> None:
        with self._scratch() as scratch:
            incoming = scratch / "entrante.db"
            self._fetch(remote, incoming)
            entorno_id = verify_file(incoming)
            self._assert_same_environment(remote, entorno_id)
            if entorno_id != entry.entorno_id:
                raise EnvironmentMismatchError(f"{entry.nombre}: identidad remota inesperada.")
            self._install(incoming, entry.file_id)
            self._record_base(remote, entorno_id)

    def _fetch(self, remote: RemoteFile, dest: Path) -> None:
        self.gateway.download(remote.id, dest)
        if remote.md5 and md5_of(dest) != remote.md5:
            raise ChecksumError(f"La descarga de {remote.name} llegó dañada.")

    def _install(self, incoming: Path, file_id: str) -> None:
        target = self.local_path(file_id)
        if target.exists():
            shutil.copy2(target, target.with_suffix(".bak"))
        os.replace(incoming, target)

    def _record_base(self, remote: RemoteFile, entorno_id: str) -> None:
        db = Database(self.local_path(remote.id))
        with self._snapshot(db) as snapshot:
            fingerprint = _sha256(snapshot)
        self.state.save_entry(
            SyncEntry(remote.id, remote.name, entorno_id, remote.head_revision, fingerprint)
        )

    def _republish(self, db: Database, entry: SyncEntry) -> RemoteFile:
        """El archivo se borró de Drive: se vuelve a crear con la misma identidad."""
        with self._snapshot(db) as snapshot:
            remote = self.gateway.create(snapshot, entry.nombre, self.folder(), entry.entorno_id)
            self._check_upload(snapshot, remote)
            fingerprint = _sha256(snapshot)
        os.replace(self.local_path(entry.file_id), self.local_path(remote.id))
        self.state.remove_entry(entry.file_id)
        self.state.save_entry(
            SyncEntry(remote.id, entry.nombre, entry.entorno_id, remote.head_revision, fingerprint)
        )
        return remote

    def _upload_as_copy(self, db: Database, entry: SyncEntry) -> RemoteFile:
        """Sube lo local como entorno nuevo, con identidad propia."""
        stem = Path(entry.nombre).stem
        name = f"{stem} (conflicto {uuid.uuid4().hex[:6]}).db"
        new_id = uuid.uuid4().hex
        with self._snapshot(db) as snapshot:
            conn = sqlite3.connect(snapshot)
            with conn:
                conn.execute("UPDATE meta SET valor = ? WHERE clave = 'entorno_id'", (new_id,))
                conn.execute("UPDATE meta SET valor = ? WHERE clave = 'nombre'", (name,))
            conn.close()
            remote = self.gateway.create(snapshot, name, self.folder(), new_id)
            self._check_upload(snapshot, remote)
        return remote

    @staticmethod
    def _check_upload(source: Path, remote: RemoteFile) -> None:
        if remote.md5 and remote.md5 != md5_of(source):
            raise ChecksumError(f"Drive reporta un contenido distinto para {remote.name}.")

    @staticmethod
    def _assert_same_environment(remote: RemoteFile, entorno_id: str) -> None:
        if remote.entorno_id and remote.entorno_id != entorno_id:
            raise EnvironmentMismatchError(
                f"{remote.name} pertenece a otro entorno. No se mezclan los datos."
            )

    def _flag_conflict(self, entry: SyncEntry, detail: str) -> SyncResult:
        if not entry.conflicto:
            entry.conflicto = True
            self.state.save_entry(entry)
        return SyncResult(SyncStatus.CONFLICT, detail)

    def _require_entry(self, file_id: str) -> SyncEntry:
        entry = self.state.entry(file_id)
        if entry is None:
            raise DriveError(f"El archivo {file_id} no está vinculado en este equipo.")
        return entry

    @contextmanager
    def _scratch(self) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(dir=self.temp_dir, ignore_cleanup_errors=True) as name:
            yield Path(name)

    @contextmanager
    def _snapshot(self, db: Database) -> Iterator[Path]:
        with self._scratch() as scratch:
            yield db.snapshot(scratch / "snapshot.db")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
