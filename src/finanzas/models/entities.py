"""Objetos de dominio. No conocen SQLite ni la interfaz."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTS = Decimal(100)


class ValidationError(ValueError):
    """Dato inválido en un formulario."""


def to_cents(value: Decimal | int | str) -> int:
    """Convierte un valor monetario a centavos sin pasar por float."""
    if isinstance(value, float):
        raise ValidationError("Usa Decimal, int o str para valores monetarios.")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValidationError(f"Valor monetario inválido: {value!r}") from exc
    if not amount.is_finite():
        raise ValidationError(f"Valor monetario inválido: {value!r}")
    return int((amount * CENTS).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    return Decimal(cents) / CENTS


def normalize_timestamp(value: date | datetime | str | None) -> str:
    """Devuelve 'YYYY-MM-DDTHH:MM:SS'. Un valor vacío toma la hora actual.

    Una fecha sin hora (la interfaz solo pide el día) se guarda a las 00:00:00.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        moment = datetime.now()
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    else:
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError(f"Fecha inválida: {value!r}") from exc
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)  # se conserva la hora de pared de la compra
    return moment.replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ItemInput:
    nombre: str
    valor_unitario: Decimal | int | str
    cantidad: int = 1


@dataclass(frozen=True)
class ExpenseInput:
    """Contenido del formulario. Solo `total` y `lugar` son obligatorios."""

    total: Decimal | int | str
    lugar: str
    ocurrido_en: date | datetime | str | None = None
    nota: str | None = None
    items: tuple[ItemInput, ...] = ()
    categoria: str | None = None


@dataclass(frozen=True)
class Item:
    nombre: str
    cantidad: int
    valor_unitario_cent: int


@dataclass(frozen=True)
class Expense:
    id: int
    lugar: str
    total_cent: int
    ocurrido_en: str
    nota: str | None
    items: tuple[Item, ...] = field(default_factory=tuple)
    categoria: str | None = None

    @property
    def items_cent(self) -> int:
        return sum(i.cantidad * i.valor_unitario_cent for i in self.items)

    @property
    def sin_detallar_cent(self) -> int:
        """Parte del total que no aparece en los ítems (propina, impuestos...)."""
        return self.total_cent - self.items_cent if self.items else 0


@dataclass(frozen=True)
class BudgetStatus:
    mes: str
    gastado_cent: int
    presupuesto_cent: int | None

    @property
    def porcentaje(self) -> float | None:
        """Sin tope: puede superar 100. `None` si no hay presupuesto."""
        if not self.presupuesto_cent:
            return None
        return 100 * self.gastado_cent / self.presupuesto_cent


@dataclass(frozen=True)
class ScatterPoint:
    """Una o varias transacciones del mismo día con monto casi igual."""

    dia: int
    monto_cent: int
    transacciones: int


@dataclass(frozen=True)
class Total:
    """Un punto o una barra de los análisis.

    `clave` es el día (1-31), el mes (1-12) o el nombre agrupado.
    """

    clave: int | str
    etiqueta: str
    total_cent: int
    transacciones: int
