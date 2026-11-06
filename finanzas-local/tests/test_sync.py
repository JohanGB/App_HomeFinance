"""Matriz de sincronización contra un Drive en memoria con dos equipos independientes."""

import hashlib
import itertools
import sqlite3

import pytest

from finanzas.models.database import CorruptDatabaseError, Database, EnvironmentClosedError
from finanzas.models.entities import ExpenseInput
from finanzas.services.drive_gateway import (
    ENV_PROPERTY,
    ChecksumError,
    OfflineError,
    RemoteFile,
    RemoteNotFoundError,
)
from finanzas.services.sync_engine import (
    EnvironmentMismatchError,
    Resolution,
    SyncEngine,
    SyncStatus,
)


class FakeDrive:
    def __init__(self):
        self.files, self.ids, self.revs = {}, itertools.count(1), itertools.count(1)
        self.offline = False
        self.corrupt_upload = False
        self.corrupt_download = False
        self.calls = []

    def _check(self, op):
        self.calls.append(op)
        if self.offline:
            raise OfflineError("sin red")

    def _meta(self, f):
        return RemoteFile(f["id"], f["name"], f["rev"], hashlib.md5(f["data"]).hexdigest(),
                          size=len(f["data"]), trashed=f["trashed"], app_properties=f["props"])

    def ensure_app_folder(self):
        self._check("folder")
        return "carpeta"

    def list_databases(self, folder_id):
        self._check("list")
        return [self._meta(f) for f in self.files.values() if not f["trashed"]]

    def get(self, file_id):
        self._check("get")
        if file_id not in self.files:
            raise RemoteNotFoundError(file_id)
        return self._meta(self.files[file_id])

    def download(self, file_id, dest):
        self._check("download")
        data = self.files[file_id]["data"]
        dest.write_bytes(data[:-1] + b"X" if self.corrupt_download else data)

    def create(self, source, name, folder_id, entorno_id):
        self._check("create")
        fid = f"f{next(self.ids)}"
        self.files[fid] = {"id": fid, "name": name, "data": source.read_bytes(),
                           "rev": f"r{next(self.revs)}", "trashed": False,
                           "props": {ENV_PROPERTY: entorno_id}}
        return self._meta(self.files[fid])

    def update(self, file_id, source):
        self._check("update")
        f = self.files[file_id]
        f["data"] = source.read_bytes() + (b"!" if self.corrupt_upload else b"")
        f["rev"] = f"r{next(self.revs)}"
        return self._meta(f)

    def tag_environment(self, file_id, entorno_id):
        self._check("tag")
        self.files[file_id]["props"][ENV_PROPERTY] = entorno_id
        return self._meta(self.files[file_id])


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
def pc1(drive, tmp_path):
    return SyncEngine(drive, tmp_path / "pc1")


@pytest.fixture
def pc2(drive, tmp_path):
    return SyncEngine(drive, tmp_path / "pc2")


def add(env, total="1000", lugar="Tienda"):
    return env.repo.add_expense(ExpenseInput(total=total, lugar=lugar, ocurrido_en="2026-11-03T10:00:00"))


def spent(env):
    return env.repo.budget_status("2026-11").gastado_cent


def seed_two_pcs(pc1, pc2):
    remote = pc1.create_environment("gastos_casa")
    env = pc1.activate(remote)
    add(env)
    pc1.close_active()
    pc2.activate(remote)
    pc2.close_active()
    return remote


# ------------------------------------------------------------------ cuatro estados
def test_new_environment_gets_db_extension_and_uploads(pc1, drive):
    remote = pc1.create_environment("gastos_casa")
    assert remote.name == "gastos_casa.db"
    assert pc1.local_path(remote.id).is_file()
    assert not list(pc1.temp_dir.iterdir())


def test_nothing_changed_means_no_upload(pc1, drive):
    remote = pc1.create_environment("g")
    pc1.activate(remote)
    drive.calls.clear()
    assert pc1.close_active().status is SyncStatus.UP_TO_DATE
    assert "update" not in drive.calls and "download" not in drive.calls


def test_local_change_uploads_once(pc1, drive):
    remote = pc1.create_environment("g")
    add(pc1.activate(remote))
    assert pc1.close_active().status is SyncStatus.UPLOADED
    pc1.activate(remote)
    assert pc1.close_active().status is SyncStatus.UP_TO_DATE


