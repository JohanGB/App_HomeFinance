"""Autorización OAuth con Google.

`drive.file` es el permiso más estrecho que sirve aquí: la app solo ve lo que ella
creó y lo que el usuario le entregó en el selector de Google. Ver todos los archivos
exigiría `drive` o `drive.readonly`, que Google clasifica como restringidos.

Dos flujos, uno por plataforma:

* Windows: cliente "Aplicación de escritorio". Abre el navegador y recibe la respuesta
  en un puerto local.
* Android: cliente "TV y dispositivos de entrada limitada". La app muestra un código y
  tú lo apruebas en google.com/device. Google no permite el puerto local en Android, y
  este flujo acepta `drive.file`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES = [DRIVE_FILE_SCOPE]
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEVICE_CODE_URI = "https://oauth2.googleapis.com/device/code"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

DESKTOP_SECRET = "client_secret.json"
DEVICE_SECRET = "client_secret_tv.json"

# Identificadores internos con el nombre anterior: cambiarlos obligaría a iniciar sesión
# otra vez y dejaría huérfanas las copias locales. El usuario nunca los ve.
_KEYRING_SERVICE = "finanzas-local"
PICK_TIMEOUT_SECONDS = 300


class AuthError(Exception):
    """No se pudo obtener o renovar la autorización."""


class AuthOfflineError(AuthError):
    """No hay red para hablar con Google."""


class PickerCancelled(AuthError):
    """El usuario cerró el selector o no eligió ningún archivo."""


class MissingClientSecret(AuthError):
    """Falta el archivo JSON del cliente OAuth."""


# --------------------------------------------------------------------- plataforma
def is_android() -> bool:
    return os.environ.get("FLET_PLATFORM") == "android" or hasattr(sys, "getandroidapilevel")


def auth_mode() -> str:
    """'device' en Android, 'desktop' en el resto. FINANZAS_AUTH lo fuerza."""
    forced = os.environ.get("FINANZAS_AUTH", "").strip().lower()
    if forced in {"device", "desktop"}:
        return forced
    return "device" if is_android() else "desktop"


def secrets_dirs() -> list[Path]:
    """Dónde buscar el JSON del cliente, en orden.

    `src/secrets/` viaja dentro del APK. El directorio actual sirve al ejecutar desde
    la carpeta del proyecto, como en las pruebas anteriores.
    """
    package_root = Path(__file__).resolve().parents[2]
    dirs = [package_root / "secrets", Path.cwd() / "secrets", Path.cwd()]
    data = os.environ.get("FLET_APP_STORAGE_DATA")
    if data:
        dirs.append(Path(data))
    return dirs


def find_client_secret(mode: str) -> Path:
    name = DEVICE_SECRET if mode == "device" else DESKTOP_SECRET
    for folder in secrets_dirs():
        if (folder / name).is_file():
            return folder / name
    raise MissingClientSecret(
        f"No encontré {name}. Ponlo en la carpeta src/secrets del proyecto."
    )


def read_client(path: Path) -> dict:
    """Acepta el JSON de cualquier tipo de cliente ("installed", "web"...)."""
    data = json.loads(Path(path).read_text("utf-8"))
    config = next(iter(data.values())) if len(data) == 1 else data
    for key in ("client_id", "client_secret"):
        if not config.get(key):
            raise MissingClientSecret(f"{Path(path).name} no tiene {key}.")
    config.setdefault("token_uri", TOKEN_URI)
    return config


# ------------------------------------------------------------------- almacenes
class TokenStore(Protocol):
    def load(self) -> str | None: ...
    def save(self, refresh_token: str) -> None: ...
    def clear(self) -> None: ...


class KeyringTokenStore:
    """Administrador de credenciales de Windows (o el llavero del sistema).

    Solo se guarda el refresh token: el JSON completo puede superar el límite del
    Administrador de credenciales de Windows, y el resto se reconstruye.
    """

    def __init__(self, account: str = "google-refresh-token") -> None:
        self.account = account

    def load(self) -> str | None:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, self.account)

    def save(self, refresh_token: str) -> None:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, self.account, refresh_token)

    def clear(self) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(_KEYRING_SERVICE, self.account)
        except PasswordDeleteError:
            pass


class FileTokenStore:
    """Archivo en el almacenamiento privado de la app. Android aísla ese directorio
    por aplicación; es la opción disponible allí, que no tiene llavero para Python."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> str | None:
        try:
            return json.loads(self.path.read_text("utf-8")).get("refresh_token")
        except (OSError, ValueError):
            return None

    def save(self, refresh_token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"refresh_token": refresh_token}), "utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def default_token_store(data_dir: Path, client_id: str) -> TokenStore:
    """Un token por cliente: el de escritorio no sirve para renovar el de TV."""
    account = f"refresh-{client_id.split('.')[0]}"
    if is_android():
        return FileTokenStore(data_dir / f"{account}.json")
    try:
        import keyring

        keyring.get_keyring()
        return KeyringTokenStore(account)
    except Exception:  # pragma: no cover - sistemas sin llavero
        return FileTokenStore(data_dir / f"{account}.json")


# ------------------------------------------------------------------ credenciales
def _credentials(config: dict, refresh_token: str, token: str | None = None) -> Credentials:
    return Credentials(
        token=token,
        refresh_token=refresh_token,
        token_uri=config.get("token_uri", TOKEN_URI),
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=SCOPES,
    )


