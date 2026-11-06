import json
import sqlite3
from decimal import Decimal

import pytest

from finanzas.models.database import (
    CorruptDatabaseError,
    Database,
    EnvironmentClosedError,
    ReadOnlyError,
    SchemaTooNewError,
    verify_file,
)
from finanzas.models.entities import ExpenseInput, ItemInput, ValidationError, to_cents
from finanzas.models.repository import Repository, place_key
from finanzas.models.security import (
    CredentialError,
    EditSession,
    LockedOutError,
    SecurityManager,
    WrongSecretError,
)


def gasto(total="10000", lugar="Éxito", when="2026-11-03T10:00:00", **kw):
    return ExpenseInput(total=total, lugar=lugar, ocurrido_en=when, **kw)


# ------------------------------------------------------------------- formulario
def test_only_total_and_place_are_required(repo):
    gasto_id = repo.add_expense(ExpenseInput(total="25000", lugar="Tienda"))
    expense = repo.get_expense(gasto_id)
    assert expense.total_cent == 2_500_000
    assert len(expense.ocurrido_en) == 19 and expense.nota is None and expense.items == ()


@pytest.mark.parametrize("total,lugar", [("0", "x"), ("-5", "x"), ("10", ""), ("10", "   "), ("abc", "x"), ("NaN", "x")])
def test_rejects_invalid_form(repo, total, lugar):
    with pytest.raises(ValidationError):
        repo.add_expense(ExpenseInput(total=total, lugar=lugar))


@pytest.mark.parametrize("mes", ["2026-13", "2026-00", "2026-1", "2026-11\n", "26-11"])
def test_rejects_invalid_month(repo, mes):
    with pytest.raises(ValidationError):
        repo.scatter_points(mes)


def test_rejects_impossible_date(repo):
    with pytest.raises(ValidationError):
        repo.add_expense(gasto(when="2026-02-30T10:00:00"))


