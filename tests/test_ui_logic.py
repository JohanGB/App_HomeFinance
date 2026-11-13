"""Formato, color y controlador, sin Flet."""

import threading
from decimal import Decimal

import pytest

from finanzas.models.database import ReadOnlyError
from finanzas.models.entities import ExpenseInput, ItemInput, ValidationError
from finanzas.services.sync_engine import Resolution, SyncStatus
from finanzas.ui import formatting as fmt
from finanzas.ui.controller import AppController, LazyGateway, NotLoggedIn, describe_sync

from test_sync import FakeDrive


# ---------------------------------------------------------------------- dinero
@pytest.mark.parametrize("text,value", [
    ("25000", "25000"), ("25.000", "25000"), ("$ 1.500.000", "1500000"),
    ("1.234,56", "1234.56"), ("1,234.56", "1234.56"), ("12,5", "12.5"),
    ("12.5", "12.5"), ("12.50", "12.50"), ("1,500", "1500"), ("0,99", "0.99"),
    (" 3 500 ", "3500"),
])
def test_parse_money_colombian(text, value):
    assert fmt.parse_money(text) == Decimal(value)


@pytest.mark.parametrize("text", ["", "abc", "1.2.3", "-5", "1e5", "12,3,4"])
def test_parse_money_rejects(text):
    with pytest.raises(ValidationError):
        fmt.parse_money(text)


def test_format_money():
    assert fmt.format_money(150_000_000) == "$ 1.500.000"
    assert fmt.format_money(123_456) == "$ 1.234,56"
    assert fmt.format_money(0) == "$ 0"
    assert fmt.money_input(250_000) == "2.500"
    assert fmt.parse_money(fmt.money_input(123_456)) == Decimal("1234.56")


def test_format_compact():
    assert fmt.format_compact(1_200_000) == "1,2 M"
    assert fmt.format_compact(45_000) == "45 mil"
    assert fmt.format_compact(2_000_000) == "2 M"
    assert fmt.format_compact(900) == "900"


def test_months():
    assert fmt.shift_month("2026-12", 1) == "2027-01"
    assert fmt.shift_month("2026-01", -1) == "2025-12"
    assert fmt.month_label("2026-11") == "Noviembre 2026"
    assert fmt.days_in_month("2028-02") == 29
    assert fmt.format_when("2026-11-03T14:05:00") == "3 nov"
    assert fmt.format_date_long("2026-11-03T00:00:00") == "3 de noviembre de 2026"
    assert fmt.date_input("2026-11-03T14:05:00") == "03/11/2026"


@pytest.mark.parametrize("text,expected", [
    ("03/11/2026", (2026, 11, 3)), ("3/11/2026", (2026, 11, 3)), ("3-11-2026", (2026, 11, 3)),
    ("2026-11-03", (2026, 11, 3)), ("29/02/2028", (2028, 2, 29)),
])
def test_parse_date(text, expected):
    from datetime import date
    assert fmt.parse_date(text) == date(*expected)
    assert fmt.parse_date("") is None


@pytest.mark.parametrize("text", ["31/02/2026", "11/2026", "2026/11/03", "hoy"])
def test_parse_date_rejects(text):
    with pytest.raises(ValidationError):
        fmt.parse_date(text)


# --------------------------------------------------------------- presupuesto
def hue_band(hue):
    if 90 <= hue <= 150:
        return "verde"
    if 30 <= hue <= 65:
        return "amarillo"
    if 0 <= hue <= 20:
        return "rojo"
    return "?"


@pytest.mark.parametrize("pct,band", [
    (0, "verde"), (25, "verde"), (49.9, "verde"),
    (50, "amarillo"), (70, "amarillo"), (84.9, "amarillo"),
    (85, "rojo"), (100, "rojo"), (250, "rojo"),
])
def test_budget_bands_follow_spec(pct, band):
    assert hue_band(fmt.budget_hue(pct)) == band


def test_budget_hue_moves_continuously_inside_each_band():
    hues = [fmt.budget_hue(p) for p in range(0, 50)]
    assert all(a > b for a, b in zip(hues, hues[1:]))
    assert fmt.budget_hue(100) == fmt.budget_hue(400) == 0


def test_budget_color_format():
    assert fmt.budget_color(None) == "#8A8F98"
    for pct in (0, 60, 90):
        color = fmt.budget_color(pct)
        assert color.startswith("#") and len(color) == 7


