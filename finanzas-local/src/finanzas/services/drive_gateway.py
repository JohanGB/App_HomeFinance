"""Traducción entre la aplicación y Google Drive API v3.

`SyncEngine` depende del protocolo `DriveGateway`, no de esta implementación, para
poder probar la lógica de sincronización sin red.

El cliente usa httplib2, que no es seguro entre hilos: crea el gateway dentro del
hilo de sincronización, con `GoogleDriveGateway.from_credentials`.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

FOLDER_MIME = "application/vnd.google-apps.folder"
SQLITE_MIME = "application/vnd.sqlite3"
APP_FOLDER_NAME = "Finanzas Local"
ENV_PROPERTY = "finanzas_entorno_id"

CHUNK_BYTES = 1024 * 1024
RETRIES = 3
_FIELDS = "id,name,md5Checksum,headRevisionId,modifiedTime,size,trashed,appProperties"

_FAILURES = (
    HttpError,
    RefreshError,
    httplib2.HttpLib2Error,
    socket.error,
    socket.timeout,
    ssl.SSLError,
    TimeoutError,
    TransportError,
)


class DriveError(Exception):
    """Error base de la integración con Drive."""


class OfflineError(DriveError):
    """No hay red. Los datos locales siguen intactos y se reintenta después."""


class RemoteNotFoundError(DriveError):
    """El archivo no existe o la app ya no tiene acceso a él."""


class DriveAccessError(DriveError):
    """Drive rechazó la operación: cuota, permisos o token."""


class ChecksumError(DriveError):
    """Los bytes transferidos no coinciden con los que reporta Drive."""


@dataclass(frozen=True)
class RemoteFile:
    id: str
    name: str
    head_revision: str
    md5: str
    modified_time: str = ""
    size: int = 0
    trashed: bool = False
    app_properties: Mapping[str, str] = field(default_factory=dict)

    @property
    def entorno_id(self) -> str | None:
        return self.app_properties.get(ENV_PROPERTY)


class DriveGateway(Protocol):
    def ensure_app_folder(self) -> str: ...
    def list_databases(self, folder_id: str) -> list[RemoteFile]: ...
    def get(self, file_id: str) -> RemoteFile: ...
    def download(self, file_id: str, dest: Path) -> None: ...
    def create(self, source: Path, name: str, folder_id: str, entorno_id: str) -> RemoteFile: ...
    def update(self, file_id: str, source: Path) -> RemoteFile: ...
    def tag_environment(self, file_id: str, entorno_id: str) -> RemoteFile: ...


def md5_of(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _to_remote(data: Mapping) -> RemoteFile:
    return RemoteFile(
        id=data["id"],
        name=data["name"],
        head_revision=data.get("headRevisionId", ""),
        md5=data.get("md5Checksum", ""),
        modified_time=data.get("modifiedTime", ""),
        size=int(data.get("size", 0)),
        trashed=bool(data.get("trashed", False)),
        app_properties=data.get("appProperties", {}),
    )


def _translate(exc: Exception) -> DriveError:
    if isinstance(exc, HttpError):
        status = exc.resp.status
        if status == 404:
            return RemoteNotFoundError(str(exc))
        return DriveAccessError(f"Drive respondió {status}: {exc.reason}")
    if isinstance(exc, RefreshError):
        return DriveAccessError("La autorización de Google venció. Vuelve a conectar la cuenta.")
    return OfflineError(str(exc))


class GoogleDriveGateway:
    def __init__(self, service) -> None:
        self._files = service.files()

    @classmethod
    def from_credentials(cls, credentials) -> "GoogleDriveGateway":
        return cls(build("drive", "v3", credentials=credentials, cache_discovery=False))

    def _run(self, request):
        try:
            return request.execute(num_retries=RETRIES)
        except _FAILURES as exc:
            raise _translate(exc) from exc

    def ensure_app_folder(self) -> str:
        query = (
            f"name = '{escape_query(APP_FOLDER_NAME)}' and mimeType = '{FOLDER_MIME}' "
            "and 'root' in parents and trashed = false"
        )
        found = self._run(
            self._files.list(q=query, spaces="drive", fields="files(id)", pageSize=1)
        ).get("files", [])
        if found:
            return found[0]["id"]
        return self._run(
            self._files.create(
                body={"name": APP_FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": ["root"]},
                fields="id",
            )
        )["id"]

    def list_databases(self, folder_id: str) -> list[RemoteFile]:
        # `name contains` solo compara prefijos: `name contains '.db'` no encuentra
        # gastos_casa.db. Se trae la carpeta y se filtra aquí por extensión.
        query = f"'{escape_query(folder_id)}' in parents and trashed = false"
        token = None
        results: list[RemoteFile] = []
        while True:
            page = self._run(
                self._files.list(
                    q=query,
                    spaces="drive",
                    fields=f"nextPageToken,files({_FIELDS},mimeType)",
                    pageSize=100,
                    pageToken=token,
                    orderBy="name",
                )
            )
            results.extend(
                _to_remote(item)
                for item in page.get("files", [])
                if item.get("mimeType") != FOLDER_MIME
                and item["name"].lower().endswith(".db")
            )
            token = page.get("nextPageToken")
            if not token:
                return results

    def get(self, file_id: str) -> RemoteFile:
        return _to_remote(self._run(self._files.get(fileId=file_id, fields=_FIELDS)))

    def download(self, file_id: str, dest: Path) -> None:
        request = self._files.get_media(fileId=file_id)
        try:
            with dest.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request, chunksize=CHUNK_BYTES)
                done = False
                while not done:
                    _, done = downloader.next_chunk(num_retries=RETRIES)
        except _FAILURES as exc:
            raise _translate(exc) from exc

    def _upload(self, source: Path, make_request: Callable) -> RemoteFile:
        # El archivo se abre y se cierra aquí: en Windows, un manejador abierto
        # impediría borrar el snapshot temporal.
        with source.open("rb") as handle:
            media = MediaIoBaseUpload(
                handle, mimetype=SQLITE_MIME, chunksize=CHUNK_BYTES, resumable=True
            )
            request = make_request(media)
            try:
                response = None
                while response is None:
                    _, response = request.next_chunk(num_retries=RETRIES)
            except _FAILURES as exc:
                raise _translate(exc) from exc
        return _to_remote(response)

    def create(self, source: Path, name: str, folder_id: str, entorno_id: str) -> RemoteFile:
        body = {
            "name": name,
            "parents": [folder_id],
            "appProperties": {ENV_PROPERTY: entorno_id},
        }
        return self._upload(
            source,
            lambda media: self._files.create(body=body, media_body=media, fields=_FIELDS),
        )

    def update(self, file_id: str, source: Path) -> RemoteFile:
        return self._upload(
            source,
            lambda media: self._files.update(fileId=file_id, media_body=media, fields=_FIELDS),
        )

    def tag_environment(self, file_id: str, entorno_id: str) -> RemoteFile:
        return _to_remote(
            self._run(
                self._files.update(
                    fileId=file_id,
                    body={"appProperties": {ENV_PROPERTY: entorno_id}},
                    fields=_FIELDS,
                )
            )
        )
