"""Consultas y reglas de negocio de un entorno."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Sequence

from .database import Database, WriteAuthorization, now_local
from .entities import (
    BudgetStatus,
    Expense,
    ExpenseInput,
    Item,
    ScatterPoint,
    Total,
    ValidationError,
    normalize_timestamp,
    to_cents,
)

_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_YEAR = re.compile(r"[0-9]{4}")
DIMENSIONS = ("categoria", "lugar", "producto")
SIN_CATEGORIA = "Sin categoría"
OTROS = "Otros"

# Agrupa por día y por los tres primeros dígitos del monto. Dos compras de
# $12.340 y $12.380 el mismo día salen como un punto de 2 transacciones. Es una
# operación de texto para no depender de las funciones matemáticas de SQLite,
# que no todas las compilaciones incluyen.
_SCATTER_SQL = """
SELECT CAST(substr(ocurrido_en, 9, 2) AS INTEGER) AS dia,
       CAST(ROUND(AVG(total_cent)) AS INTEGER) AS monto_cent,
       COUNT(*) AS transacciones
  FROM gasto
 WHERE ocurrido_en >= :desde AND ocurrido_en < :hasta
 GROUP BY dia,
          CAST(substr(total_cent, 1, 3)
               || substr('000000000000000000', 1, max(length(total_cent) - 3, 0))
               AS INTEGER)
 ORDER BY dia, monto_cent
"""

_SPENT_SQL = (
    "SELECT COALESCE(SUM(total_cent), 0) FROM gasto "
    "WHERE ocurrido_en >= :desde AND ocurrido_en < :hasta"
)


def place_key(nombre: str) -> str:
    """Clave sin mayúsculas, tildes ni espacios repetidos."""
    decomposed = unicodedata.normalize("NFKD", nombre)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def _check_month(mes: str) -> str:
    if not _MONTH.fullmatch(mes):
        raise ValidationError(f"Mes inválido: {mes!r}. Usa el formato YYYY-MM.")
    return mes


def month_range(mes: str) -> dict[str, str]:
    """Límites del mes como texto ISO: el índice de fechas responde sin leer la tabla."""
    year, month = map(int, _check_month(mes).split("-"))
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return {
        "desde": f"{year:04d}-{month:02d}-01T00:00:00",
        "hasta": f"{next_year:04d}-{next_month:02d}-01T00:00:00",
    }


def period_range(period: str) -> dict[str, str]:
    """'YYYY-MM' es un mes y 'YYYY' un año completo."""
    if _YEAR.fullmatch(period):
        year = int(period)
        return {"desde": f"{year:04d}-01-01T00:00:00", "hasta": f"{year + 1:04d}-01-01T00:00:00"}
    return month_range(period)


# Totales diarios y mensuales: agrupan por un trozo de la fecha del índice.
_DAILY_SQL = """
SELECT CAST(substr(ocurrido_en, 9, 2) AS INTEGER) AS clave,
       SUM(total_cent) AS total_cent, COUNT(*) AS transacciones
  FROM gasto
 WHERE ocurrido_en >= :desde AND ocurrido_en < :hasta
 GROUP BY clave ORDER BY clave
