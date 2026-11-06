"""Prueba de humo contra Google Drive real. Crea y sincroniza un entorno de prueba.

    python tools/probar_drive.py --paso 1
    python tools/probar_drive.py --paso 2 --datos segundo_pc
    python tools/probar_drive.py --paso vincular

--datos usa otra carpeta local, lo que simula un segundo equipo en esta misma máquina.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finanzas.models.entities import ExpenseInput  # noqa: E402
from finanzas.services.drive_auth import get_credentials, pick_drive_files  # noqa: E402
from finanzas.services.drive_gateway import GoogleDriveGateway  # noqa: E402
from finanzas.services.sync_engine import SyncEngine  # noqa: E402

NOMBRE = "prueba_conexion"
PIN = "4827"


def motor(credenciales, datos: str | None) -> SyncEngine:
    gateway = GoogleDriveGateway.from_credentials(credenciales)
    return SyncEngine(gateway, Path(datos) if datos else None)


def paso_sincronizar(args) -> None:
    engine = motor(get_credentials(args.secrets), args.datos)
    print("Conectado a Drive. Datos locales en:", engine.data_dir)

    existente = next((e for e in engine.list_environments() if e.name == NOMBRE + ".db"), None)
    remoto = existente or engine.create_environment(NOMBRE)
    print("Entorno en Drive:", remoto.name, "(nuevo)" if existente is None else "(ya existía)")

    env = engine.activate(remoto)
    print("Al abrir:", env.sync.status.name, env.sync.detail)

    if not env.security.has_credential():
        env.security.set_initial_credential(PIN)
        print(f"PIN inicial definido: {PIN}")

    env.repo.add_expense(ExpenseInput(total="1000", lugar=f"Prueba paso {args.paso}"))
    mes = date.today().strftime("%Y-%m")
    estado = env.repo.budget_status(mes)
    print(f"Gastado este mes en este equipo: {estado.gastado_cent / 100:,.0f}")

    resultado = engine.close_active()
    print("Al cerrar:", resultado.status.name, resultado.detail)


def paso_vincular(args) -> None:
    credenciales, ids = pick_drive_files(args.secrets)
    engine = motor(credenciales, args.datos)
    for file_id in ids:
        remoto = engine.link_picked_file(file_id)
        print("Vinculado:", remoto.name, "| entorno", remoto.entorno_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paso", choices=["1", "2", "vincular"], required=True)
    parser.add_argument("--secrets", default="client_secret.json")
    parser.add_argument("--datos", help="carpeta local alternativa (simula otro equipo)")
    args = parser.parse_args()
    (paso_vincular if args.paso == "vincular" else paso_sincronizar)(args)


if __name__ == "__main__":
    main()
