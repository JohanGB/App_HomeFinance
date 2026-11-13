"""Formato colombiano de dinero y fechas, y color de la barra de presupuesto.

Sin dependencias de Flet: se prueba como lógica pura.
"""

from __future__ import annotations

import calendar
import colorsys
import re
from datetime import date, datetime
from decimal import Decimal

from ..models.entities import ValidationError

MONTHS = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]
_SHORT = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

_THOUSANDS_DOT = re.compile(r"\d{1,3}(\.\d{3})+")
_THOUSANDS_COMMA = re.compile(r"\d{1,3}(,\d{3})+")
_PLAIN = re.compile(r"\d+(\.\d+)?")


def parse_money(text: str) -> Decimal:
    """Lee montos como se escriben en Colombia.

    "25.000" son veinticinco mil, no veinticinco: un punto seguido de tres dígitos
    se toma como separador de miles. La coma decimal también se acepta.
    """
    raw = (text or "").replace("$", "").replace(" ", "").replace(" ", "").strip()
    if not raw:
        raise ValidationError("Escribe un valor.")
    if "." in raw and "," in raw:
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        raw = raw.replace(thousands, "").replace(decimal_sep, ".")
    elif "," in raw:
        raw = raw.replace(",", "") if _THOUSANDS_COMMA.fullmatch(raw) else raw.replace(",", ".")
    elif "." in raw and _THOUSANDS_DOT.fullmatch(raw):
        raw = raw.replace(".", "")
    if not _PLAIN.fullmatch(raw):
        raise ValidationError(f"Valor no válido: {text!r}")
    return Decimal(raw)


def _group(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    pesos, rest = divmod(abs(cents), 100)
    text = f"{sign}$ {_group(pesos)}"
    return f"{text},{rest:02d}" if rest else text


def format_compact(pesos: float) -> str:
    """Etiquetas cortas para ejes: 1,2 M · 45 mil · 900."""
    value = abs(pesos)
    if value >= 1_000_000:
        number, suffix = value / 1_000_000, " M"
    elif value >= 1_000:
        number, suffix = value / 1_000, " mil"
    else:
        return f"{value:.0f}"
    text = f"{number:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return text + suffix


def money_input(cents: int) -> str:
    """Valor para precargar un campo de texto al editar."""
    pesos, rest = divmod(cents, 100)
    return f"{_group(pesos)},{rest:02d}" if rest else _group(pesos)


# ------------------------------------------------------------------------ fechas
def current_month() -> str:
    return date.today().strftime("%Y-%m")


def shift_month(mes: str, delta: int) -> str:
    year, month = map(int, mes.split("-"))
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_label(mes: str) -> str:
    year, month = map(int, mes.split("-"))
    return f"{MONTHS[month - 1]} {year}"


def days_in_month(mes: str) -> int:
    year, month = map(int, mes.split("-"))
    return calendar.monthrange(year, month)[1]


def format_when(iso: str) -> str:
    """Solo el día: la app no pide la hora."""
    moment = datetime.fromisoformat(iso)
    return f"{moment.day} {_SHORT[moment.month - 1]}"


def format_date_long(iso: str) -> str:
    moment = datetime.fromisoformat(iso)
    return f"{moment.day} de {MONTHS[moment.month - 1].lower()} de {moment.year}"


def date_input(value: date | str) -> str:
    """Fecha para un campo de texto: DD/MM/AAAA."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value).date()
    return f"{value:%d/%m/%Y}"


_DATE_DMY = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_DATE_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def parse_date(text: str) -> date | None:
    """DD/MM/AAAA (también con - o .) o AAAA-MM-DD. Vacío devuelve None."""
    raw = (text or "").strip()
    if not raw:
        return None
    if match := _DATE_DMY.fullmatch(raw):
        day, month, year = map(int, match.groups())
    elif match := _DATE_ISO.fullmatch(raw):
        year, month, day = map(int, match.groups())
    else:
        raise ValidationError("Usa el formato DD/MM/AAAA.")
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValidationError(f"Esa fecha no existe: {raw}") from exc


def picked_date(value: date | datetime) -> date:
    """Fecha elegida en el calendario.

    Flet entrega la medianoche local convertida a UTC: en Bogotá, el 2 de noviembre
    llega como 2026-11-02T05:00Z. Pasarla a la zona local evita guardar el día anterior.
    """
    if isinstance(value, datetime):
        return (value.astimezone() if value.tzinfo else value).date()
    return value


MONTH_INITIALS = ["E", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]


def short_month(month: int) -> str:
    return _SHORT[month - 1]


# ------------------------------------------------------------------ presupuesto
GREEN_HUES = (140.0, 105.0)    # 0 % → 49,9 %
YELLOW_HUES = (55.0, 38.0)     # 50 % → 84,9 %
RED_HUES = (15.0, 0.0)         # 85 % → 100 % (y más)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def budget_hue(pct: float) -> float:
    """Tono HSL según el porcentaje gastado.

    Cada tramo conserva su color (verde, amarillo, rojo) y dentro de él el tono se
    desplaza linealmente hacia el siguiente, así la barra avisa antes del salto.
    """
    if pct < 50:
        return _lerp(*GREEN_HUES, pct / 50)
    if pct < 85:
        return _lerp(*YELLOW_HUES, (pct - 50) / 35)
    return _lerp(*RED_HUES, (pct - 85) / 15)


def budget_color(pct: float | None) -> str:
    if pct is None:
        return "#8A8F98"
    red, green, blue = colorsys.hls_to_rgb(budget_hue(pct) / 360, 0.45, 0.72)
    return "#{:02X}{:02X}{:02X}".format(*(round(c * 255) for c in (red, green, blue)))
