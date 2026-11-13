"""Interfaz Flet: un solo código para Windows y Android.

La vista nunca hace trabajo bloqueante en el hilo de eventos: todo lo que toca disco,
red o bcrypt pasa por `asyncio.to_thread` hacia `AppController`.
"""

from __future__ import annotations

import asyncio
import math
import threading
from datetime import date, datetime
from typing import Any, Callable

import flet as ft
import flet_charts as fch

from ..models.database import DatabaseError
from ..models.entities import Expense, ExpenseInput, ItemInput, ValidationError
from ..models.repository import OTROS
from ..services.drive_auth import AuthError, AuthOfflineError, MissingClientSecret
from ..services.drive_gateway import DriveError, OfflineError, RemoteFile
from ..services.sync_engine import Resolution, SyncResult, SyncStatus
from . import formatting as fmt
from .controller import ANALYSIS_VIEWS, PAGE_SIZE, Analysis, AppController, describe_sync

EXPECTED_ERRORS = (ValidationError, DatabaseError, DriveError, AuthError, ValueError)
SEED = ft.Colors.TEAL


class FinanzasApp:
    def __init__(self, page: ft.Page, controller: AppController | None = None) -> None:
        self.page = page
        self.ctrl = controller or AppController()
        self.mes = fmt.current_month()
        self.limit = PAGE_SIZE
        self.catalog_items: list[RemoteFile] = []
        self.online = False
        self.last_sync: SyncResult | None = None

        self.busy = ft.ProgressBar(visible=False, bar_height=3)
        self.sync_button = ft.IconButton(ft.Icons.CLOUD_DONE, on_click=self.on_sync_click)
        self.lock_button = ft.IconButton(ft.Icons.LOCK, on_click=self.on_lock_click)
        self.env_dropdown = ft.Dropdown(
            label="Entorno", expand=True, dense=True, on_select=self.on_env_select
        )
        self.month_text = ft.Text(weight=ft.FontWeight.W_600, size=16)
        self.budget_box = ft.Column(spacing=8)
        self.view, self.scope = "dia", "mes"
        self.chart_box = ft.Container(height=230)
        self.analysis_title = ft.Text(size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.view_dropdown = ft.Dropdown(
            value=self.view, dense=True, expand=True,
            options=[ft.DropdownOption(key=k, text=t) for k, t in ANALYSIS_VIEWS],
            on_select=self.on_view_select,
        )
        self.scope_button = ft.SegmentedButton(
            segments=[ft.Segment(value="mes", label=ft.Text("Mes")),
                      ft.Segment(value="anio", label=ft.Text("Año"))],
            selected=[self.scope], show_selected_icon=False, visible=False,
            on_change=self.on_scope_change,
        )
        self.list_box = ft.Column(spacing=0)
        self.more_button = ft.TextButton("Ver más", on_click=self.on_more, visible=False)

    # ================================================================ arranque
    async def start(self) -> None:
        page = self.page
        page.title = "MyCents"
        page.theme = ft.Theme(color_scheme_seed=SEED)
        page.dark_theme = ft.Theme(color_scheme_seed=SEED)
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.padding = 0
        page.locale_configuration = ft.LocaleConfiguration(
            supported_locales=[ft.Locale("es", "CO"), ft.Locale("es")],
            current_locale=ft.Locale("es", "CO"),
        )
        if page.platform in (ft.PagePlatform.WINDOWS, ft.PagePlatform.MACOS, ft.PagePlatform.LINUX):
            page.window.width, page.window.height = 440, 860
            page.window.min_width, page.window.min_height = 360, 560
            page.window.prevent_close = True
            page.window.on_event = self.on_window_event
        page.on_app_lifecycle_state_change = self.on_lifecycle
        self.show_center(ft.ProgressRing(), "Abriendo…")

        try:
            logged = await asyncio.to_thread(self.ctrl.try_silent_login)
        except MissingClientSecret as exc:
            self.show_center(
                ft.Icon(ft.Icons.KEY_OFF, size=48), "Falta configurar Google Drive", str(exc)
            )
            return
        except AuthOfflineError:
            logged = False
        except AuthError as exc:
            self.toast(str(exc))
            logged = False

        await self.load_catalog()
        if not logged and not self.catalog_items:
            self.show_login()
            return
        await self.open_initial()

    async def load_catalog(self) -> None:
        catalog = await asyncio.to_thread(self.ctrl.catalog)
        self.catalog_items, self.online = catalog.environments, catalog.online
        if catalog.error:
            self.toast(catalog.error)

    async def open_initial(self) -> None:
        if not self.catalog_items:
            self.show_first_environment()
            return
        last = self.ctrl.engine.state.last_environment
        remote = next((r for r in self.catalog_items if r.id == last), self.catalog_items[0])
        await self.open_environment(remote)

    # ========================================================= inicio de sesión
    def show_login(self, message: str = "") -> None:
        button = ft.FilledButton(
            "Conectar con Google Drive", icon=ft.Icons.LOGIN, on_click=self.on_login
        )
        actions: list[ft.Control] = [button]
        if self.catalog_items:
            actions.append(ft.TextButton("Seguir sin conexión", on_click=self.on_offline))
        self.show_center(
            ft.Icon(ft.Icons.SAVINGS_OUTLINED, size=56, color=SEED),
            "MyCents",
            message or "Tus datos viven en este equipo. Drive guarda una copia de cada entorno.",
            actions,
        )

    async def on_offline(self, e) -> None:
        await self.open_initial()

    async def on_login(self, e) -> None:
        if self.ctrl.mode == "device":
            await self.device_login()
            return
        self.show_center(ft.ProgressRing(), "Completa el inicio de sesión en el navegador")
        ok, _ = await self.call(self.ctrl.desktop_login, quiet=True)
        if not ok:
            self.show_login("No se completó el inicio de sesión. Intenta de nuevo.")
            return
        await self.after_login()

    async def device_login(self) -> None:
        ok, started = await self.call(self.ctrl.start_device_login)
        if not ok:
            self.show_login()
            return
        flow, code = started
        cancel = threading.Event()

        async def open_google(e):
            await ft.UrlLauncher().launch_url(code.verification_url)

        async def copy_code(e):
            await ft.Clipboard().set(code.user_code)
            self.toast("Código copiado")

        def stop(e):
            cancel.set()

        self.show_center(
            ft.Text(code.user_code, size=34, weight=ft.FontWeight.BOLD, selectable=True),
            "Escribe este código en Google",
            f"Abre {code.verification_url}, inicia sesión y aprueba el acceso. "
            "Esta pantalla avanza sola.",
            [
                ft.FilledButton("Abrir Google", icon=ft.Icons.OPEN_IN_NEW, on_click=open_google),
                ft.OutlinedButton("Copiar código", icon=ft.Icons.CONTENT_COPY, on_click=copy_code),
                ft.ProgressRing(width=22, height=22, stroke_width=2),
                ft.TextButton("Cancelar", on_click=stop),
            ],
        )
        ok, _ = await self.call(self.ctrl.finish_device_login, flow, code, cancel)
        if not ok:
            self.show_login()
            return
        await self.after_login()

    async def after_login(self) -> None:
        self.show_center(ft.ProgressRing(), "Buscando tus entornos en Drive…")
        await self.load_catalog()
        await self.open_initial()

    def show_first_environment(self) -> None:
        name = ft.TextField(label="Nombre", value="gastos_casa", autofocus=True)

        async def create(e):
            name.error = None
            self.show_busy(True)
            ok, remote = await self.call(self.ctrl.create_environment, name.value or "")
            self.show_busy(False)
            if ok:
                self.catalog_items = [remote]
                await self.open_environment(remote)

        self.show_center(
            ft.Icon(ft.Icons.FOLDER_OPEN, size=48, color=SEED),
            "Crea tu primer entorno",
            "Un entorno es un archivo de gastos aparte, por ejemplo la casa o lo personal.",
            [name, ft.FilledButton("Crear", on_click=create), self.busy],
        )

    # ================================================================ entorno
    async def open_environment(self, remote: RemoteFile) -> None:
        self.show_center(ft.ProgressRing(), f"Abriendo {remote.name}…")
        ok, env = await self.call(self.ctrl.open_environment, remote)
        if not ok:
            self.show_login("No se pudo abrir el entorno.") if not self.catalog_items else (
                self.show_main()
            )
            return
        self.ctrl.engine.state.last_environment = env.file_id
        self.last_sync = env.sync
        self.limit = PAGE_SIZE
        self.show_main()
        if env.sync.status is SyncStatus.CONFLICT:
            await self.ask_conflict(env.sync.detail)
        if self.ctrl.needs_pin_setup():
            await self.pin_setup_dialog()
        await self.refresh()

    async def on_env_select(self, e) -> None:
        remote = next((r for r in self.catalog_items if r.id == e.control.value), None)
        env = self.ctrl.env
        if remote and (env is None or remote.id != env.file_id):
            await self.open_environment(remote)

    # ============================================================ vista principal
    def show_main(self) -> None:
        env = self.ctrl.env
        self.env_dropdown.options = [ft.DropdownOption(key=r.id, text=r.name) for r in self.catalog_items]
        self.env_dropdown.value = env.file_id if env else None
        self.update_status_icons()

        self.page.appbar = ft.AppBar(
            title=ft.Text("MyCents", weight=ft.FontWeight.W_600),
            actions=[
                self.sync_button,
                self.lock_button,
                ft.PopupMenuButton(
                    items=[
                        ft.PopupMenuItem(content="Nuevo entorno", icon=ft.Icons.CREATE_NEW_FOLDER,
                                         on_click=self.on_new_env),
                        ft.PopupMenuItem(content="Cambiar PIN", icon=ft.Icons.PASSWORD,
                                         on_click=self.on_change_pin),
                        ft.PopupMenuItem(
                            content="Cerrar sesión de Google" if self.ctrl.online else "Conectar Google Drive",
                            icon=ft.Icons.LOGOUT if self.ctrl.online else ft.Icons.LOGIN,
                            on_click=self.on_account,
                        ),
                    ]
                ),
            ],
        )
        self.page.floating_action_button = ft.FloatingActionButton(
            icon=ft.Icons.ADD, on_click=self.on_add, tooltip="Registrar gasto"
        )
        month_row = ft.Row(
            [
                ft.IconButton(ft.Icons.CHEVRON_LEFT, on_click=self.on_prev_month),
                self.month_text,
                ft.IconButton(ft.Icons.CHEVRON_RIGHT, on_click=self.on_next_month),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
        )
        body = ft.ListView(
            [
                self.busy,
                ft.Container(self.env_dropdown, padding=ft.Padding.only(left=16, right=16, top=12)),
                month_row,
                self.card(self.budget_box),
                self.card(ft.Column(
                    [
                        ft.Text("Análisis", weight=ft.FontWeight.W_600),
                        ft.Row([self.view_dropdown]),
                        ft.Row([self.analysis_title, self.scope_button],
                               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        self.chart_box,
                    ],
                    spacing=8,
                )),
                ft.Container(ft.Text("Movimientos", weight=ft.FontWeight.W_600),
                             padding=ft.Padding.only(left=20, top=8)),
                self.list_box,
                ft.Container(self.more_button, align=ft.Alignment.CENTER),
                ft.Container(height=88),
            ],
            expand=True,
        )
        self.page.controls.clear()
        self.page.add(ft.SafeArea(body, expand=True))
        self.page.update()

    @staticmethod
    def card(content: ft.Control) -> ft.Control:
        return ft.Container(
            ft.Card(ft.Container(content, padding=16)),
            padding=ft.Padding.symmetric(horizontal=12, vertical=4),
        )

    async def refresh(self) -> None:
        env = self.ctrl.env
        if env is None:
            return
        generation = env.generation
        ok, board = await self.call(self.ctrl.dashboard, self.mes, self.limit)
        if not ok or self.ctrl.env is None or board.generation != generation or board.mes != self.mes:
            return  # llegó tarde: el usuario ya cambió de entorno o de mes
        self.month_text.value = fmt.month_label(self.mes)
        self.render_budget(board.budget)
        self.render_list(board.expenses)
        self.more_button.visible = board.has_more
        self.update_status_icons()
        self.page.update()
        await self.refresh_analysis()

    async def refresh_analysis(self) -> None:
        env = self.ctrl.env
        if env is None:
            return
        request = (env.generation, self.view, self.mes, self.scope)
        ok, result = await self.call(self.ctrl.analysis, self.view, self.mes, self.scope)
        current = self.ctrl.env
        if not ok or current is None or request != (current.generation, self.view, self.mes, self.scope):
            return  # el usuario cambió algo mientras se calculaba
        self.render_analysis(result)
        self.page.update()

    async def on_view_select(self, e) -> None:
        self.view = e.control.value
        await self.refresh_analysis()

    async def on_scope_change(self, e) -> None:
        self.scope = (e.control.selected or ["mes"])[0]
        await self.refresh_analysis()

    # ------------------------------------------------------------- presupuesto
    def render_budget(self, status) -> None:
        pct = status.porcentaje
        color = fmt.budget_color(pct)
        header = ft.Row(
            [
                ft.Text("Presupuesto", weight=ft.FontWeight.W_600, expand=True),
                ft.TextButton("Definir" if pct is None else "Editar", on_click=self.on_budget),
            ]
        )
        if pct is None:
            self.budget_box.controls = [
                header,
                ft.Text(f"Gastado: {fmt.format_money(status.gastado_cent)}", size=20),
                ft.Text("Sin presupuesto para este mes.", color=ft.Colors.ON_SURFACE_VARIANT),
            ]
            return
        self.budget_box.controls = [
            header,
            ft.ProgressBar(
                value=min(pct / 100, 1.0), color=color,
                bgcolor=ft.Colors.with_opacity(0.15, color), bar_height=14, border_radius=7,
            ),
            ft.Row(
                [
                    ft.Text(
                        f"{fmt.format_money(status.gastado_cent)} de "
                        f"{fmt.format_money(status.presupuesto_cent)}",
                        expand=True,
                    ),
                    ft.Text(f"{pct:.0f} %", weight=ft.FontWeight.BOLD, color=color),
                ]
            ),
        ]

    # ---------------------------------------------------------------- análisis
    def render_analysis(self, result: Analysis) -> None:
        year = int(result.period[:4])
        self.scope_button.visible = result.kind == "bars"
        self.scope_button.selected = [self.scope]
        self.analysis_title.value = (
            f"Año {year}" if len(result.period) == 4 else fmt.month_label(result.period)
        )
        if not result.totals:
            self.chart_box.height = 120
            self.chart_box.content = ft.Container(
                ft.Text("Sin gastos en este periodo.", color=ft.Colors.ON_SURFACE_VARIANT),
                align=ft.Alignment.CENTER,
            )
            return
        if result.kind == "bars":
            self.chart_box.height = None
            self.chart_box.content = self.bars(result)
            return
        self.chart_box.height = 230
        self.chart_box.content = self.scatter(result, year)

    def scatter(self, result: Analysis, year: int) -> ft.Control:
        """Puntos con el total de cada día (o mes) unidos por una línea, en orden."""
        daily = result.view == "dia"
        top = max(t.total_cent for t in result.totals) / 100
        step = _nice_step(top / 4)
        max_y = step * math.ceil(top / step * 1.05)

        def tooltip(t) -> str:
            if daily:
                when = f"{t.clave} {fmt.short_month(int(result.period[5:7]))}"
            else:
                when = f"{fmt.MONTHS[t.clave - 1]} {year}"
            count = f"\n{t.transacciones} gasto{'s' if t.transacciones != 1 else ''}"
            return f"{when}\n{fmt.format_money(t.total_cent)}{count}"

        series = fch.LineChartData(
            points=[
                fch.LineChartDataPoint(
                    x=t.clave, y=t.total_cent / 100,
                    tooltip=fch.LineChartDataPointTooltip(
                        text=tooltip(t),
                        text_style=ft.TextStyle(color=ft.Colors.ON_INVERSE_SURFACE, size=12),
                    ),
                )
                for t in result.totals
            ],
            color=ft.Colors.with_opacity(0.55, SEED),
            stroke_width=2,
            curved=False,
            point=fch.ChartCirclePoint(radius=3.5, color=SEED, stroke_width=0),
        )
        if daily:
            days = fmt.days_in_month(result.period)
            min_x, max_x = 0, days + 1
            bottom = [(d, str(d)) for d in range(5, days + 1, 5)]
        else:
            min_x, max_x = 0.5, 12.5
            bottom = [(m, fmt.MONTH_INITIALS[m - 1]) for m in range(1, 13)]
        return fch.LineChart(
            data_series=[series],
            # Colores del tema invertido: claro sobre oscuro en modo claro y al revés.
            tooltip=fch.LineChartTooltip(bgcolor=ft.Colors.INVERSE_SURFACE, border_radius=8),
            min_x=min_x, max_x=max_x, min_y=0, max_y=max_y,
            horizontal_grid_lines=fch.ChartGridLines(
                interval=step, color=ft.Colors.with_opacity(0.12, ft.Colors.ON_SURFACE), width=1
            ),
            left_axis=fch.ChartAxis(
                label_size=52,
                labels=[
                    fch.ChartAxisLabel(value=v, label=ft.Text(fmt.format_compact(v), size=11))
                    for v in _frange(step, max_y - step / 2, step)
                ],
            ),
            bottom_axis=fch.ChartAxis(
                label_size=24,
                label_spacing=5 if daily else 1,
                labels=[fch.ChartAxisLabel(value=x, label=ft.Text(text, size=11))
                        for x, text in bottom],
            ),
            expand=True,
        )

    @staticmethod
    def bar_row(t, top: int, grand: int, trailing: ft.Control | None = None) -> ft.Control:
        share = 100 * t.total_cent / grand if grand else 0
        count = f"{share:.0f} % · {t.transacciones} gasto{'s' if t.transacciones != 1 else ''}"
        return ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(t.etiqueta, expand=True, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(fmt.format_money(t.total_cent), weight=ft.FontWeight.W_600),
                    ]
                ),
                ft.ProgressBar(value=t.total_cent / top if top else 0, color=SEED,
                               bgcolor=ft.Colors.with_opacity(0.08, SEED),
                               bar_height=10, border_radius=5),
                ft.Row(
                    [ft.Text(count, size=11, color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                     *([trailing] if trailing else [])],
                ),
            ],
            spacing=3,
        )

    def bars(self, result: Analysis) -> ft.Control:
        """Barras horizontales: los nombres largos se leen completos en un teléfono."""
        grand = sum(t.total_cent for t in result.totals)
        top = max(t.total_cent for t in result.totals)
        rows: list[ft.Control] = []
        for t in result.totals:
            if t.clave == OTROS and result.others:
                rows.append(
                    ft.Container(
                        self.bar_row(t, top, grand, ft.Text("Ver detalle ›", size=12, color=SEED,
                                                            weight=ft.FontWeight.W_600)),
                        on_click=lambda e, r=result, g=grand: self.show_others(r, g),
                        ink=True, border_radius=8,
                        tooltip="Ver qué incluye",
                    )
                )
            else:
                rows.append(self.bar_row(t, top, grand))
        if result.view == "producto":
            rows.append(ft.Text("Solo suma lo registrado como ítems.", size=11, italic=True,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        return ft.Column(rows, spacing=12)

    def show_others(self, result: Analysis, grand: int) -> None:
        """Detalle de la barra "Otros". El porcentaje sigue siendo sobre el total del periodo."""
        top = max(t.total_cent for t in result.others)
        period = f"año {result.period}" if len(result.period) == 4 else fmt.month_label(result.period)
        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text(f"Otros · {period}"),
                content=ft.Container(
                    ft.Column([self.bar_row(t, top, grand) for t in result.others],
                              spacing=12, tight=True),
                    width=420,
                ),
                scrollable=True,
                actions=[ft.TextButton("Cerrar", on_click=lambda e: self.page.pop_dialog())],
            )
        )

    # --------------------------------------------------------------- historial
    def render_list(self, expenses: list[Expense]) -> None:
        if not expenses:
            self.list_box.controls = [
                ft.Container(
                    ft.Text("Todavía no hay gastos este mes. Usa el botón +.",
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    padding=20,
                )
            ]
            return
        self.list_box.controls = [
            ft.ListTile(
                title=ft.Text(x.lugar, max_lines=1),
                subtitle=ft.Text(
                    " · ".join(filter(None, [fmt.format_when(x.ocurrido_en), x.categoria, x.nota])),
                    max_lines=1,
                ),
                trailing=ft.Text(fmt.format_money(x.total_cent), size=15, weight=ft.FontWeight.W_600),
                data=x.id,
                on_click=self.on_expense_click,
            )
            for x in expenses
        ]

    async def on_more(self, e) -> None:
        self.limit += PAGE_SIZE
        await self.refresh()

    async def on_prev_month(self, e) -> None:
        self.mes, self.limit = fmt.shift_month(self.mes, -1), PAGE_SIZE
        await self.refresh()

    async def on_next_month(self, e) -> None:
        self.mes, self.limit = fmt.shift_month(self.mes, 1), PAGE_SIZE
        await self.refresh()

    # ============================================================ formularios
    async def on_add(self, e) -> None:
        await self.expense_form()

    async def on_expense_click(self, e) -> None:
        ok, expense = await self.call(self.ctrl.get_expense, e.control.data)
        if not ok or expense is None:
            return
        items = [
            ft.Row(
                [
                    ft.Text(f"{i.cantidad} × {i.nombre}", expand=True),
                    ft.Text(fmt.format_money(i.cantidad * i.valor_unitario_cent)),
                ]
            )
            for i in expense.items
        ]
        if expense.items and expense.sin_detallar_cent:
            items.append(
                ft.Row([ft.Text("Sin detallar", expand=True, italic=True),
                        ft.Text(fmt.format_money(expense.sin_detallar_cent), italic=True)])
            )

        async def edit(e):
            self.page.pop_dialog()
            if await self.ensure_unlocked():
                await self.expense_form(expense)

        async def delete(e):
            self.page.pop_dialog()
            if await self.ensure_unlocked() and await self.confirm(
                "¿Eliminar este gasto?", f"{expense.lugar} · {fmt.format_money(expense.total_cent)}"
            ):
                if (await self.call(self.ctrl.delete_expense, expense.id))[0]:
                    self.toast("Gasto eliminado")
                    await self.refresh()

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text(expense.lugar),
                content=ft.Column(
                    [
                        ft.Text(fmt.format_money(expense.total_cent), size=24, weight=ft.FontWeight.BOLD),
                        ft.Text(fmt.format_date_long(expense.ocurrido_en)
                                + (f" · {expense.categoria}" if expense.categoria else "")),
                        *([ft.Text(expense.nota, italic=True)] if expense.nota else []),
                        *([ft.Divider()] + items if items else []),
                    ],
                    tight=True,
                ),
                scrollable=True,
                actions=[
                    ft.TextButton("Eliminar", icon=ft.Icons.DELETE_OUTLINE, on_click=delete),
                    ft.TextButton("Editar", icon=ft.Icons.EDIT, on_click=edit),
                    ft.TextButton("Cerrar", on_click=lambda e: self.page.pop_dialog()),
                ],
            )
        )

    async def expense_form(self, expense: Expense | None = None) -> None:
        total = ft.TextField(
            label="Valor total *", prefix="$ ", keyboard_type=ft.KeyboardType.NUMBER, autofocus=True,
            value=fmt.money_input(expense.total_cent) if expense else "",
        )
        place = ft.TextField(
            label="Lugar de compra *", capitalization=ft.TextCapitalization.SENTENCES,
            value=expense.lugar if expense else "",
        )
        suggestions = ft.Row(wrap=True, spacing=6)
        category = ft.TextField(
            label="Categoría (opcional)", capitalization=ft.TextCapitalization.SENTENCES,
            value=(expense.categoria or "") if expense else "",
        )
        category_chips = ft.Row(wrap=True, spacing=6)
        original_date = fmt.date_input(expense.ocurrido_en) if expense else None
        when = ft.TextField(
            label="Fecha", hint_text="DD/MM/AAAA", keyboard_type=ft.KeyboardType.DATETIME,
            value=original_date or fmt.date_input(date.today()), expand=True,
        )
        note = ft.TextField(label="Nota", multiline=True, max_lines=3,
                            value=(expense.nota or "") if expense else "")
        items_col = ft.Column(spacing=6)
        summary = ft.Text(size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        def update_summary(e=None):
            try:
                total_cents = int(fmt.parse_money(total.value) * 100) if total.value.strip() else 0
                item_cents = 0
                for row in items_col.controls:
                    name_f, qty_f, val_f = row.data
                    if val_f.value.strip():
                        item_cents += int(fmt.parse_money(val_f.value) * 100) * int(qty_f.value or 1)
            except (ValidationError, ValueError):
                summary.value = ""
            else:
                if not items_col.controls:
                    summary.value = ""
                elif total_cents and item_cents > total_cents:
                    summary.value = f"Ítems: {fmt.format_money(item_cents)} · supera el total"
                    summary.color = ft.Colors.ERROR
                else:
                    summary.value = (
                        f"Ítems: {fmt.format_money(item_cents)} · "
                        f"sin detallar: {fmt.format_money(max(total_cents - item_cents, 0))}"
                    )
                    summary.color = ft.Colors.ON_SURFACE_VARIANT
            _refresh(summary)

        def add_item(e=None, nombre="", cantidad=1, valor=""):
            # Dos líneas por ítem: en un teléfono no caben cuatro campos en una fila.
            name_f = ft.TextField(label="Producto", value=nombre, expand=True, dense=True)
            qty_f = ft.TextField(label="Cant.", value=str(cantidad), width=72, dense=True,
                                 keyboard_type=ft.KeyboardType.NUMBER,
                                 input_filter=ft.NumbersOnlyInputFilter(), on_change=update_summary)
            val_f = ft.TextField(label="Valor unitario", prefix="$ ", value=valor, expand=True,
                                 dense=True, keyboard_type=ft.KeyboardType.NUMBER,
                                 on_change=update_summary)
            row = ft.Container(padding=ft.Padding.only(bottom=6))

            def remove(ev):
                items_col.controls.remove(row)
                update_summary()
                items_col.update()

            row.data = (name_f, qty_f, val_f)
            row.content = ft.Column(
                [
                    ft.Row([name_f, ft.IconButton(ft.Icons.REMOVE_CIRCLE_OUTLINE, tooltip="Quitar",
                                                  on_click=remove)], spacing=4),
                    ft.Row([qty_f, val_f], spacing=8),
                ],
                spacing=6,
                tight=True,
            )
            items_col.controls.append(row)
            _refresh(items_col)

        if expense:
            for i in expense.items:
                add_item(nombre=i.nombre, cantidad=i.cantidad, valor=fmt.money_input(i.valor_unitario_cent))
        total.on_change = update_summary

        async def suggest(e):
            names = await asyncio.to_thread(self.ctrl.suggest_places, place.value or "")
            names = [n for n in names if n != place.value][:4]
            suggestions.controls = [
                ft.Chip(label=ft.Text(n), on_click=lambda e, n=n: (setattr(place, "value", n),
                                                                  suggestions.controls.clear(),
                                                                  self.page.update()))
                for n in names
            ]
            suggestions.update()

        place.on_change = suggest

        def pick_category(name: str):
            def handler(e):
                category.value = name
                category_chips.controls.clear()
                self.page.update()
            return handler

        async def suggest_category(e=None):
            names = await asyncio.to_thread(self.ctrl.suggest_categories, category.value or "")
            names = [n for n in names if n != category.value]
            category_chips.controls = [
                ft.Chip(label=ft.Text(n), on_click=pick_category(n)) for n in names
            ]
            _refresh(category_chips)

        category.on_change = suggest_category

        async def pick_date(e):
            def on_date(ev):
                if ev.control.value:
                    when.value = fmt.date_input(fmt.picked_date(ev.control.value))
                    when.error = None
                    when.update()

            try:
                current = fmt.parse_date(when.value) or date.today()
            except ValidationError:
                current = date.today()
            self.page.show_dialog(ft.DatePicker(
                value=datetime(current.year, current.month, current.day), on_change=on_date,
                first_date=datetime(2000, 1, 1), last_date=datetime(2100, 12, 31),
            ))

        async def save(e):
            for field in (total, place, when):
                field.error = None
            try:
                amount = fmt.parse_money(total.value)
            except ValidationError as exc:
                total.error = str(exc)
                self.page.update()
                return
            try:
                picked_date = fmt.parse_date(when.value) or date.today()
            except ValidationError as exc:
                when.error = str(exc)
                self.page.update()
                return
            # Si al editar no se tocó la fecha, se conserva el valor guardado tal cual.
            if expense and when.value.strip() == original_date:
                occurred: Any = expense.ocurrido_en
            else:
                occurred = picked_date
            items = []
            for row in items_col.controls:
                name_f, qty_f, val_f = row.data
                if not (name_f.value or "").strip() and not (val_f.value or "").strip():
                    continue
                try:
                    unit = fmt.parse_money(val_f.value or "0")
                except ValidationError as exc:
                    val_f.error = str(exc)
                    self.page.update()
                    return
                items.append(ItemInput(name_f.value or "", str(unit), int(qty_f.value or 1)))
            data = ExpenseInput(
                total=str(amount), lugar=place.value or "", ocurrido_en=occurred,
                nota=note.value, items=tuple(items), categoria=category.value,
            )
            if expense is None:
                ok, _ = await self.call(self.ctrl.add_expense, data)
            else:
                ok, _ = await self.call(self.ctrl.update_expense, expense.id, data)
            if ok:
                self.page.pop_dialog()
                self.toast("Gasto guardado" if expense is None else "Gasto actualizado")
                await self.refresh()

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Nuevo gasto" if expense is None else "Editar gasto"),
                content=ft.Container(
                    ft.Column(
                        [
                            total, place, suggestions, category, category_chips,
                            ft.Row([when, ft.IconButton(ft.Icons.CALENDAR_MONTH, on_click=pick_date)]),
                            note,
                            ft.Row([ft.Text("Ítems (opcional)", weight=ft.FontWeight.W_600, expand=True),
                                    ft.IconButton(ft.Icons.ADD, tooltip="Agregar ítem",
                                                  on_click=lambda e: add_item())]),
                            items_col,
                            summary,
                        ],
                        tight=True,
                        spacing=10,
                    ),
                    width=420,
                ),
                scrollable=True,
                actions=[
                    ft.TextButton("Cancelar", on_click=lambda e: self.page.pop_dialog()),
                    ft.FilledButton("Guardar", on_click=save),
                ],
            )
        )
        update_summary()
        await suggest_category()

    async def on_budget(self, e) -> None:
        if not await self.ensure_unlocked():
            return
        amount = ft.TextField(label="Presupuesto mensual", prefix="$ ", autofocus=True,
                              keyboard_type=ft.KeyboardType.NUMBER)
        done: asyncio.Future = asyncio.get_running_loop().create_future()

        async def save(ev):
            try:
                value = fmt.parse_money(amount.value)
            except ValidationError as exc:
                amount.error = str(exc)
                amount.update()
                return
            if (await self.call(self.ctrl.set_budget, self.mes, str(value)))[0]:
                self.page.pop_dialog()
                done.set_result(True)

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Presupuesto"),
                content=ft.Column(
                    [amount, ft.Text(f"Rige desde {fmt.month_label(self.mes)} en adelante.", size=12)],
                    tight=True,
                ),
                actions=[ft.TextButton("Cancelar", on_click=lambda ev: self.page.pop_dialog()),
                         ft.FilledButton("Guardar", on_click=save)],
            )
        )
        if await done:
            await self.refresh()

    # =================================================================== PIN
    def pin_field(self, label: str, autofocus: bool = False) -> ft.TextField:
        return ft.TextField(
            label=label, password=True, can_reveal_password=True, max_length=4,
            keyboard_type=ft.KeyboardType.NUMBER, input_filter=ft.NumbersOnlyInputFilter(),
            autofocus=autofocus, text_align=ft.TextAlign.CENTER,
        )

    async def pin_setup_dialog(self) -> None:
        first, second = self.pin_field("PIN de 4 dígitos", True), self.pin_field("Repite el PIN")
        done: asyncio.Future = asyncio.get_running_loop().create_future()

        async def save(e):
            second.error = None
            if first.value != second.value:
                second.error = "No coincide"
                self.page.update()
                return
            if (await self.call(self.ctrl.setup_pin, first.value or ""))[0]:
                self.page.pop_dialog()
                done.set_result(True)

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Crea un PIN"),
                content=ft.Column(
                    [ft.Text("Lo pedirá la app para editar, borrar o cambiar el presupuesto. "
                             "Registrar gastos nuevos no lo pide."), first, second],
                    tight=True,
                ),
                actions=[ft.FilledButton("Guardar", on_click=save)],
            )
        )
        await done

    async def ensure_unlocked(self) -> bool:
        if self.ctrl.is_unlocked:
            return True
        pin = self.pin_field("PIN", True)
        done: asyncio.Future = asyncio.get_running_loop().create_future()

        async def submit(e):
            pin.error = None
            ok, _ = await self.call(self.ctrl.unlock, pin.value or "", quiet=True, show_error=pin)
            if ok:
                self.page.pop_dialog()
                self.update_status_icons()
                self.page.update()
                if not done.done():
                    done.set_result(True)

        def cancel(e):
            self.page.pop_dialog()
            if not done.done():
                done.set_result(False)

        pin.on_submit = submit
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Desbloquear edición"),
                content=ft.Column([pin], tight=True),
                actions=[ft.TextButton("Cancelar", on_click=cancel),
                         ft.FilledButton("Desbloquear", on_click=submit)],
            )
        )
        return await done

    async def on_lock_click(self, e) -> None:
        if self.ctrl.is_unlocked:
            self.ctrl.lock_editing()
            self.toast("Edición bloqueada")
        else:
            await self.ensure_unlocked()
        self.update_status_icons()
        self.page.update()

    async def on_change_pin(self, e) -> None:
        current, new = self.pin_field("PIN actual", True), self.pin_field("PIN nuevo")

        async def save(ev):
            if (await self.call(self.ctrl.change_pin, current.value or "", new.value or ""))[0]:
                self.page.pop_dialog()
                self.toast("PIN actualizado")

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Cambiar PIN"),
                content=ft.Column([current, new], tight=True),
                actions=[ft.TextButton("Cancelar", on_click=lambda ev: self.page.pop_dialog()),
                         ft.FilledButton("Guardar", on_click=save)],
            )
        )

    # ======================================================= entornos y cuenta
    async def on_new_env(self, e) -> None:
        name = ft.TextField(label="Nombre", hint_text="gastos_privados", autofocus=True)

        async def create(ev):
            self.show_busy(True)
            ok, remote = await self.call(self.ctrl.create_environment, name.value or "")
            self.show_busy(False)
            if ok:
                self.page.pop_dialog()
                self.catalog_items = sorted(self.catalog_items + [remote], key=lambda r: r.name.lower())
                await self.open_environment(remote)

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Nuevo entorno"),
                content=ft.Column([name, ft.Text("Necesita conexión: se crea también en Drive.",
                                                 size=12)], tight=True),
                actions=[ft.TextButton("Cancelar", on_click=lambda ev: self.page.pop_dialog()),
                         ft.FilledButton("Crear", on_click=create)],
            )
        )

    async def on_account(self, e) -> None:
        if not self.ctrl.online:
            self.show_login()
            return
        if await self.confirm("¿Cerrar sesión de Google?",
                              "Tus datos quedan en este equipo. Para sincronizar tendrás que "
                              "volver a iniciar sesión."):
            await self.call(self.ctrl.logout)
            self.online = False
            self.show_main()

    async def ask_conflict(self, detail: str) -> None:
        done: asyncio.Future = asyncio.get_running_loop().create_future()

        def choose(resolution):
            async def handler(e):
                self.page.pop_dialog()
                done.set_result(resolution)
            return handler

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Hay dos versiones"),
                content=ft.Text(
                    f"{detail}\n\nNada se ha sobrescrito. Elige qué conservar:\n"
                    "• Este equipo: Drive queda con lo de aquí.\n"
                    "• Drive: este equipo adopta lo de Drive (se guarda una copia .bak).\n"
                    "• Ambas: lo de aquí se sube como un entorno aparte."
                ),
                actions=[
                    ft.TextButton("Decidir luego", on_click=choose(None)),
                    ft.TextButton("Ambas", on_click=choose(Resolution.KEEP_BOTH)),
                    ft.TextButton("Drive", on_click=choose(Resolution.KEEP_REMOTE)),
                    ft.FilledButton("Este equipo", on_click=choose(Resolution.KEEP_LOCAL)),
                ],
            )
        )
        resolution = await done
        if resolution is None:
            return
        self.show_busy(True)
        ok, env = await self.call(self.ctrl.resolve_conflict, resolution)
        self.show_busy(False)
        if ok:
            self.last_sync = SyncResult(SyncStatus.UP_TO_DATE)
            await self.load_catalog()
            self.show_main()
            await self.refresh()

    # ========================================================= sincronización
    def update_status_icons(self) -> None:
        kind, text = describe_sync(self.last_sync)
        if not self.ctrl.online:
            kind, text = "offline", "Sin sesión de Google: los cambios se guardan aquí"
        self.sync_button.icon = {
            "ok": ft.Icons.CLOUD_DONE, "offline": ft.Icons.CLOUD_OFF, "conflict": ft.Icons.SYNC_PROBLEM
        }[kind]
        self.sync_button.tooltip = text
        unlocked = self.ctrl.is_unlocked
        self.lock_button.icon = ft.Icons.LOCK_OPEN if unlocked else ft.Icons.LOCK
        self.lock_button.tooltip = "Edición desbloqueada" if unlocked else "Solo lectura"

    async def on_sync_click(self, e) -> None:
        if self.last_sync and self.last_sync.status is SyncStatus.CONFLICT:
            await self.ask_conflict(self.last_sync.detail)
            return
        self.show_busy(True)
        ok, result = await self.call(self.ctrl.sync_in_background)
        self.show_busy(False)
        if ok:
            self.last_sync = result or self.last_sync
            self.toast(describe_sync(result)[1])
            self.update_status_icons()
            self.page.update()
            if result and result.status is SyncStatus.CONFLICT:
                await self.ask_conflict(result.detail)

    async def on_lifecycle(self, e) -> None:
        # Android puede cerrar la app en segundo plano sin aviso: se sube al salir.
        if e.state in (ft.AppLifecycleState.PAUSE, ft.AppLifecycleState.HIDE):
            ok, result = await self.call(self.ctrl.sync_in_background, quiet=True)
            if ok and result:
                self.last_sync = result

    async def on_window_event(self, e) -> None:
        if e.type != ft.WindowEventType.CLOSE:
            return
        self.show_center(ft.ProgressRing(), "Guardando en Drive…")
        await self.call(self.ctrl.sync_on_exit, quiet=True)
        await self.page.window.destroy()

    # ================================================================ apoyo
    async def call(self, fn: Callable, *args, quiet: bool = False, show_error=None):
        """Ejecuta fuera del hilo de eventos. Devuelve (ok, resultado)."""
        try:
            return True, await asyncio.to_thread(fn, *args)
        except EXPECTED_ERRORS as exc:
            message = user_message(exc)
            if show_error is not None:
                show_error.error = message
                self.page.update()
            elif not quiet:
                self.toast(message)
            return False, None

    async def confirm(self, title: str, text: str) -> bool:
        done: asyncio.Future = asyncio.get_running_loop().create_future()

        def answer(value):
            def handler(e):
                self.page.pop_dialog()
                done.set_result(value)
            return handler

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text(title), content=ft.Text(text),
                actions=[ft.TextButton("Cancelar", on_click=answer(False)),
                         ft.FilledButton("Sí", on_click=answer(True))],
            )
        )
        return await done

    def toast(self, text: str) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(text)))

    def show_busy(self, value: bool) -> None:
        self.busy.visible = value
        _refresh(self.busy)

    def show_center(self, icon: ft.Control, title: str, text: str = "",
                    actions: list[ft.Control] | None = None) -> None:
        self.page.appbar = None
        self.page.floating_action_button = None
        self.page.controls.clear()
        column = ft.Column(
            [
                icon,
                ft.Text(title, size=20, weight=ft.FontWeight.W_600, text_align=ft.TextAlign.CENTER),
                *([ft.Text(text, text_align=ft.TextAlign.CENTER,
                           color=ft.Colors.ON_SURFACE_VARIANT)] if text else []),
                *(actions or []),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=16,
            width=360,
        )
        self.page.add(
            ft.SafeArea(ft.Container(column, align=ft.Alignment.CENTER, padding=24, expand=True),
                        expand=True)
        )
        self.page.update()


OFFLINE_MESSAGE = (
    "Sin conexión con Google. Revisa que el equipo tenga internet e intenta de nuevo. "
    "Mientras tanto, la app sigue funcionando con los datos guardados aquí."
)


def user_message(exc: Exception) -> str:
    """Texto para el usuario. Los errores de red de las librerías llegan en inglés y
    con detalles técnicos (DNS, puertos); aquí se resumen en una frase."""
    if isinstance(exc, (AuthOfflineError, OfflineError)):
        return OFFLINE_MESSAGE
    return str(exc)


def _refresh(control: ft.Control) -> None:
    """Actualiza un control solo si ya está en pantalla.

    En Flet 1.0, leer `control.page` antes de agregarlo lanza RuntimeError en vez de
    devolver None; pasa al precargar los ítems de un gasto que se va a editar.
    """
    try:
        mounted = control.page is not None
    except RuntimeError:
        mounted = False
    if mounted:
        control.update()


def _nice_step(raw: float) -> float:
    """1, 2 o 5 × potencia de 10, para que las etiquetas del eje sean redondas."""
    if raw <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def _frange(start: float, stop: float, step: float) -> list[float]:
    values, value = [], start
    while value <= stop + 1e-9:
        values.append(value)
        value += step
    return values


async def main(page: ft.Page) -> None:
    await FinanzasApp(page).start()
