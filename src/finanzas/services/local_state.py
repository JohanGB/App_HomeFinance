"""Estado por equipo que no viaja con el .db: base de sincronización e intentos de PIN."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SyncEntry:
    """Lo último en que este equipo y Drive estuvieron de acuerdo."""

    file_id: str
    nombre: str
    entorno_id: str
    base_revision: str
    base_fingerprint: str
    conflicto: bool = False


class LocalState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict = {"entornos": {}, "intentos": {}, "carpeta_id": None}
        if path.is_file():
            self._data.update(json.loads(path.read_text("utf-8")))

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
        os.replace(temp, self.path)

    # ------------------------------------------------------------ sincronización
    def entry(self, file_id: str) -> SyncEntry | None:
        with self._lock:
            raw = self._data["entornos"].get(file_id)
        return SyncEntry(**raw) if raw else None

    def entries(self) -> list[SyncEntry]:
        with self._lock:
            return [SyncEntry(**raw) for raw in self._data["entornos"].values()]

    def save_entry(self, entry: SyncEntry) -> None:
        with self._lock:
            self._data["entornos"][entry.file_id] = asdict(entry)
            self._flush()

    def remove_entry(self, file_id: str) -> None:
        with self._lock:
            self._data["entornos"].pop(file_id, None)
            self._flush()

    @property
    def last_environment(self) -> str | None:
        return self._data.get("ultimo_entorno")

    @last_environment.setter
    def last_environment(self, file_id: str | None) -> None:
        with self._lock:
            self._data["ultimo_entorno"] = file_id
            self._flush()

    @property
    def folder_id(self) -> str | None:
        return self._data.get("carpeta_id")

    @folder_id.setter
    def folder_id(self, value: str) -> None:
        with self._lock:
            self._data["carpeta_id"] = value
            self._flush()

    # ----------------------------------------------------- AttemptStore (security)
    def get_attempts(self, entorno_id: str) -> tuple[int, float]:
        with self._lock:
            failures, until = self._data["intentos"].get(entorno_id, (0, 0.0))
        return int(failures), float(until)

    def set_attempts(self, entorno_id: str, failures: int, locked_until: float) -> None:
        with self._lock:
            self._data["intentos"][entorno_id] = [failures, locked_until]
            self._flush()
