"""Mide tiempo y memoria de la vista mensual: SQL agregado frente a cargar todo a Pandas.

    python tools/benchmark.py --rows 1000000

Cada escenario corre en un proceso aparte para que el pico de memoria sea el suyo.
Solo Linux: usa `resource`, y `ru_maxrss` se interpreta en KiB.
"""

from __future__ import annotations

import argparse
import random
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MONTH = "2026-06"
YEARS = 10


def build(path: Path, rows: int) -> None:
    from finanzas.models.database import Database

    db = Database.create(path, nombre="benchmark")
    rng = random.Random(7)
    with db._transaction() as conn:
        conn.executemany(
            "INSERT INTO lugar (id, nombre, clave) VALUES (?, ?, ?)",
            [(i, f"Lugar {i}", f"lugar {i}") for i in range(1, 501)],
        )
        conn.executemany(
            "INSERT INTO gasto (lugar_id, total_cent, ocurrido_en, creado_en, actualizado_en) "
            "VALUES (?, ?, ?, 'x', 'x')",
            (
                (
                    rng.randint(1, 500),
                    int(rng.lognormvariate(11.5, 1.2)) + 100,
                    f"{2017 + rng.randrange(YEARS)}-{rng.randint(1, 12):02d}-"
                    f"{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:00",
                    "x",
                )[:3]
                for _ in range(rows)
            ),
        )


def peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def run_sql(path: str) -> None:
    from finanzas.models.database import Database
    from finanzas.models.repository import Repository

    repo = Repository(Database(path))
    base = peak_mb()
    start = time.perf_counter()
    points = repo.scatter_points(MONTH)
    status = repo.budget_status(MONTH)
    elapsed = time.perf_counter() - start
    print(f"sql|{elapsed*1000:.1f}|{base:.0f}|{peak_mb():.0f}|{len(points)}|{status.gastado_cent}")


def run_pandas(path: str) -> None:
    import sqlite3

    import pandas as pd

    base = peak_mb()
    start = time.perf_counter()
    conn = sqlite3.connect(path)
    frame = pd.read_sql_query("SELECT * FROM gasto", conn)
    month = frame[frame["ocurrido_en"].str.startswith(MONTH)]
    spent = int(month["total_cent"].sum())
    elapsed = time.perf_counter() - start
    print(f"pandas|{elapsed*1000:.1f}|{base:.0f}|{peak_mb():.0f}|{len(month)}|{spent}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--worker", choices=["sql", "pandas"])
    parser.add_argument("--db")
    args = parser.parse_args()
    if args.worker:
        {"sql": run_sql, "pandas": run_pandas}[args.worker](args.db)
        return

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "bench.db"
        start = time.perf_counter()
        build(path, args.rows)
        print(f"{args.rows:,} gastos, {path.stat().st_size/1024**2:.0f} MB, "
              f"creados en {time.perf_counter()-start:.0f} s")
        print("método | ms | RSS base MB | pico MB | filas devueltas | gastado")
        for worker in ("sql", "sql", "pandas"):
            out = subprocess.run(
                [sys.executable, __file__, "--worker", worker, "--db", str(path)],
                capture_output=True, text=True, check=True,
            )
            print(out.stdout.strip())


if __name__ == "__main__":
    main()