def test_schema_rejects_bad_data_even_without_the_repository(db):
    with db._transaction() as conn:
        conn.execute("INSERT INTO lugar (nombre, clave) VALUES ('a', 'a')")
    for row in [
        (1, 12.5, "2026-11-03T10:00:00"),          # float
        (1, 100, "2026-02-30T10:00:00"),           # 30 de febrero
        (1, 100, "03/11/2026"),                    # formato
        (99, 100, "2026-11-03T10:00:00"),          # lugar inexistente
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            with db._transaction() as conn:
                conn.execute(
                    "INSERT INTO gasto (lugar_id, total_cent, ocurrido_en, creado_en, actualizado_en)"
                    " VALUES (?, ?, ?, 'x', 'x')", row)


def test_items_are_stored_and_compared_with_total(repo):
    gasto_id = repo.add_expense(gasto(total="11000", items=(
        ItemInput("Leche", "4000", 2), ItemInput("Pan", "2500"))))
    expense = repo.get_expense(gasto_id)
    assert [i.nombre for i in expense.items] == ["Leche", "Pan"]
    assert expense.items_cent == 1_050_000
    assert expense.sin_detallar_cent == 50_000


def test_failed_item_rolls_back_whole_expense(repo, db):
    with pytest.raises(ValidationError):
        repo.add_expense(gasto(items=(ItemInput("", "10"),)))
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM gasto").fetchone()[0] == 0


def test_money_never_uses_float():
    assert to_cents("0.07") == 7
    assert to_cents(Decimal("19.995")) == 2000
    with pytest.raises(ValidationError):
        to_cents(0.07)


def test_places_deduplicate_accents_and_case(repo, db):
    for name in ("Éxito", "exito", "  EXITO "):
        repo.add_expense(gasto(lugar=name))
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM lugar").fetchone()[0] == 1
    assert repo.suggest_places("exi") == ["Éxito"]
    assert place_key("Café  Ñandú") == "cafe nandu"


def test_like_wildcards_are_literal_in_suggestions(repo):
    repo.add_expense(gasto(lugar="Tienda"))
    assert repo.suggest_places("%") == []


def test_foreign_keys_cascade_items_and_restrict_places(repo, db, session):
    gasto_id = repo.add_expense(gasto(items=(ItemInput("a", "1"),)))
    with pytest.raises(sqlite3.IntegrityError):
        with db.write(session) as conn:
            conn.execute("DELETE FROM lugar")
    repo.delete_expense(gasto_id, session)
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM gasto_item").fetchone()[0] == 0


# -------------------------------------------------------------------- solo lectura
def test_read_connection_cannot_write_at_sqlite_level(db):
    with db.read() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta VALUES ('x', 'y')")


def test_history_changes_require_unlock(repo):
    gasto_id = repo.add_expense(gasto())
    with pytest.raises(ReadOnlyError):
        repo.update_expense(gasto_id, gasto(total="1"), None)
    with pytest.raises(ReadOnlyError):
        repo.delete_expense(gasto_id, None)
    with pytest.raises(ReadOnlyError):
        repo.set_budget("2026-11", "100", None)


def test_capture_policy_can_require_pin(repo, db, session):
    with db.write(session) as conn:
        conn.execute("UPDATE meta SET valor = '1' WHERE clave = 'captura_requiere_pin'")
    with pytest.raises(ReadOnlyError):
        repo.add_expense(gasto())
    assert repo.add_expense(gasto(), session) > 0


def test_session_expires_and_is_bound_to_one_environment(tmp_path, db, security, repo):
    other = Database.create(tmp_path / "otro.db", nombre="Otro")
    clock = {"t": 1000.0}
    session = EditSession(db.entorno_id, 1300.0, lambda: clock["t"])
    with db.write(session):
        pass
    with pytest.raises(ReadOnlyError):
        with other.write(session):
            pass
    clock["t"] = 1300.0
    with pytest.raises(ReadOnlyError):
        with db.write(session):
            pass


def test_revoked_session_is_rejected(db, session):
    session.revoke()
    with pytest.raises(ReadOnlyError):
        with db.write(session):
            pass


# ------------------------------------------------------------------------ bcrypt
def test_hash_is_bcrypt_and_never_plain(db, security):
    with db.read() as conn:
        stored = conn.execute("SELECT hash FROM credencial").fetchone()[0]
    assert stored.startswith(b"$2") and b"4827" not in stored


@pytest.mark.parametrize("bad", ["123", "12345", "abcd", "12 4", "4827\n", "٤٨٢٧"])
def test_rejects_invalid_or_trivial_pin(tmp_path, bad):
    manager = SecurityManager(Database.create(tmp_path / "x.db", nombre="x"), rounds=4)
    with pytest.raises(CredentialError):
        manager.set_initial_credential(bad)


def test_rejects_password_over_72_bytes_counting_utf8(tmp_path):
    manager = SecurityManager(Database.create(tmp_path / "x.db", nombre="x"), rounds=4)
    with pytest.raises(CredentialError):
        manager.set_initial_credential("ñ" * 37, "password")   # 37 caracteres, 74 bytes
    manager.set_initial_credential("ñ" * 36, "password")
    assert manager.unlock("ñ" * 36)


def test_long_guess_fails_without_exception(security):
    with pytest.raises(WrongSecretError):
        security.unlock("x" * 200)


def test_second_initial_credential_is_refused(security):
    with pytest.raises(ReadOnlyError):
        security.set_initial_credential("9513")


def test_change_credential(security):
    security.change_credential("4827", "9513")
    assert security.unlock("9513")
    with pytest.raises(WrongSecretError):
        security.unlock("4827")


def test_lockout_grows_and_survives_correct_pin_during_wait(db):
    clock = {"t": 0.0}
    manager = SecurityManager(db, rounds=4, clock=lambda: clock["t"])
    manager.set_initial_credential("4827")
    for _ in range(4):
        with pytest.raises(WrongSecretError):
            manager.unlock("0001")
    with pytest.raises(LockedOutError) as first:
        manager.unlock("0001")
    assert first.value.seconds == 30
    with pytest.raises(LockedOutError):
        manager.unlock("4827")
    clock["t"] = 31
    with pytest.raises(LockedOutError) as second:
        manager.unlock("0001")
    assert second.value.seconds == 60
    clock["t"] = 100
    assert manager.unlock("4827")


# ---------------------------------------------------------------------- auditoría
def test_audit_keeps_previous_version_with_items(repo, session):
    gasto_id = repo.add_expense(gasto(total="500", items=(ItemInput("Pan", "500"),)))
    repo.update_expense(gasto_id, gasto(total="700"), session)
    repo.delete_expense(gasto_id, session)
    trail = repo.audit_trail(gasto_id)
    assert [t["accion"] for t in trail] == ["DELETE", "UPDATE"]
    before = json.loads(trail[1]["antes"])
    assert before["total_cent"] == 50_000 and before["items"][0]["nombre"] == "Pan"


# --------------------------------------------------------------------- presupuesto
def test_budget_inherits_latest_value_and_can_exceed_100(repo, session):
    repo.set_budget("2026-01", "100", session)
    repo.set_budget("2026-06", "1000", session)
    repo.add_expense(gasto(total="1500", when="2026-07-02T08:00:00"))
    status = repo.budget_status("2026-07")
    assert status.presupuesto_cent == 100_000
    assert status.porcentaje == 150.0
    assert repo.budget_status("2026-03").presupuesto_cent == 10_000
    assert repo.budget_status("2025-12").porcentaje is None


def test_budget_excludes_other_months(repo):
    repo.add_expense(gasto(when="2026-10-31T23:59:59"))
    repo.add_expense(gasto(when="2026-11-01T00:00:00"))
    assert repo.budget_status("2026-11").gastado_cent == 1_000_000


# ------------------------------------------------------------------------- scatter
def test_scatter_keeps_each_distinct_transaction(repo):
    repo.add_expense(gasto(total="5000", when="2026-11-03T08:00:00"))
    repo.add_expense(gasto(total="90000", when="2026-11-03T09:00:00"))
    repo.add_expense(gasto(total="5000", when="2026-11-04T09:00:00"))
    points = repo.scatter_points("2026-11")
    assert [(p.dia, p.monto_cent, p.transacciones) for p in points] == [
        (3, 500_000, 1), (3, 9_000_000, 1), (4, 500_000, 1)]


def test_scatter_merges_near_equal_amounts_same_day(repo):
    repo.add_expense(gasto(total="12340", when="2026-11-03T08:00:00"))
    repo.add_expense(gasto(total="12380", when="2026-11-03T18:00:00"))
    assert [(p.monto_cent, p.transacciones) for p in repo.scatter_points("2026-11")] == [(1_236_000, 2)]


def test_scatter_keeps_small_and_large_amounts_apart(repo):
    """Un arriendo alto no puede tragarse los cafés del mismo día."""
    repo.add_expense(gasto(total="2000000", when="2026-11-03T08:00:00"))
    repo.add_expense(gasto(total="4500", when="2026-11-03T09:00:00"))
    repo.add_expense(gasto(total="5200", when="2026-11-03T10:00:00"))
    assert len(repo.scatter_points("2026-11")) == 3


def test_scatter_with_short_amounts(repo):
    repo.add_expense(gasto(total="0.01", when="2026-11-03T08:00:00"))
    assert repo.scatter_points("2026-11")[0].monto_cent == 1


def test_month_queries_use_covering_index(repo, db):
    from finanzas.models.repository import _SCATTER_SQL, _SPENT_SQL, month_range
    with db.read() as conn:
        for sql in (_SCATTER_SQL, _SPENT_SQL):
            plan = " ".join(
                r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, month_range("2026-11")))
            assert "COVERING INDEX ix_gasto_analitica" in plan, plan


