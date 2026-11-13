"""Controlador de la aplicación. No importa Flet: la vista lo usa y las pruebas también.

Todas sus operaciones bloquean (red, bcrypt, disco). La vista las llama con
`asyncio.to_thread`. Un candado serializa todo lo que toca Drive o cambia de entorno:
así el cliente HTTP nunca se usa desde dos hilos a la vez y un cierre no se cruza con
un cambio de entorno.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..models.entities import BudgetStatus, Expense, ExpenseInput, Total
from ..models.repository import OTROS
from ..models.security import EditSession
from ..services import drive_auth
from ..services.drive_gateway import (
    DriveAccessError,
    GoogleDriveGateway,
    OfflineError,
    RemoteFile,
)
from ..services.sync_engine import (
    ActiveEnvironment,
    Resolution,
    SyncEngine,
    SyncResult,
    SyncStatus,
    default_data_dir,
)

PAGE_SIZE = 30


class NotLoggedIn(OfflineError):
    """Aún no hay sesión de Google. La app sigue con las copias locales."""


class LazyGateway:
    """Crea el cliente de Drive la primera vez que se usa, ya con credenciales."""

    def __init__(self, factory: Callable = GoogleDriveGateway.from_credentials) -> None:
        self._factory = factory
        self._credentials = None
        self._gateway = None

    @property
    def connected(self) -> bool:
        return self._credentials is not None

    def connect(self, credentials) -> None:
        self._credentials = credentials
        self._gateway = None

    def disconnect(self) -> None:
        self._credentials = None
        self._gateway = None

    def __getattr__(self, name: str):
        if self._credentials is None:
            raise NotLoggedIn("Sin sesión de Google.")
        if self._gateway is None:
            self._gateway = self._factory(self._credentials)
        return getattr(self._gateway, name)


@dataclass(frozen=True)
class Dashboard:
    generation: int
    mes: str
    budget: BudgetStatus
    expenses: list[Expense]
    has_more: bool


# Vistas de la tarjeta de análisis: (clave, texto del selector).
ANALYSIS_VIEWS = [
    ("dia", "Total por día"),
    ("mes", "Total por mes"),
    ("categoria", "Por categoría"),
    ("lugar", "Por lugar"),
    ("producto", "Por producto"),
]


@dataclass(frozen=True)
class Analysis:
    generation: int
    view: str
    period: str          # 'YYYY-MM' o 'YYYY'
    kind: str            # 'scatter' o 'bars'
    totals: list[Total]
    others: list[Total] = field(default_factory=list)  # lo que suma la barra "Otros"


BAR_TOP = 8


def split_top(totals: list[Total], top: int = BAR_TOP) -> tuple[list[Total], list[Total]]:
    """Las primeras `top - 1` barras, más una barra "Otros" que suma el resto.

    Devuelve también el resto por separado, para poder mostrar su detalle.
    """
    if len(totals) <= top:
        return totals, []
    head, rest = totals[: top - 1], totals[top - 1:]
    others = Total(
        OTROS, f"{OTROS} ({len(rest)})",
        sum(t.total_cent for t in rest), sum(t.transacciones for t in rest),
    )
    return head + [others], rest


@dataclass
class Catalog:
    environments: list[RemoteFile] = field(default_factory=list)
    online: bool = False
    error: str = ""


def default_app_dir() -> Path:
    """En Android, FLET_APP_STORAGE_DATA apunta al almacenamiento privado de la app."""
    flet_data = os.environ.get("FLET_APP_STORAGE_DATA")
    return Path(flet_data) / "FinanzasLocal" if flet_data else default_data_dir()


class AppController:
    def __init__(self, data_dir: Path | None = None, gateway=None, mode: str | None = None) -> None:
        self.data_dir = Path(data_dir or default_app_dir())
        self.mode = mode or drive_auth.auth_mode()
        self.gateway = gateway if gateway is not None else LazyGateway()
        self.engine = SyncEngine(self.gateway, self.data_dir)
        self.session: EditSession | None = None
        self._lock = threading.RLock()
        self._client: dict | None = None
        self._store = None

    # ------------------------------------------------------------- estado simple
    @property
    def env(self) -> ActiveEnvironment | None:
        return self.engine.active

    @property
    def online(self) -> bool:
        return getattr(self.gateway, "connected", True)

    @property
    def is_unlocked(self) -> bool:
        env = self.env
        return bool(env and self.session and self.session.is_valid_for(env.db.entorno_id))

    # ------------------------------------------------------------ sesión Google
    def _client_config(self) -> dict:
        if self._client is None:
            path = drive_auth.find_client_secret(self.mode)
            self._client = drive_auth.read_client(path)
            self._client["_path"] = str(path)
            self._store = drive_auth.default_token_store(self.data_dir, self._client["client_id"])
        return self._client

    def try_silent_login(self) -> bool:
        """Usa el token guardado. Falso si hace falta iniciar sesión."""
        if not isinstance(self.gateway, LazyGateway):
            return True
        config = self._client_config()
        credentials = drive_auth.load_saved_credentials(config, self._store)
        if credentials is None:
            return False
        with self._lock:
            self.gateway.connect(credentials)
        return True

    def desktop_login(self) -> None:
        config = self._client_config()
        credentials = drive_auth.desktop_login(Path(config["_path"]), self._store)
        with self._lock:
            self.gateway.connect(credentials)

    def start_device_login(self) -> tuple[drive_auth.DeviceFlow, drive_auth.DeviceCode]:
        flow = drive_auth.DeviceFlow(self._client_config(), self._store)
        return flow, flow.start()

    def finish_device_login(self, flow, code, cancelled: threading.Event) -> None:
        credentials = flow.wait(code, cancelled)
        with self._lock:
            self.gateway.connect(credentials)

    def logout(self) -> None:
        self._client_config()
        self._store.clear()
        with self._lock:
            self.gateway.disconnect()

    # -------------------------------------------------------------- entornos
    def catalog(self) -> Catalog:
        """Entornos de Drive; sin red, los que ya tienen copia en este equipo."""
        with self._lock:
            try:
                return Catalog(self.engine.list_environments(), online=True)
            except (OfflineError, DriveAccessError) as exc:
                error = "" if isinstance(exc, NotLoggedIn) else str(exc)
                return Catalog(self.engine.known_environments(), online=False, error=error)

    def open_environment(self, remote: RemoteFile) -> ActiveEnvironment:
        with self._lock:
            self.lock_editing()
            return self.engine.activate(remote)

    def create_environment(self, nombre: str) -> RemoteFile:
        nombre = nombre.strip()
        if not nombre:
            raise ValueError("Escribe un nombre para el entorno.")
        with self._lock:
            return self.engine.create_environment(nombre)

    def sync_on_exit(self) -> SyncResult | None:
        """Cierre de la app: sube si hay cambios y libera el entorno."""
        with self._lock:
            self.lock_editing()
            return self.engine.close_active()

    def sync_in_background(self) -> SyncResult | None:
        """Android al pasar a segundo plano: sube sin cerrar el entorno."""
        with self._lock:
            env = self.env
            if env is None or not self.engine.has_local_changes(env.file_id):
                return None
            return self.engine.sync(env.file_id, pull=False)

    def resolve_conflict(self, resolution: Resolution) -> ActiveEnvironment:
        with self._lock:
            env = self.env
            if env is None:
                raise ValueError("No hay un entorno abierto.")
            remote_id, entorno_id = env.file_id, env.db.entorno_id
            self.lock_editing()
            self.engine.close_active()
            self.engine.resolve_conflict(remote_id, resolution)
            # Si Drive había perdido el archivo, se republica con otro id y la misma identidad.
            target = next(
                r for r in self.engine.known_environments() if r.entorno_id == entorno_id
            )
            return self.engine.activate(target)

    # ------------------------------------------------------------------ PIN
    def needs_pin_setup(self) -> bool:
        env = self.env
        return env is not None and not env.security.has_credential()

    def setup_pin(self, pin: str) -> None:
        self._require_env().security.set_initial_credential(pin, "pin")

    def unlock(self, pin: str) -> None:
        self.session = self._require_env().security.unlock(pin)

    def change_pin(self, current: str, new: str) -> None:
        self._require_env().security.change_credential(current, new, "pin")

    def lock_editing(self) -> None:
        if self.session is not None:
            self.session.revoke()
        self.session = None

    # ----------------------------------------------------------------- datos
    def dashboard(self, mes: str, limit: int = PAGE_SIZE) -> Dashboard:
        env = self._require_env()
        expenses = env.repo.list_expenses(mes, limit=limit + 1)
        return Dashboard(
            generation=env.generation,
            mes=mes,
            budget=env.repo.budget_status(mes),
            expenses=expenses[:limit],
            has_more=len(expenses) > limit,
        )

    def analysis(self, view: str, mes: str, scope: str = "mes") -> Analysis:
        """`scope` aplica a las barras: el mes elegido o todo su año."""
        env = self._require_env()
        year = mes[:4]
        if view == "dia":
            return Analysis(env.generation, view, mes, "scatter", env.repo.daily_totals(mes))
        if view == "mes":
            return Analysis(env.generation, view, year, "scatter", env.repo.monthly_totals(year))
        period = year if scope == "anio" else mes
        shown, others = split_top(env.repo.totals_by(view, period, top=None))
        return Analysis(env.generation, view, period, "bars", shown, others)

    def suggest_categories(self, prefix: str = "") -> list[str]:
        env = self.env
        return env.repo.suggest_categories(prefix, limit=6) if env else []

    def get_expense(self, gasto_id: int) -> Expense | None:
        return self._require_env().repo.get_expense(gasto_id)

    def add_expense(self, data: ExpenseInput) -> int:
        return self._require_env().repo.add_expense(data, self.session)

    def update_expense(self, gasto_id: int, data: ExpenseInput) -> None:
        self._require_env().repo.update_expense(gasto_id, data, self.session)

    def delete_expense(self, gasto_id: int) -> None:
        self._require_env().repo.delete_expense(gasto_id, self.session)

    def set_budget(self, mes: str, monto) -> None:
        self._require_env().repo.set_budget(mes, monto, self.session)

    def suggest_places(self, prefix: str) -> list[str]:
        env = self.env
        return env.repo.suggest_places(prefix) if env and prefix.strip() else []

    def _require_env(self) -> ActiveEnvironment:
        env = self.env
        if env is None:
            raise ValueError("Primero elige o crea un entorno.")
        return env


def describe_sync(result: SyncResult | None) -> tuple[str, str]:
    """(clave de icono, texto) para mostrar el estado de sincronización."""
    if result is None:
        return "ok", "Sin cambios por sincronizar"
    return {
        SyncStatus.UP_TO_DATE: ("ok", "Al día con Drive"),
        SyncStatus.UPLOADED: ("ok", "Cambios subidos a Drive"),
        SyncStatus.DOWNLOADED: ("ok", "Versión de Drive descargada"),
        SyncStatus.OFFLINE: ("offline", "Sin conexión: se sube después"),
        SyncStatus.CONFLICT: ("conflict", result.detail or "Conflicto con Drive"),
    }[result.status]