def test_second_computer_receives_changes(pc1, pc2):
    remote = seed_two_pcs(pc1, pc2)
    env = pc2.activate(remote)
    assert spent(env) == 100_000
    add(env, "500")
    pc2.close_active()
    env = pc1.activate(remote)
    assert env.sync.status is SyncStatus.DOWNLOADED and spent(env) == 150_000


def test_opening_after_a_download_does_not_look_dirty(pc1, pc2, drive):
    remote = seed_two_pcs(pc1, pc2)
    drive.calls.clear()
    pc2.activate(remote)
    pc2.close_active()
    assert "update" not in drive.calls


# ----------------------------------------------------------------------- conflicto
def make_conflict(pc1, pc2, remote):
    add(pc1.activate(remote), "111")
    pc1.close_active()
    db = Database(pc2.local_path(remote.id))
    from finanzas.models.repository import Repository
    Repository(db).add_expense(ExpenseInput("222", "Otra", "2026-11-03T11:00:00"))
    return pc2.sync(remote.id)


def test_conflict_never_overwrites_either_side(pc1, pc2, drive):
    from finanzas.models.repository import Repository
    remote = seed_two_pcs(pc1, pc2)
    assert make_conflict(pc1, pc2, remote).status is SyncStatus.CONFLICT
    # Drive sigue en la revisión de pc1 y pc2 conserva su gasto sin subirlo.
    assert drive.files[remote.id]["rev"] == pc1.state.entry(remote.id).base_revision
    local = Repository(Database(pc2.local_path(remote.id)))
    assert local.budget_status("2026-11").gastado_cent == 100_000 + 22_200


def test_conflict_stays_until_resolved(pc1, pc2):
    remote = seed_two_pcs(pc1, pc2)
    make_conflict(pc1, pc2, remote)
    assert pc2.sync(remote.id).status is SyncStatus.CONFLICT


def test_resolve_keep_local(pc1, pc2):
    remote = seed_two_pcs(pc1, pc2)
    make_conflict(pc1, pc2, remote)
    assert pc2.resolve_conflict(remote.id, Resolution.KEEP_LOCAL).status is SyncStatus.UPLOADED
    env = pc1.activate(remote)
    assert spent(env) == 100_000 + 22_200


def test_resolve_keep_remote_saves_local_backup(pc1, pc2):
    remote = seed_two_pcs(pc1, pc2)
    make_conflict(pc1, pc2, remote)
    assert pc2.resolve_conflict(remote.id, Resolution.KEEP_REMOTE).status is SyncStatus.DOWNLOADED
    assert spent(pc2.activate(pc2.gateway.get(remote.id))) == 100_000 + 11_100
    assert pc2.local_path(remote.id).with_suffix(".bak").is_file()


def test_resolve_keep_both_creates_separate_environment(pc1, pc2, drive):
    remote = seed_two_pcs(pc1, pc2)
    make_conflict(pc1, pc2, remote)
    pc2.resolve_conflict(remote.id, Resolution.KEEP_BOTH)
    envs = pc2.list_environments()
    assert len(envs) == 2
    assert len({e.entorno_id for e in envs}) == 2
    copy = next(e for e in envs if "conflicto" in e.name)
    assert spent(pc2.activate(copy)) == 100_000 + 22_200
    assert spent(pc2.activate(pc2.gateway.get(remote.id))) == 100_000 + 11_100


def test_cannot_resolve_while_environment_is_open(pc1, pc2):
    remote = seed_two_pcs(pc1, pc2)
    pc2.activate(remote)
    with pytest.raises(Exception, match="Cierra"):
        pc2.resolve_conflict(remote.id, Resolution.KEEP_REMOTE)


def test_deleted_or_trashed_remote_is_not_recreated_nor_lost(pc1, drive):
    remote = pc1.create_environment("g")
    add(pc1.activate(remote))
    drive.files[remote.id]["trashed"] = True
    assert pc1.close_active().status is SyncStatus.CONFLICT
    assert pc1.local_path(remote.id).is_file()


def test_closing_does_not_download_remote_changes(pc1, pc2, drive):
    remote = seed_two_pcs(pc1, pc2)
    pc1.activate(remote)
    add(pc2.activate(remote), "500")
    pc2.close_active()
    drive.calls.clear()
    assert pc1.close_active().status is SyncStatus.UP_TO_DATE
    assert "download" not in drive.calls


