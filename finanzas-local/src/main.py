"""Punto de entrada para `flet run` y `flet build` (Windows y Android)."""

import flet as ft

from finanzas.ui.app import main

if __name__ == "__main__":
    ft.run(main)