def test_month_range_crosses_year_end():
    from finanzas.models.repository import month_range
    assert month_range("2026-12") == {"desde": "2026-12-01T00:00:00", "hasta": "2027-01-01T00:00:00"}


def test_history_pagination_has_no_gaps(repo):
    ids = [repo.add_expense(gasto(when=f"2026-11-{d:02d}T10:00:00")) for d in range(1, 8)]
    seen, after = [], None
    while page := repo.list_expenses("2026-11", limit=3, after=after):
        seen += [p.id for p in page]
        after = (page[-1].ocurrido_en, page[-1].id)
    assert seen == ids[::-1]


# ----------------------------------------------------------------- archivo y ciclo
def test_closed_environment_refuses_late_queries(repo, db):
    db.close()
    with pytest.raises(EnvironmentClosedError):
        repo.budget_status("2026-11")


def test_newer_schema_is_refused(db):
    conn = sqlite3.connect(db.path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaTooNewError):
        Database.open(db.path)


def test_create_refuses_existing_file(db):
    with pytest.raises(Exception):
        Database.create(db.path, nombre="otro")


def test_reopen_keeps_data(tmp_path, repo, db):
    repo.add_expense(gasto())
    assert Repository(Database.open(db.path)).budget_status("2026-11").gastado_cent == 1_000_000