def load_saved_credentials(config: dict, store: TokenStore) -> Credentials | None:
    """Renueva sin interacción. None si no hay token o Google lo rechazó."""
    saved = store.load()
    if not saved:
        return None
    credentials = _credentials(config, saved)
    try:
        credentials.refresh(Request())
    except RefreshError:
        # Revocado o vencido: en estado "Testing" dura 7 días.
        store.clear()
        return None
    except TransportError as exc:
        raise AuthOfflineError(str(exc)) from exc
    return credentials


def desktop_login(secret_path: Path, store: TokenStore) -> Credentials:
    """Abre el navegador del sistema y espera la respuesta en un puerto local."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    credentials = flow.run_local_server(
        port=0,
        open_browser=True,
        success_message="Listo. Ya puedes volver a MyCents.",
    )
    if credentials.refresh_token:
        store.save(credentials.refresh_token)
    return credentials


def get_credentials(client_secrets: Path | str, store: TokenStore | None = None) -> Credentials:
    """Token guardado o, si no sirve, inicio de sesión en el navegador (Windows)."""
    store = store or KeyringTokenStore()
    config = read_client(Path(client_secrets))
    credentials = load_saved_credentials(config, store)
    return credentials or desktop_login(Path(client_secrets), store)


# ------------------------------------------------------------- flujo dispositivo
@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: float


class DeviceFlow:
    """OAuth para dispositivos de entrada limitada: código + google.com/device."""

    def __init__(
        self,
        config: dict,
        store: TokenStore,
        *,
        post: Callable | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.store = store
        self._post = post or self._requests_post
        self.clock = clock
        self.sleep = sleep

    @staticmethod
    def _requests_post(url: str, data: dict) -> tuple[int, dict]:
        import requests

        try:
            response = requests.post(url, data=data, timeout=20)
        except requests.RequestException as exc:
            raise AuthOfflineError(str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body

    def start(self) -> DeviceCode:
        status, body = self._post(
            DEVICE_CODE_URI,
            {"client_id": self.config["client_id"], "scope": " ".join(SCOPES)},
        )
        if status != 200:
            raise AuthError(
                f"Google rechazó la solicitud ({body.get('error', status)}). "
                "Revisa que el cliente sea de tipo TV y dispositivos de entrada limitada."
            )
        return DeviceCode(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_url=body.get("verification_url") or body.get("verification_uri"),
            interval=int(body.get("interval", 5)),
            expires_at=self.clock() + int(body.get("expires_in", 1800)),
        )

    def wait(self, code: DeviceCode, cancelled: threading.Event | None = None) -> Credentials:
        """Consulta a Google hasta que apruebes, rechaces o venza el código."""
        interval = code.interval
        while self.clock() < code.expires_at:
            if cancelled is not None and cancelled.is_set():
                raise AuthError("Inicio de sesión cancelado.")
            self.sleep(interval)
            try:
                status, body = self._post(
                    self.config.get("token_uri", TOKEN_URI),
                    {
                        "client_id": self.config["client_id"],
                        "client_secret": self.config["client_secret"],
                        "device_code": code.device_code,
                        "grant_type": DEVICE_GRANT,
                    },
                )
            except AuthOfflineError:
                # Mientras apruebas en el navegador, Android puede cortarle la red a la app
                # en segundo plano (Ahorro de datos). Se sigue intentando hasta que vuelva
                # o venza el código.
                continue
            if status == 200 and body.get("access_token"):
                if not body.get("refresh_token"):
                    raise AuthError("Google no entregó un token de renovación.")
                self.store.save(body["refresh_token"])
                return _credentials(self.config, body["refresh_token"], body["access_token"])
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "access_denied":
                raise AuthError("Rechazaste el acceso en Google.")
            raise AuthError(f"Google respondió: {error or status}.")
        raise AuthError("El código venció. Inicia sesión de nuevo.")


# ----------------------------------------------------------- selector de Google
class _RedirectHandler(BaseHTTPRequestHandler):
    params: dict[str, list[str]] = {}

    def do_GET(self) -> None:  # noqa: N802 - nombre impuesto por http.server
        type(self).params = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Listo. Ya puedes volver a MyCents.".encode("utf-8"))

    def log_message(self, *args) -> None:
        pass


def pick_drive_files(
    client_secrets: Path | str, store: TokenStore | None = None
) -> tuple[Credentials, list[str]]:
    """Abre el selector de Google en el navegador y devuelve los ids elegidos (Windows).

    Es la forma de entregarle a la app un .db que ella no creó. Requiere la API de
    Google Picker habilitada en el proyecto. `run_local_server` no expone el parámetro
    `picked_file_ids` de la redirección; por eso aquí se recibe directamente.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    store = store or KeyringTokenStore()
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    handler = type("Handler", (_RedirectHandler,), {"params": {}})
    server = HTTPServer(("127.0.0.1", 0), handler)
    server.timeout = PICK_TIMEOUT_SECONDS
    try:
        flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            trigger_onepick="true",
            allow_multiple="true",
        )
        waiter = threading.Thread(target=server.handle_request, daemon=True)
        waiter.start()
        webbrowser.open(url)
        waiter.join(PICK_TIMEOUT_SECONDS + 1)
    finally:
        server.server_close()

    params = handler.params
    code = params.get("code", [None])[0]
    picked = params.get("picked_file_ids", [""])[0]
    if "error" in params or not code or not picked:
        raise PickerCancelled("No se eligió ningún archivo.")

    flow.fetch_token(code=code)
    credentials = flow.credentials
    if credentials.refresh_token:
        store.save(credentials.refresh_token)
    return credentials, [item for item in picked.split(",") if item]
