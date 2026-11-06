"""Abre la interfaz en el navegador con datos de ejemplo y un Drive en memoria.

    python tools/demo_web.py --port 8550

Sirve para revisar la interfaz sin cuenta de Google. Nada sale del equipo.
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import flet as ft  # noqa: E402

from finanzas.models.entities import ExpenseInput, ItemInput  # noqa: E402
from finanzas.ui.app import FinanzasApp  # noqa: E402
from finanzas.ui.controller import AppController  # noqa: E402
from test_sync import FakeDrive  # noqa: E402

PLACES = {
    "Éxito": "Mercado", "D1": "Mercado", "Ara": "Mercado", "Tienda de la esquina": "Mercado",
    "Juan Valdez": "Restaurantes", "Uber": "Transporte", "Farmatodo": "Salud",
}


def seeded_controller(pct_target: float) -> AppController:
    ctrl = AppController(Path(tempfile.mkdtemp()), gateway=FakeDrive(), mode="desktop")
    for name in ("gastos_casa", "gastos_privados"):
        ctrl.create_environment(name)
    ctrl.open_environment(ctrl.catalog().environments[0])
    ctrl.env.security.rounds = 4
    ctrl.setup_pin("4827")
    ctrl.unlock("4827")
    rng = random.Random(3)
    today = date.today()
    previous = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
    # Mes anterior completo y el actual hasta hoy.
    for month, last_day, target in ((previous, 28, 2_500_000 * pct_target), (today, today.day, 400_000)):
        spent = 0
        while spent < target:
            amount = rng.choice([4500, 8900, 12500, 23000, 38000, 67000, 125000, 210000])
            items = ()
            if rng.random() < 0.3:
                items = (ItemInput("Leche", "4200", 2), ItemInput("Pan", "3500"))
            place = rng.choice(list(PLACES))
            ctrl.add_expense(ExpenseInput(
                str(amount), place,
                ocurrido_en=date(month.year, month.month, rng.randint(1, last_day)),
                items=items, categoria=PLACES[place] if rng.random() < 0.9 else None))
            spent += amount
    ctrl.set_budget(f"{previous:%Y-%m}", "2500000")
    ctrl.lock_editing()
    ctrl.engine.close_active()
    return ctrl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8550)
    parser.add_argument("--pct", type=float, default=0.62)
    args = parser.parse_args()
    ctrl = seeded_controller(args.pct)

    async def app(page: ft.Page) -> None:
        await FinanzasApp(page, ctrl).start()

    ft.run(app, view=None, port=args.port, no_cdn=True)


if __name__ == "__main__":
    main()