# --------------------------------------------------------------- controlador
@pytest.fixture
def ctrl(tmp_path):
    controller = AppController(tmp_path / "app", gateway=FakeDrive(), mode="desktop")
    remote = controller.create_environment("casa")
    controller.open_environment(remote)
    controller.engine.active.security.rounds = 4
    return controller


def test_first_open_requires_pin_setup(ctrl):
    assert ctrl.needs_pin_setup()
    ctrl.setup_pin("4827")
    assert not ctrl.needs_pin_setup()


def test_capture_without_pin_but_edits_need_it(ctrl):
    ctrl.setup_pin("4827")
    gasto_id = ctrl.add_expense(ExpenseInput("25.000".replace(".", ""), "Tienda"))
    with pytest.raises(ReadOnlyError):
        ctrl.delete_expense(gasto_id)
    ctrl.unlock("4827")
    assert ctrl.is_unlocked
    ctrl.update_expense(gasto_id, ExpenseInput("30000", "Tienda", items=(ItemInput("a", "1"),)))
    ctrl.lock_editing()
    assert not ctrl.is_unlocked


def test_switching_environment_locks_editing(ctrl):
    ctrl.setup_pin("4827")
    ctrl.unlock("4827")
    other = ctrl.create_environment("privado")
    ctrl.open_environment(other)
    assert not ctrl.is_unlocked


def test_dashboard_pages_and_generation(ctrl):
    for day in range(1, 6):
        ctrl.add_expense(ExpenseInput("1000", "x", ocurrido_en=f"2026-11-{day:02d}T10:00:00"))
    board = ctrl.dashboard("2026-11", limit=3)
    assert len(board.expenses) == 3 and board.has_more
    assert board.budget.gastado_cent == 500_000
    assert board.generation == ctrl.env.generation
    assert not ctrl.dashboard("2026-11", limit=5).has_more


def test_catalog_falls_back_to_local_copies_when_offline(ctrl):
    ctrl.add_expense(ExpenseInput("1000", "x"))
    ctrl.sync_on_exit()
    ctrl.gateway.offline = True
    catalog = ctrl.catalog()
    assert not catalog.online and [e.name for e in catalog.environments] == ["casa.db"]
    env = ctrl.open_environment(catalog.environments[0])
    assert env.sync.status is SyncStatus.OFFLINE


def test_background_sync_uploads_only_with_changes(ctrl):
    assert ctrl.sync_in_background() is None
    ctrl.add_expense(ExpenseInput("1000", "x"))
    assert ctrl.sync_in_background().status is SyncStatus.UPLOADED
    assert ctrl.env is not None
    assert ctrl.sync_in_background() is None


def test_resolve_conflict_reopens_same_environment(ctrl, tmp_path):
    drive = ctrl.gateway
    remote_id = ctrl.env.file_id
    ctrl.add_expense(ExpenseInput("1000", "x"))
    ctrl.sync_on_exit()
    ctrl.open_environment(drive.get(remote_id))
    other = AppController(tmp_path / "pc2", gateway=drive, mode="desktop")
    other.open_environment(drive.get(remote_id))
    other.add_expense(ExpenseInput("2000", "y"))
    other.sync_on_exit()
    ctrl.add_expense(ExpenseInput("5000", "z"))
    assert ctrl.sync_in_background().status is SyncStatus.CONFLICT
    env = ctrl.resolve_conflict(Resolution.KEEP_BOTH)
    assert env.file_id == remote_id
    assert ctrl.dashboard(fmt.current_month()).budget.gastado_cent == 300_000


def test_lazy_gateway_without_login_behaves_offline(tmp_path):
    gateway = LazyGateway(factory=lambda creds: FakeDrive())
    with pytest.raises(NotLoggedIn):
        gateway.get("x")
    controller = AppController(tmp_path, gateway=gateway, mode="desktop")
    catalog = controller.catalog()
    assert catalog.environments == [] and not catalog.online and catalog.error == ""
    gateway.connect(object())
    assert controller.catalog().online


def test_lock_serializes_close_and_switch(ctrl):
    ctrl.add_expense(ExpenseInput("1000", "x"))
    other = ctrl.create_environment("b")
    errors = []

    def run(fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # pragma: no cover - solo si falla
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(ctrl.sync_on_exit,)),
               threading.Thread(target=run, args=(ctrl.open_environment, other))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_describe_sync_texts():
    assert describe_sync(None)[0] == "ok"
    from finanzas.services.sync_engine import SyncResult
    assert describe_sync(SyncResult(SyncStatus.CONFLICT, "x")) == ("conflict", "x")