"""
_MONTHLY_SQL = _DAILY_SQL.replace("substr(ocurrido_en, 9, 2)", "substr(ocurrido_en, 6, 2)")

_BY_SQL = {
    "lugar": """
        SELECT l.nombre AS etiqueta, SUM(g.total_cent) AS total_cent, COUNT(*) AS transacciones
          FROM gasto AS g JOIN lugar AS l ON l.id = g.lugar_id
         WHERE g.ocurrido_en >= :desde AND g.ocurrido_en < :hasta
         GROUP BY g.lugar_id""",
    "categoria": f"""
        SELECT COALESCE(c.nombre, '{SIN_CATEGORIA}') AS etiqueta,
               SUM(g.total_cent) AS total_cent, COUNT(*) AS transacciones
          FROM gasto AS g LEFT JOIN categoria AS c ON c.id = g.categoria_id
         WHERE g.ocurrido_en >= :desde AND g.ocurrido_en < :hasta
         GROUP BY g.categoria_id""",
    # Solo la parte detallada en ítems; los nombres se unen después sin tildes ni mayúsculas.
    "producto": """
        SELECT i.nombre AS etiqueta, SUM(i.cantidad * i.valor_unitario_cent) AS total_cent,
               COUNT(DISTINCT i.gasto_id) AS transacciones
          FROM gasto_item AS i JOIN gasto AS g ON g.id = i.gasto_id
         WHERE g.ocurrido_en >= :desde AND g.ocurrido_en < :hasta
         GROUP BY i.nombre""",
}

_SELECT_EXPENSE = (
    "SELECT g.id, l.nombre AS lugar, c.nombre AS categoria, g.total_cent, g.ocurrido_en, "
    "g.nota FROM gasto AS g JOIN lugar AS l ON l.id = g.lugar_id "
    "LEFT JOIN categoria AS c ON c.id = g.categoria_id "
)


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ----------------------------------------------------------------- lectura
    def budget_status(self, mes: str) -> BudgetStatus:
        with self.db.read() as conn:
            gastado = conn.execute(_SPENT_SQL, month_range(mes)).fetchone()[0]
            row = conn.execute(
                "SELECT monto_cent FROM presupuesto WHERE vigente_desde <= ? "
                "ORDER BY vigente_desde DESC LIMIT 1",
                (mes,),
            ).fetchone()
        return BudgetStatus(mes, gastado, row["monto_cent"] if row else None)

    def scatter_points(self, mes: str) -> list[ScatterPoint]:
        with self.db.read() as conn:
            rows = conn.execute(_SCATTER_SQL, month_range(mes)).fetchall()
        return [ScatterPoint(r["dia"], r["monto_cent"], r["transacciones"]) for r in rows]

    # ---------------------------------------------------------------- análisis
    def daily_totals(self, mes: str) -> list[Total]:
        """Un punto por día con gastos: el total consolidado de ese día."""
        with self.db.read() as conn:
            rows = conn.execute(_DAILY_SQL, month_range(mes)).fetchall()
        return [Total(r["clave"], str(r["clave"]), r["total_cent"], r["transacciones"]) for r in rows]

    def monthly_totals(self, year: int | str) -> list[Total]:
        with self.db.read() as conn:
            rows = conn.execute(_MONTHLY_SQL, period_range(f"{int(year):04d}")).fetchall()
        return [Total(r["clave"], str(r["clave"]), r["total_cent"], r["transacciones"]) for r in rows]

    def totals_by(self, dimension: str, period: str, top: int | None = 8) -> list[Total]:
        """Totales por categoría, lugar o producto en un mes ('YYYY-MM') o año ('YYYY').

        Ordenados de mayor a menor. Lo que pase de `top` se suma en "Otros";
        con `top=None` se devuelven todos.
        """
        if dimension not in DIMENSIONS:
            raise ValidationError(f"Dimensión desconocida: {dimension!r}")
        with self.db.read() as conn:
            rows = conn.execute(_BY_SQL[dimension], period_range(period)).fetchall()
        merged: dict[str, list] = {}
        for r in rows:
            key = place_key(r["etiqueta"])
            slot = merged.setdefault(key, [r["etiqueta"], 0, 0])
            slot[1] += r["total_cent"]
            slot[2] += r["transacciones"]
        totals = sorted(
            (Total(k, label, cents, count) for k, (label, cents, count) in merged.items()),
            key=lambda t: (-t.total_cent, t.etiqueta.casefold()),
        )
        if top is not None and len(totals) > top:
            rest = totals[top - 1:]
            totals = totals[: top - 1] + [
                Total(OTROS, f"{OTROS} ({len(rest)})", sum(t.total_cent for t in rest),
                      sum(t.transacciones for t in rest))
            ]
        return totals

    def list_expenses(
        self,
        mes: str,
        *,
        limit: int = 50,
        after: tuple[str, int] | None = None,
    ) -> list[Expense]:
        """Página del historial. `after` es (ocurrido_en, id) del último registro visto."""
        bounds = month_range(mes)
        limit = max(1, min(limit, 500))
        sql = _SELECT_EXPENSE + "WHERE g.ocurrido_en >= ? AND g.ocurrido_en < ? "
        params: list[object] = [bounds["desde"], bounds["hasta"]]
        if after is not None:
            sql += "AND (g.ocurrido_en, g.id) < (?, ?) "
            params.extend(after)
        sql += "ORDER BY g.ocurrido_en DESC, g.id DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            return [Expense(**dict(row)) for row in conn.execute(sql, params)]

    def get_expense(self, gasto_id: int) -> Expense | None:
        with self.db.read() as conn:
            row = conn.execute(_SELECT_EXPENSE + "WHERE g.id = ?", (gasto_id,)).fetchone()
            if row is None:
                return None
            items = tuple(
                Item(r["nombre"], r["cantidad"], r["valor_unitario_cent"])
                for r in conn.execute(
                    "SELECT nombre, cantidad, valor_unitario_cent FROM gasto_item "
                    "WHERE gasto_id = ? ORDER BY id",
                    (gasto_id,),
                )
            )
        return Expense(**dict(row), items=items)

    def suggest_places(self, prefix: str, limit: int = 8) -> list[str]:
        return self._suggest("lugar", "lugar_id", prefix, limit)

    def suggest_categories(self, prefix: str = "", limit: int = 8) -> list[str]:
        """Sin texto devuelve las más usadas, para mostrarlas como atajos."""
        return self._suggest("categoria", "categoria_id", prefix, limit)

    def _suggest(self, table: str, column: str, prefix: str, limit: int) -> list[str]:
        key = place_key(prefix)
        escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # table y column vienen de las dos llamadas de arriba, nunca del usuario.
        sql = (
            f"SELECT t.nombre FROM {table} AS t "
            f"LEFT JOIN gasto AS g ON g.{column} = t.id "
            "WHERE t.clave LIKE ? ESCAPE '\\' "
            "GROUP BY t.id ORDER BY COUNT(g.id) DESC, t.clave LIMIT ?"
        )
        with self.db.read() as conn:
            rows = conn.execute(sql, (f"{escaped}%", limit)).fetchall()
        return [r["nombre"] for r in rows]

    def audit_trail(self, gasto_id: int) -> list[sqlite3.Row]:
        with self.db.read() as conn:
            return conn.execute(
                "SELECT accion, antes, registrado_en FROM auditoria "
                "WHERE gasto_id = ? ORDER BY id DESC",
                (gasto_id,),
            ).fetchall()

    # --------------------------------------------------------------- escritura
    def add_expense(
        self, data: ExpenseInput, session: WriteAuthorization | None = None
    ) -> int:
        clean = self._validate(data)
        now = now_local()
        with self.db.capture(session) as conn:
            cursor = conn.execute(
                "INSERT INTO gasto (lugar_id, categoria_id, total_cent, ocurrido_en, nota, "
                "creado_en, actualizado_en) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._place_id(conn, clean["lugar"]),
                 self._category_id(conn, clean["categoria"]), clean["total_cent"],
                 clean["ocurrido_en"], clean["nota"], now, now),
            )
            gasto_id = int(cursor.lastrowid)
            self._insert_items(conn, gasto_id, clean["items"])
        return gasto_id

    def update_expense(
        self, gasto_id: int, data: ExpenseInput, session: WriteAuthorization
    ) -> None:
        clean = self._validate(data)
        with self.db.write(session) as conn:
            # El gasto va primero: su trigger copia los ítems viejos a la auditoría.
            cursor = conn.execute(
                "UPDATE gasto SET lugar_id = ?, categoria_id = ?, total_cent = ?, "
                "ocurrido_en = ?, nota = ?, actualizado_en = ? WHERE id = ?",
                (self._place_id(conn, clean["lugar"]),
                 self._category_id(conn, clean["categoria"]), clean["total_cent"],
                 clean["ocurrido_en"], clean["nota"], now_local(), gasto_id),
            )
            if cursor.rowcount == 0:
                raise ValidationError(f"No existe el gasto {gasto_id}.")
            conn.execute("DELETE FROM gasto_item WHERE gasto_id = ?", (gasto_id,))
            self._insert_items(conn, gasto_id, clean["items"])

    def delete_expense(self, gasto_id: int, session: WriteAuthorization) -> None:
        with self.db.write(session) as conn:
            cursor = conn.execute("DELETE FROM gasto WHERE id = ?", (gasto_id,))
            if cursor.rowcount == 0:
                raise ValidationError(f"No existe el gasto {gasto_id}.")

    def set_budget(
        self, vigente_desde: str, monto: object, session: WriteAuthorization
    ) -> None:
        _check_month(vigente_desde)
        cents = to_cents(monto)  # type: ignore[arg-type]
        if cents <= 0:
            raise ValidationError("El presupuesto debe ser mayor que cero.")
        with self.db.write(session) as conn:
            conn.execute(
                "INSERT INTO presupuesto (vigente_desde, monto_cent) VALUES (?, ?) "
                "ON CONFLICT (vigente_desde) DO UPDATE SET monto_cent = excluded.monto_cent",
                (vigente_desde, cents),
            )

    # ----------------------------------------------------------------- apoyo
    @staticmethod
    def _validate(data: ExpenseInput) -> dict:
        lugar = " ".join((data.lugar or "").split())
        if not lugar or not place_key(lugar):
            raise ValidationError("El lugar de compra es obligatorio.")
        total_cent = to_cents(data.total)
        if total_cent <= 0:
            raise ValidationError("El valor total debe ser mayor que cero.")
        items: list[tuple[str, int, int]] = []
        for item in data.items:
            nombre = " ".join((item.nombre or "").split())
            if not nombre:
                raise ValidationError("Cada ítem necesita un nombre.")
            if item.cantidad < 1:
                raise ValidationError(f"Cantidad inválida para {nombre!r}.")
            unit_cent = to_cents(item.valor_unitario)
            if unit_cent < 0:
                raise ValidationError(f"Valor unitario inválido para {nombre!r}.")
            items.append((nombre, item.cantidad, unit_cent))
        return {
            "lugar": lugar,
            "total_cent": total_cent,
            "ocurrido_en": normalize_timestamp(data.ocurrido_en),
            "nota": (data.nota or "").strip() or None,
            "items": items,
            "categoria": " ".join((data.categoria or "").split()) or None,
        }

    @staticmethod
    def _place_id(conn: sqlite3.Connection, nombre: str) -> int:
        key = place_key(nombre)
        conn.execute(
            "INSERT INTO lugar (nombre, clave) VALUES (?, ?) ON CONFLICT (clave) DO NOTHING",
            (nombre, key),
        )
        return conn.execute("SELECT id FROM lugar WHERE clave = ?", (key,)).fetchone()[0]

    @staticmethod
    def _category_id(conn: sqlite3.Connection, nombre: str | None) -> int | None:
        if not nombre or not place_key(nombre):
            return None
        key = place_key(nombre)
        conn.execute(
            "INSERT INTO categoria (nombre, clave) VALUES (?, ?) ON CONFLICT (clave) DO NOTHING",
            (nombre, key),
        )
        return conn.execute("SELECT id FROM categoria WHERE clave = ?", (key,)).fetchone()[0]

    @staticmethod
    def _insert_items(
        conn: sqlite3.Connection, gasto_id: int, items: Sequence[tuple[str, int, int]]
    ) -> None:
        conn.executemany(
            "INSERT INTO gasto_item (gasto_id, nombre, cantidad, valor_unitario_cent) "
            "VALUES (?, ?, ?, ?)",
            [(gasto_id, *item) for item in items],
        )
