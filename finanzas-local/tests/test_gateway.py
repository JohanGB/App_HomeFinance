"""Prueba las solicitudes HTTP reales que arma la librería de Google, sin red."""

import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httplib2
import pytest
from googleapiclient.discovery import build

from finanzas.services.drive_gateway import (
    ENV_PROPERTY,
    DriveAccessError,
    GoogleDriveGateway,
    OfflineError,
    RemoteNotFoundError,
    escape_query,
)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Sin esto, los reintentos de la librería esperan varios segundos reales."""
    monkeypatch.setattr("time.sleep", lambda *_: None)


class RecordingHttp:
    """Responde en orden y guarda lo que recibió."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.timeout = None

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.requests.append({"uri": uri, "method": method, "body": body, "headers": headers or {}})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, content, extra = (*item, {})[:3]
        if isinstance(content, (dict, list)):
            content = json.dumps(content)
        data = content if isinstance(content, bytes) else content.encode()
        return httplib2.Response({"status": str(status), **extra}), data

    def query(self, index=0):
        return parse_qs(urlparse(self.requests[index]["uri"]).query)


def gateway(*responses):
    http = RecordingHttp(*responses)
    service = build("drive", "v3", http=http, static_discovery=True, developerKey=None)
    return GoogleDriveGateway(service), http


def test_list_filters_extension_in_python_and_paginates():
    gw, http = gateway(
        (200, {"nextPageToken": "t2", "files": [
            {"id": "1", "name": "gastos_casa.db", "mimeType": "application/octet-stream", "headRevisionId": "r"},
            {"id": "2", "name": "notas.txt", "mimeType": "text/plain"},
        ]}),
        (200, {"files": [
            {"id": "3", "name": "PRIVADOS.DB", "mimeType": "application/vnd.sqlite3"},
            {"id": "4", "name": "carpeta.db", "mimeType": "application/vnd.google-apps.folder"},
        ]}),
    )
    assert [f.id for f in gw.list_databases("folder1")] == ["1", "3"]
    q = http.query(0)["q"][0]
    assert q == "'folder1' in parents and trashed = false"
    assert "contains" not in q
    assert http.query(1)["pageToken"] == ["t2"]


def test_ensure_folder_reuses_existing():
    gw, http = gateway((200, {"files": [{"id": "abc"}]}))
    assert gw.ensure_app_folder() == "abc"
    assert len(http.requests) == 1


def test_ensure_folder_creates_when_missing():
    gw, http = gateway((200, {"files": []}), (200, {"id": "new"}))
    assert gw.ensure_app_folder() == "new"
    body = json.loads(http.requests[1]["body"])
    assert body["mimeType"] == "application/vnd.google-apps.folder"


def test_get_requests_only_needed_fields():
    gw, http = gateway((200, {
        "id": "1", "name": "a.db", "headRevisionId": "rev", "md5Checksum": "m",
        "size": "2048", "appProperties": {ENV_PROPERTY: "env"}}))
    remote = gw.get("1")
    assert (remote.head_revision, remote.md5, remote.size, remote.entorno_id) == ("rev", "m", 2048, "env")
    assert "headRevisionId" in http.query()["fields"][0]


def test_query_escaping():
    assert escape_query("d'Art\\e") == "d\\'Art\\\\e"


def test_404_and_403_are_translated():
    gw, _ = gateway((404, {"error": {"message": "nope"}}))
    with pytest.raises(RemoteNotFoundError):
        gw.get("1")
    gw, _ = gateway((403, {"error": {"message": "quota"}}))
    with pytest.raises(DriveAccessError):
        gw.get("1")


def test_network_failure_becomes_offline():
    gw, _ = gateway(*[httplib2.ServerNotFoundError("dns")] * 5)
    with pytest.raises(OfflineError):
        gw.get("1")


def test_expired_authorization_is_reported_as_access_problem(monkeypatch):
    from google.auth.exceptions import RefreshError
    gw, _ = gateway(RefreshError("invalid_grant"))
    with pytest.raises(DriveAccessError, match="venció"):
        gw.get("1")


def test_create_uses_resumable_upload_with_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    source = tmp_path / "s.db"
    source.write_bytes(b"x" * 1000)
    md5 = hashlib.md5(source.read_bytes()).hexdigest()
    gw, http = gateway(
        (200, {}, {"location": "https://upload.example/session"}),
        (200, {"id": "9", "name": "a.db", "headRevisionId": "r1", "md5Checksum": md5}),
    )
    remote = gw.create(source, "a.db", "folder1", "entorno-1")
    assert remote.id == "9" and remote.md5 == md5
    start = http.requests[0]
    assert "uploadType=resumable" in start["uri"]
    body = json.loads(start["body"])
    assert body["parents"] == ["folder1"]
    assert body["appProperties"] == {ENV_PROPERTY: "entorno-1"}
    assert http.requests[1]["uri"] == "https://upload.example/session"


def test_resumable_update_sends_all_chunks(tmp_path):
    source = tmp_path / "s.db"
    source.write_bytes(b"y" * (2 * 1024 * 1024 + 5))
    gw, http = gateway(
        (200, {}, {"location": "https://upload.example/s"}),
        (308, b"", {"range": "0-1048575"}),
        (308, b"", {"range": "0-2097151"}),
        (200, {"id": "1", "name": "a.db", "headRevisionId": "r2"}),
    )
    assert gw.update("1", source).head_revision == "r2"
    assert len(http.requests) == 4


def test_download_writes_all_chunks(tmp_path):
    dest = tmp_path / "d.db"
    gw, _ = gateway(
        (206, b"ab", {"content-range": "bytes 0-1/4", "content-length": "2"}),
        (206, b"cd", {"content-range": "bytes 2-3/4", "content-length": "2"}),
    )
    gw.download("1", dest)
    assert dest.read_bytes() == b"abcd"