def test_analysis_views(ctrl):
    for day, cat, amount in [(1, "Mercado", "1000"), (1, "Mercado", "500"), (2, "Transporte", "300")]:
        ctrl.add_expense(ExpenseInput(amount, "x", ocurrido_en=f"2026-11-{day:02d}", categoria=cat))
    ctrl.add_expense(ExpenseInput("700", "x", ocurrido_en="2026-03-15"))
    daily = ctrl.analysis("dia", "2026-11")
    assert daily.kind == "scatter" and [(t.clave, t.total_cent) for t in daily.totals] == [(1, 150_000), (2, 30_000)]
    monthly = ctrl.analysis("mes", "2026-11")
    assert monthly.period == "2026" and [t.clave for t in monthly.totals] == [3, 11]
    bars_month = ctrl.analysis("categoria", "2026-11", "mes")
    assert bars_month.kind == "bars" and [t.etiqueta for t in bars_month.totals] == ["Mercado", "Transporte"]
    bars_year = ctrl.analysis("categoria", "2026-11", "anio")
    assert bars_year.period == "2026" and [t.etiqueta for t in bars_year.totals] == ["Mercado", "Sin categoría", "Transporte"]
    assert ctrl.analysis("lugar", "2026-11").totals[0].total_cent == 180_000
    assert ctrl.suggest_categories() == ["Mercado", "Transporte"]


def test_picked_date_uses_local_day(monkeypatch):
    import os
    import time as _time
    from datetime import date, datetime, timedelta, timezone
    if not hasattr(_time, "tzset"):
        pytest.skip("tzset no existe en Windows")
    monkeypatch.setenv("TZ", "America/Bogota")
    _time.tzset()
    try:
        utc_value = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
        assert fmt.picked_date(utc_value) == date(2026, 11, 2)
        assert fmt.picked_date(datetime(2026, 11, 2)) == date(2026, 11, 2)
        assert fmt.picked_date(date(2026, 11, 2)) == date(2026, 11, 2)
        bogota = timezone(timedelta(hours=-5))
        assert fmt.picked_date(datetime(2026, 11, 2, 0, 0, tzinfo=bogota)) == date(2026, 11, 2)
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()
    assert os.environ.get("TZ") is None


def test_refresh_ignores_controls_not_yet_on_screen():
    """Editar un gasto con ítems fallaba: Flet 1.0 lanza error al leer `.page` antes de tiempo."""
    import flet as ft
    from finanzas.ui.app import _refresh
    column = ft.Column()
    with pytest.raises(RuntimeError):
        column.page
    _refresh(column)


def test_network_errors_become_a_spanish_sentence():
    from finanzas.services.drive_auth import AuthOfflineError
    from finanzas.services.drive_gateway import OfflineError
    from finanzas.ui.app import OFFLINE_MESSAGE, user_message
    raw = ("HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries exceeded "
           "(Caused by NameResolutionError ... [Errno 7] No address associated with hostname)")
    assert user_message(AuthOfflineError(raw)) == OFFLINE_MESSAGE
    assert user_message(OfflineError("timed out")) == OFFLINE_MESSAGE
    assert user_message(ValidationError("Escribe un valor.")) == "Escribe un valor."


def test_bars_keep_detail_of_others(ctrl):
    for i in range(12):
        ctrl.add_expense(ExpenseInput(str(1000 + i * 100), f"Lugar {i:02d}", ocurrido_en="2026-11-02"))
    result = ctrl.analysis("lugar", "2026-11")
    assert len(result.totals) == 8 and result.totals[-1].etiqueta == "Otros (5)"
    assert [t.etiqueta for t in result.others] == [f"Lugar {i:02d}" for i in (4, 3, 2, 1, 0)]
    assert result.totals[-1].total_cent == sum(t.total_cent for t in result.others)
    few = ctrl.analysis("categoria", "2026-11")
    assert few.others == []


def test_split_top_without_overflow():
    from finanzas.models.entities import Total
    from finanzas.ui.controller import split_top
    totals = [Total(str(i), str(i), 100 - i, 1) for i in range(8)]
    assert split_top(totals) == (totals, [])