def test_open_with_stale_dropdown_row_downloads_current_version(pc1, drive):
    stale = pc1.create_environment("g")
    add(pc1.activate(stale))
    pc1.close_active()
    other = SyncEngine(drive, pc1.data_dir.parent / "pc3")
    assert spent(other.activate(stale)) == 100_000


def test_failed_close_still_closes_the_environment(pc1, pc2, drive):
    remote = seed_two_pcs(pc1, pc2)
    env = pc1.activate(remote)
    add(env)
    drive.corrupt_upload = True
    with pytest.raises(ChecksumError):
        pc1.close_active()
    with pytest.raises(EnvironmentClosedError):
        spent(env)


@pytest.mark.parametrize("how", ["deleted", "trashed"])
def test_lost_remote_can_be_republished(pc1, drive, how):
    remote = pc1.create_environment("g")
    add(pc1.activate(remote))
    pc1.close_active()
    if how == "deleted":
        del drive.files[remote.id]
    else:
        drive.files[remote.id]["trashed"] = True
    assert pc1.sync(remote.id).status is SyncStatus.CONFLICT
    with pytest.raises(Exception, match="adoptar"):
        pc1.resolve_conflict(remote.id, Resolution.KEEP_REMOTE)
    assert pc1.resolve_conflict(remote.id, Resolution.KEEP_LOCAL).status is SyncStatus.UPLOADED
    fresh = next(e for e in pc1.list_environments() if e.id != remote.id)
    assert spent(pc1.activate(fresh)) == 100_000
    assert pc1.close_active().status is SyncStatus.UP_TO_DATE


def test_missing_remote_is_reported(pc1, drive):
    remote = pc1.create_environment("g")
    del drive.files[remote.id]
    assert pc1.sync(remote.id).status is SyncStatus.CONFLICT


# ------------------------------------------------------------------ sin red y fallas
def test_offline_close_keeps_changes_for_next_time(pc1, drive):
    remote = pc1.create_environment("g")
    add(pc1.activate(remote))
    drive.offline = True
    assert pc1.close_active().status is SyncStatus.OFFLINE
    drive.offline = False
    pc1.activate(remote)
    assert pc1.close_active().status is SyncStatus.UP_TO_DATE


def test_offline_open_uses_local_copy(pc1, drive):
    remote = pc1.create_environment("g")
    add(pc1.activate(remote))
    pc1.close_active()
    drive.offline = True
    env = pc1.activate(remote)
    assert env.sync.status is SyncStatus.OFFLINE and spent(env) == 100_000


def test_upload_mismatch_does_not_advance_base(pc1, drive):
    remote = pc1.create_environment("g")
    env = pc1.activate(remote)
    add(env)
    base = pc1.state.entry(remote.id).base_fingerprint
    drive.corrupt_upload = True
    with pytest.raises(ChecksumError):
        pc1.sync(remote.id)
    assert pc1.state.entry(remote.id).base_fingerprint == base


def test_damaged_download_leaves_local_file_untouched(pc1, pc2, drive):
    remote = seed_two_pcs(pc1, pc2)
    add(pc2.activate(remote), "500")
    pc2.close_active()
    original = pc1.local_path(remote.id).read_bytes()
    drive.corrupt_download = True
    with pytest.raises(ChecksumError):
        pc1.sync(remote.id)
    assert pc1.local_path(remote.id).read_bytes() == original


def test_valid_md5_but_invalid_sqlite_is_rejected(pc1, pc2, drive):
    remote = seed_two_pcs(pc1, pc2)
    drive.files[remote.id]["data"] = b"no soy una base de datos" * 200
    drive.files[remote.id]["rev"] = "r999"
    original = pc1.local_path(remote.id).read_bytes()
    with pytest.raises(CorruptDatabaseError):
        pc1.sync(remote.id)
    assert pc1.local_path(remote.id).read_bytes() == original


# ---------------------------------------------------------------------- aislamiento
def test_switching_keeps_environments_separate(pc1):
    casa = pc1.create_environment("gastos_casa")
    privado = pc1.create_environment("gastos_privados")
    add(pc1.activate(casa), "100")
    env = pc1.activate(privado)
    add(env, "900")
    assert spent(env) == 90_000
    env = pc1.activate(casa)
    assert spent(env) == 10_000


def test_old_environment_object_cannot_be_used_after_switch(pc1):
    casa = pc1.create_environment("a")
    privado = pc1.create_environment("b")
    old = pc1.activate(casa)
    new = pc1.activate(privado)
    assert new.generation > old.generation
    with pytest.raises(EnvironmentClosedError):
        spent(old)


