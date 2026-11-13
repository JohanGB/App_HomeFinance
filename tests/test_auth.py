"""Flujo del selector y manejo de tokens, sin Google de verdad."""

import json
import threading
import urllib.parse
import urllib.request
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError

from google_auth_oauthlib.flow import InstalledAppFlow

from finanzas.services import drive_auth
from finanzas.services.drive_auth import PickerCancelled, get_credentials, pick_drive_files


class MemoryStore:
    def __init__(self, token=None):
        self.token = token

    def load(self):
        return self.token

    def save(self, value):
        self.token = value

    def clear(self):
        self.token = None


@pytest.fixture
def secrets(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({"installed": {
        "client_id": "id", "client_secret": "s", "redirect_uris": ["http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token"}}))
    return path


def browser_returns(monkeypatch, query):
    seen = {}

    def fake_open(url):
        seen["url"] = url
        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
        threading.Thread(target=urllib.request.urlopen, args=(redirect + query,), daemon=True).start()
        return True

    monkeypatch.setattr(drive_auth.webbrowser, "open", fake_open)
    return seen


def fake_token(monkeypatch):
    def fetch(self, **kwargs):
        self.oauth2session.token = {"access_token": "a", "refresh_token": "nuevo", "expires_at": 4102444800}
        return self.oauth2session.token

    monkeypatch.setattr(InstalledAppFlow, "fetch_token", fetch)


def test_picker_returns_ids_and_saves_token(monkeypatch, secrets):
    seen = browser_returns(monkeypatch, "?code=c&picked_file_ids=f1,f2")
    fake_token(monkeypatch)
    store = MemoryStore()
    _, ids = pick_drive_files(secrets, store)
    assert ids == ["f1", "f2"] and store.token == "nuevo"
    assert "trigger_onepick=true" in seen["url"]
    assert "drive.file" in seen["url"] and "prompt=consent" in seen["url"]


@pytest.mark.parametrize("query", ["?error=access_denied", "?code=c", "?code=c&picked_file_ids="])
def test_picker_cancelled(monkeypatch, secrets, query):
    browser_returns(monkeypatch, query)
    with pytest.raises(PickerCancelled):
        pick_drive_files(secrets, MemoryStore())


def test_saved_token_is_reused_without_browser(monkeypatch, secrets):
    monkeypatch.setattr(drive_auth.Credentials, "refresh", lambda self, request: None)
    browser = MagicMock()
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", browser)
    credentials = get_credentials(secrets, MemoryStore("guardado"))
    assert credentials.refresh_token == "guardado" and not browser.called


def test_expired_token_falls_back_to_consent(monkeypatch, secrets):
    def refuse(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(drive_auth.Credentials, "refresh", refuse)
    fresh = MagicMock(refresh_token="nuevo")
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", lambda self, **k: fresh)
    store = MemoryStore("vencido")
    assert get_credentials(secrets, store) is fresh and store.token == "nuevo"


def test_only_the_narrow_scope_is_requested():
    assert drive_auth.SCOPES == ["https://www.googleapis.com/auth/drive.file"]


# ------------------------------------------------------------ flujo de dispositivo
from finanzas.services.drive_auth import (  # noqa: E402
    AuthError,
    AuthOfflineError,
    DeviceFlow,
    FileTokenStore,
    MissingClientSecret,
    auth_mode,
    find_client_secret,
    read_client,
)

CONFIG = {"client_id": "tv.apps", "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token"}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def flow_with(responses, store=None):
    clock = Clock()
    sent = []

    def post(url, data):
        sent.append((url, data))
        return responses.pop(0)

    flow = DeviceFlow(CONFIG, store or MemoryStore(), post=post, clock=clock, sleep=clock.sleep)
    return flow, sent, clock


START = (200, {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device",
               "expires_in": 1800, "interval": 5})


def test_device_flow_requests_drive_file_and_polls_until_approved():
    store = MemoryStore()
    flow, sent, clock = flow_with([
        START,
        (428, {"error": "authorization_pending"}),
        (403, {"error": "slow_down"}),
        (200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600}),
    ], store)
    code = flow.start()
    assert code.user_code == "ABCD-EFGH"
    assert sent[0][1]["scope"] == "https://www.googleapis.com/auth/drive.file"
    credentials = flow.wait(code)
    assert credentials.refresh_token == "r" and store.token == "r"
    assert sent[-1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert clock.t == 5 + 5 + 10   # slow_down suma 5 s al intervalo


@pytest.mark.parametrize("reply,message", [
    ((403, {"error": "access_denied"}), "Rechazaste"),
    ((400, {"error": "invalid_grant"}), "invalid_grant"),
])
def test_device_flow_errors(reply, message):
    flow, _, _ = flow_with([START, reply])
    with pytest.raises(AuthError, match=message):
        flow.wait(flow.start())


def test_device_flow_code_expires():
    flow, _, _ = flow_with([(200, {**START[1], "expires_in": 12})] + [(428, {"error": "authorization_pending"})] * 5)
    with pytest.raises(AuthError, match="venció"):
        flow.wait(flow.start())


def test_device_flow_can_be_cancelled():
    cancel = threading.Event()
    cancel.set()
    flow, _, _ = flow_with([START])
    with pytest.raises(AuthError, match="cancelado"):
        flow.wait(flow.start(), cancel)


def test_device_flow_rejects_wrong_client_type():
    flow, _, _ = flow_with([(400, {"error": "invalid_client"})])
    with pytest.raises(AuthError, match="TV"):
        flow.start()


def test_file_token_store_roundtrip(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    assert store.load() is None
    store.save("abc")
    assert store.load() == "abc"
    store.clear()
    assert store.load() is None


def test_auth_mode_by_platform(monkeypatch):
    monkeypatch.delenv("FINANZAS_AUTH", raising=False)
    monkeypatch.setenv("FLET_PLATFORM", "android")
    assert auth_mode() == "device"
    monkeypatch.setenv("FLET_PLATFORM", "windows")
    assert auth_mode() == "desktop"
    monkeypatch.setenv("FINANZAS_AUTH", "device")
    assert auth_mode() == "device"


def test_find_and_read_client_secret(tmp_path, monkeypatch, secrets):
    # Aislada de src/secrets: allí pueden estar los JSON reales del usuario.
    monkeypatch.setattr(drive_auth, "secrets_dirs", lambda: [tmp_path / "secrets"])
    (tmp_path / "secrets").mkdir()
    with pytest.raises(MissingClientSecret):
        find_client_secret("device")
    (tmp_path / "secrets" / "client_secret_tv.json").write_text(secrets.read_text())
    assert read_client(find_client_secret("device"))["client_id"] == "id"


def test_device_flow_survives_network_loss_while_polling():
    """Android corta la red de la app en segundo plano mientras el usuario aprueba."""
    store = MemoryStore()
    replies = [START, AuthOfflineError("Failed to resolve 'oauth2.googleapis.com'"),
               AuthOfflineError("sin red"),
               (200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})]

    def post(url, data):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    clock = Clock()
    flow = DeviceFlow(CONFIG, store, post=post, clock=clock, sleep=clock.sleep)
    assert flow.wait(flow.start()).refresh_token == "r" and store.token == "r"


def test_device_flow_without_network_until_expiry_reports_expired():
    clock = Clock()
    calls = {"n": 0}

    def post(url, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return (200, {**START[1], "expires_in": 20})
        raise AuthOfflineError("sin red")

    flow = DeviceFlow(CONFIG, MemoryStore(), post=post, clock=clock, sleep=clock.sleep)
    with pytest.raises(AuthError, match="venció"):
        flow.wait(flow.start())