def test_path_with_special_characters(tmp_path):
    # Válidos en Windows; '#', '%' y los espacios rompen una URI mal escapada.
    db = Database.create(tmp_path / "mis gastos #1 ñ %20 & más.db", nombre="x")
    Repository(db).add_expense(gasto())
    assert db.path.is_file()


def test_no_wal_sidecars(repo, db):
    repo.add_expense(gasto())
    assert sorted(p.name for p in db.path.parent.iterdir()) == ["gastos.db"]


def test_snapshot_is_deterministic_and_changes_with_data(tmp_path, repo, db):
    a, b, c = (tmp_path / n for n in ("a.bin", "b.bin", "c.bin"))
    db.snapshot(a), db.snapshot(b)
    assert a.read_bytes() == b.read_bytes()
    repo.add_expense(gasto())
    db.snapshot(c)
    assert c.read_bytes() != a.read_bytes()


def test_verify_detects_garbage_and_truncation(tmp_path, repo, db):
    repo.add_expense(gasto())
    assert verify_file(db.path) == db.entorno_id
    garbage = tmp_path / "g.db"
    garbage.write_bytes(b"esto no es sqlite" * 100)
    with pytest.raises(CorruptDatabaseError):
        verify_file(garbage)
    truncated = tmp_path / "t.db"
    truncated.write_bytes(db.path.read_bytes()[:6000])
    with pytest.raises(CorruptDatabaseError):
        verify_file(truncated)


# ------------------------------------------------------------ esquema v2 (categoría)
from datetime import date  # noqa: E402

from finanzas.models.database import SCHEMA_VERSION  # noqa: E402
from finanzas.models.repository import _BY_SQL, _DAILY_SQL, _MONTHLY_SQL, period_range  # noqa: E402


def make_v1(path):
    """Archivo idéntico al que creaba la versión anterior de la app."""
    from importlib import resources
    conn = sqlite3.connect(path)
    conn.executescript(resources.files("finanzas.models").joinpath("schema.sql").read_text("utf-8"))
    conn.executescript(
        "PRAGMA user_version = 1;"
        "INSERT INTO meta VALUES ('entorno_id','v1env'),('nombre','viejo'),('moneda','COP'),"
        "('captura_requiere_pin','0'),('creado_en','x');"
        "INSERT INTO lugar (id, nombre, clave) VALUES (1, 'Éxito', 'exito');"
        "INSERT INTO gasto (lugar_id, total_cent, ocurrido_en, creado_en, actualizado_en) "
        "VALUES (1, 500000, '2026-10-05T14:30:00', 'x', 'x');"
    )
    conn.commit()
    conn.close()


def test_v1_file_migrates_and_keeps_data(tmp_path):
    path = tmp_path / "viejo.db"
    make_v1(path)
    db = Database.open(path)
    with db.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    repo = Repository(db)
    assert repo.budget_status("2026-10").gastado_cent == 500_000
    assert repo.get_expense(1).categoria is None
    assert verify_file(path) == "v1env"


def test_migration_is_idempotent_on_reopen(tmp_path):
    path = tmp_path / "viejo.db"
    make_v1(path)
    Database.open(path)
    Database.open(path)


def test_new_and_migrated_schemas_are_identical(tmp_path):
    migrated = tmp_path / "viejo.db"
    make_v1(migrated)
    Database.open(migrated)
    fresh = Database.create(tmp_path / "nuevo.db", nombre="n").path

    def shape(path):
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        conn.close()
        return rows

    assert shape(migrated) == shape(fresh)


def test_category_is_optional_and_deduplicated(repo, db):
    a = repo.add_expense(gasto(categoria="Mercado"))
    repo.add_expense(gasto(categoria="  mercado "))
    b = repo.add_expense(gasto())
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM categoria").fetchone()[0] == 1
    assert repo.get_expense(a).categoria == "Mercado"
    assert repo.get_expense(b).categoria is None
    assert repo.list_expenses("2026-11")[0].categoria is None


def test_category_update_is_audited(repo, session):
    gasto_id = repo.add_expense(gasto(categoria="Mercado"))
    repo.update_expense(gasto_id, gasto(categoria="Hogar"), session)
    assert json.loads(repo.audit_trail(gasto_id)[0]["antes"])["categoria"] == "Mercado"
    assert repo.get_expense(gasto_id).categoria == "Hogar"