def test_same_name_in_drive_does_not_collide_locally(pc1):
    a = pc1.create_environment("igual")
    b = pc1.create_environment("igual")
    assert pc1.local_path(a.id) != pc1.local_path(b.id)


def test_switch_syncs_previous_environment(pc1, drive):
    casa = pc1.create_environment("a")
    privado = pc1.create_environment("b")
    add(pc1.activate(casa))
    drive.calls.clear()
    pc1.activate(privado)
    assert "update" in drive.calls


def test_refuses_to_upload_over_another_environment(pc1, drive):
    casa = pc1.create_environment("a")
    env = pc1.activate(casa)
    add(env)
    drive.files[casa.id]["props"][ENV_PROPERTY] = "otro-entorno"
    with pytest.raises(EnvironmentMismatchError):
        pc1.sync(casa.id)


def test_refuses_to_download_a_different_environment(pc1, pc2, drive):
    a = seed_two_pcs(pc1, pc2)
    b = pc1.create_environment("b")
    drive.files[a.id]["data"] = drive.files[b.id]["data"]
    drive.files[a.id]["rev"] = "r999"
    original = pc2.local_path(a.id).read_bytes()
    with pytest.raises(EnvironmentMismatchError):
        pc2.sync(a.id)
    assert pc2.local_path(a.id).read_bytes() == original


def test_link_picked_file_rejects_foreign_sqlite(pc1, drive, tmp_path):
    foreign = tmp_path / "ajeno.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE fotos (id)")
    conn.commit()
    conn.close()
    drive.files["x"] = {"id": "x", "name": "ajeno.db", "data": foreign.read_bytes(),
                        "rev": "r1", "trashed": False, "props": {}}
    with pytest.raises(CorruptDatabaseError):
        pc1.link_picked_file("x")
    assert not pc1.local_path("x").exists()


def test_link_picked_file_adopts_app_database_uploaded_by_hand(pc1, drive, tmp_path):
    made = Database.create(tmp_path / "a_mano.db", nombre="a_mano")
    drive.files["x"] = {"id": "x", "name": "a_mano.db", "data": made.path.read_bytes(),
                        "rev": "r1", "trashed": False, "props": {}}
    remote = pc1.link_picked_file("x")
    assert remote.entorno_id == made.entorno_id
    assert pc1.activate(remote).db.entorno_id == made.entorno_id


def test_lockout_is_stored_on_this_computer_not_in_the_db(pc1, tmp_path):
    remote = pc1.create_environment("g")
    env = pc1.activate(remote)
    env.security.rounds = 4
    env.security.set_initial_credential("4827")
    with pytest.raises(Exception):
        env.security.unlock("0001")
    assert pc1.state.get_attempts(env.db.entorno_id)[0] == 1
    restarted = SyncEngine(pc1.gateway, pc1.data_dir)
    assert restarted.state.get_attempts(env.db.entorno_id)[0] == 1


def test_close_without_local_changes_makes_no_network_call(pc1, drive):
    remote = pc1.create_environment("g")
    pc1.activate(remote)
    drive.calls.clear()
    drive.offline = True
    assert pc1.close_active().status is SyncStatus.UP_TO_DATE
    assert drive.calls == []


def test_has_local_changes_needs_no_network(pc1, drive):
    remote = pc1.create_environment("g")
    env = pc1.activate(remote)
    drive.offline = True
    assert not pc1.has_local_changes(remote.id)
    add(env)
    assert pc1.has_local_changes(remote.id)


def test_known_environments_allow_offline_start(pc1, drive):
    remote = pc1.create_environment("casa")
    add(pc1.activate(remote))
    pc1.close_active()
    drive.offline = True
    restarted = SyncEngine(drive, pc1.data_dir)
    known = restarted.known_environments()
    assert [k.name for k in known] == ["casa.db"]
    env = restarted.activate(known[0])
    assert env.sync.status is SyncStatus.OFFLINE and spent(env) == 100_000


def test_expired_authorization_keeps_changes_for_later(pc1, drive, monkeypatch):
    from finanzas.services.drive_gateway import DriveAccessError
    remote = pc1.create_environment("g")
    add(pc1.activate(remote))
    def refuse(file_id):
        raise DriveAccessError("venció")
    monkeypatch.setattr(drive, "get", refuse)
    assert pc1.close_active().status is SyncStatus.OFFLINE
    assert pc1.has_local_changes(remote.id)
