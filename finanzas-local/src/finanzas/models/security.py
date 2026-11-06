"""Desbloqueo de edición con bcrypt.

Protege contra alguien frente a la aplicación abierta. No cifra el archivo: quien
tenga el .db puede leerlo con cualquier cliente SQLite (ver README, Limitaciones).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import bcrypt

from .database import Database, DatabaseError, now_local

BCRYPT_MAX_BYTES = 72
BCRYPT_ROUNDS = 12
SESSION_SECONDS = 300
FREE_ATTEMPTS = 5
BASE_LOCKOUT_SECONDS = 30
MAX_LOCKOUT_SECONDS = 900

_PIN = re.compile(r"[0-9]{4}")


class CredentialError(DatabaseError):
    """La credencial no cumple las reglas."""


class WrongSecretError(DatabaseError):
    """PIN o contraseña incorrectos."""

    def __init__(self, remaining: int) -> None:
        super().__init__("PIN o contraseña incorrectos.")
        self.remaining = remaining


class LockedOutError(DatabaseError):
    """Demasiados intentos fallidos."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"Demasiados intentos. Espera {seconds} s.")
        self.seconds = seconds


class AttemptStore(Protocol):
    """Guarda los intentos fallidos por entorno, fuera del .db sincronizado."""

    def get_attempts(self, entorno_id: str) -> tuple[int, float]: ...
    def set_attempts(self, entorno_id: str, failures: int, locked_until: float) -> None: ...


class MemoryAttemptStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[int, float]] = {}

    def get_attempts(self, entorno_id: str) -> tuple[int, float]:
        return self._data.get(entorno_id, (0, 0.0))

    def set_attempts(self, entorno_id: str, failures: int, locked_until: float) -> None:
        self._data[entorno_id] = (failures, locked_until)


@dataclass
class EditSession:
    """Permiso temporal de edición, atado a un solo entorno."""

    entorno_id: str
    expires_at: float
    _clock: Callable[[], float] = time.time
    revoked: bool = False

    def is_valid_for(self, entorno_id: str) -> bool:
        return (
            not self.revoked
            and self.entorno_id == entorno_id
            and self._clock() < self.expires_at
        )

    def revoke(self) -> None:
        self.revoked = True


def validate_secret(secret: str, tipo: str) -> bytes:
    encoded = secret.encode("utf-8")
    if tipo == "pin":
        if not _PIN.fullmatch(secret):
            raise CredentialError("El PIN debe tener exactamente 4 dígitos.")
    elif tipo == "password":
        if len(secret) < 8:
            raise CredentialError("La contraseña debe tener al menos 8 caracteres.")
        if len(encoded) > BCRYPT_MAX_BYTES:
            # bcrypt 5 lanza ValueError y las versiones anteriores truncaban en silencio.
            raise CredentialError(f"La contraseña no puede superar {BCRYPT_MAX_BYTES} bytes.")
    else:
        raise CredentialError(f"Tipo de credencial desconocido: {tipo!r}")
    return encoded


class SecurityManager:
    """bcrypt tarda a propósito: llama a estos métodos fuera del hilo de la UI."""

    def __init__(
        self,
        db: Database,
        attempts: AttemptStore | None = None,
        *,
        rounds: int = BCRYPT_ROUNDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.attempts = attempts or MemoryAttemptStore()
        self.rounds = rounds
        self.clock = clock

    def has_credential(self) -> bool:
        with self.db.read() as conn:
            return conn.execute("SELECT 1 FROM credencial").fetchone() is not None

    def set_initial_credential(self, secret: str, tipo: str = "pin") -> None:
        hashed = self._hash(secret, tipo)
        with self.db.bootstrap_write() as conn:
            conn.execute(
                "INSERT INTO credencial (id, tipo, hash, actualizado_en) VALUES (1, ?, ?, ?)",
                (tipo, hashed, now_local()),
            )

    def change_credential(self, current: str, new: str, tipo: str = "pin") -> None:
        session = self.unlock(current)
        hashed = self._hash(new, tipo)
        with self.db.write(session) as conn:
            conn.execute(
                "UPDATE credencial SET tipo = ?, hash = ?, actualizado_en = ? WHERE id = 1",
                (tipo, hashed, now_local()),
            )
        session.revoke()

    def unlock(self, secret: str) -> EditSession:
        entorno = self.db.entorno_id
        failures, locked_until = self.attempts.get_attempts(entorno)
        now = self.clock()
        if now < locked_until:
            raise LockedOutError(int(locked_until - now) + 1)

        with self.db.read() as conn:
            row = conn.execute("SELECT hash FROM credencial WHERE id = 1").fetchone()
        if row is None:
            raise CredentialError("Este entorno todavía no tiene credencial.")

        encoded = secret.encode("utf-8")
        ok = len(encoded) <= BCRYPT_MAX_BYTES and bcrypt.checkpw(encoded, row["hash"])
        if ok:
            self.attempts.set_attempts(entorno, 0, 0.0)
            return EditSession(entorno, self.clock() + SESSION_SECONDS, self.clock)

        failures += 1
        over = failures - FREE_ATTEMPTS
        if over >= 0:
            delay = min(BASE_LOCKOUT_SECONDS * 2**over, MAX_LOCKOUT_SECONDS)
            self.attempts.set_attempts(entorno, failures, self.clock() + delay)
            raise LockedOutError(delay)
        self.attempts.set_attempts(entorno, failures, 0.0)
        raise WrongSecretError(FREE_ATTEMPTS - failures)

    def _hash(self, secret: str, tipo: str) -> bytes:
        return bcrypt.hashpw(validate_secret(secret, tipo), bcrypt.gensalt(self.rounds))