def test_category_suggestions_rank_by_use(repo):
    for cat in ["Transporte", "Mercado", "Mercado", "Mercado", "Transporte", "Ocio"]:
        repo.add_expense(gasto(categoria=cat))
    assert repo.suggest_categories() == ["Mercado", "Transporte", "Ocio"]
    assert repo.suggest_categories("tra") == ["Transporte"]


def test_date_without_time_is_stored_at_midnight(repo):
    gasto_id = repo.add_expense(ExpenseInput("10", "x", ocurrido_en=date(2026, 11, 3)))
    assert repo.get_expense(gasto_id).ocurrido_en == "2026-11-03T00:00:00"


# --------------------------------------------------------------------- análisis
def test_daily_totals_consolidate_each_day(repo):
    repo.add_expense(gasto(total="1000", when="2026-11-03T08:00:00"))
    repo.add_expense(gasto(total="2500", when="2026-11-03T00:00:00"))
    repo.add_expense(gasto(total="700", when="2026-11-10T00:00:00"))
    repo.add_expense(gasto(total="999", when="2026-10-31T00:00:00"))
    assert [(t.clave, t.total_cent, t.transacciones) for t in repo.daily_totals("2026-11")] == [
        (3, 350_000, 2), (10, 70_000, 1)]


def test_monthly_totals_for_a_year(repo):
    repo.add_expense(gasto(total="100", when="2026-01-15T00:00:00"))
    repo.add_expense(gasto(total="200", when="2026-01-20T00:00:00"))
    repo.add_expense(gasto(total="300", when="2026-12-31T00:00:00"))
    repo.add_expense(gasto(total="999", when="2027-01-01T00:00:00"))
    assert [(t.clave, t.total_cent) for t in repo.monthly_totals(2026)] == [(1, 30_000), (12, 30_000)]


def test_totals_by_category_and_place(repo):
    repo.add_expense(gasto(total="100", lugar="D1", categoria="Mercado"))
    repo.add_expense(gasto(total="300", lugar="Éxito", categoria="mercado"))
    repo.add_expense(gasto(total="50", lugar="Uber", categoria="Transporte"))
    repo.add_expense(gasto(total="20", lugar="exito"))
    cats = repo.totals_by("categoria", "2026-11")
    assert [(t.etiqueta, t.total_cent, t.transacciones) for t in cats] == [
        ("Mercado", 40_000, 2), ("Transporte", 5_000, 1), ("Sin categoría", 2_000, 1)]
    places = repo.totals_by("lugar", "2026")
    assert [(t.etiqueta, t.total_cent) for t in places] == [
        ("Éxito", 32_000), ("D1", 10_000), ("Uber", 5_000)]


def test_totals_by_product_merges_spelling(repo):
    repo.add_expense(gasto(total="20000", items=(ItemInput("Café", "8000", 2),)))
    repo.add_expense(gasto(total="5000", items=(ItemInput("cafe", "3000"), ItemInput("Pan", "2000"))))
    repo.add_expense(gasto(total="9000"))
    products = repo.totals_by("producto", "2026-11")
    assert [(t.etiqueta, t.total_cent, t.transacciones) for t in products] == [
        ("Café", 1_900_000, 2), ("Pan", 200_000, 1)]


def test_totals_by_groups_the_tail_as_others(repo):
    for i in range(12):
        repo.add_expense(gasto(total=str(100 + i), lugar=f"Lugar {i:02d}"))
    totals = repo.totals_by("lugar", "2026-11", top=5)
    assert len(totals) == 5
    assert totals[-1].clave == "Otros" and totals[-1].etiqueta == "Otros (8)"
    assert sum(t.total_cent for t in totals) == sum(100 + i for i in range(12)) * 100


def test_totals_by_rejects_unknown_dimension(repo):
    with pytest.raises(ValidationError):
        repo.totals_by("lugar; DROP TABLE gasto", "2026-11")


def test_period_range_year_and_month():
    assert period_range("2026") == {"desde": "2026-01-01T00:00:00", "hasta": "2027-01-01T00:00:00"}
    assert period_range("2026-11")["hasta"] == "2026-12-01T00:00:00"


def test_analysis_queries_use_covering_index(db):
    with db.read() as conn:
        for sql in (_DAILY_SQL, _MONTHLY_SQL, _BY_SQL["lugar"], _BY_SQL["categoria"]):
            plan = " ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, period_range("2026")))
            assert "COVERING INDEX ix_gasto_analitica" in plan, plan
