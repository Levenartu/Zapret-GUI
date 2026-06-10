"""
Zapret Interface — слой отображения (UI) на Flet (актуальный синтаксис 0.25+).
Вся бизнес-логика делегируется в zapret_core.ZapretEngine.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import flet as ft
from PIL import Image, ImageDraw

from zapret_core import (
    GAME_FILTER_MODES,
    ProcessState,
    ZapretConfig,
    ZapretEngine,
    clear_discord_cache,
    get_app_dir,
    is_valid_zapret_root,
    run_in_background,
    scan_bat_configs,
)

# ---------------------------------------------------------------------------
# Палитра (Soft Dark / Google AI Studio style)
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#0f1012",
    "sidebar": "#131416",
    "card": "#181a1f",
    "sidebar_active": "#252830",
    "border": "#2c2e33",
    "accent": "#4f46e5",
    "accent_hover": "#818cf8",
    "text": "#e8eaed",
    "text_muted": "#9aa0a6",
    "text_faint": "#5f6368",
    "success": "#34d399",
    "success_dim": "#064e3b",
    "danger": "#f87171",
    "danger_hover": "#ef4444",
    "warning": "#fbbf24",
    "footer": "#0a0b0d",
}

SIDEBAR_W = 72
BTN_W = 220
STATUS_POLL_MS = 2500

# ---------------------------------------------------------------------------
# UAC
# ---------------------------------------------------------------------------


def is_user_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    if is_user_admin():
        return
    script = os.path.abspath(sys.argv[0])
    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    if script.lower().endswith((".py", ".pyw")):
        exe = sys.executable
        if script.lower().endswith(".pyw"):
            pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if os.path.isfile(pw):
                exe = pw
        params = f'"{script}"' + (f" {params}" if params else "")
    else:
        exe = script
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params or None, str(get_app_dir()), 0)
    sys.exit(0)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


def _dbg_log(location: str, message: str, data: dict | None = None, hypothesis_id: str = "A"):
    # #region agent log
    try:
        with open("debug-887490.log", "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": "887490",
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                        "hypothesisId": hypothesis_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion


class ZapretGUI:
    def __init__(self, page: ft.Page):
        self.page = page
        self._loop = asyncio.get_running_loop()
        self._quitting = False
        self._ready = False
        self._last_state = None
        self._config = ZapretConfig()
        self._zapret_root: Path | None = self._config.resolve_zapret_root()
        self._engine: ZapretEngine | None = None
        self._bat_map: dict[str, Path] = {}
        self._tray_icon = None
        self._tray_thread = None
        self._folder_picker_mode = "setup"

        self._setup_page()

        # Flet 0.85+: FilePicker is a Service, registered via page.services
        # (NOT page.overlay, and it no longer uses the on_result event).
        self._folder_picker = ft.FilePicker()
        page.services.append(self._folder_picker)

        self._build_layout()
        _dbg_log("gui.py:__init__", "UI layout built", {"zapret_root": str(self._zapret_root)}, "A")

        if not self._zapret_root:
            self._show_path_setup()
        else:
            relaunch_as_admin()
            self._init_app()

    # ---- Page chrome --------------------------------------------------------

    def _setup_page(self):
        p = self.page
        p.title = "Zapret Interface"
        p.theme_mode = ft.ThemeMode.DARK
        p.bgcolor = COLORS["bg"]
        p.padding = 0
        p.spacing = 0
        p.window.width = 900
        p.window.height = 720
        p.window.min_width = 700
        p.window.min_height = 550
        p.window.prevent_close = True
        p.window.on_event = self._on_window_event

    def _on_window_event(self, e):
        # Flet 0.85+ delivers the kind in e.type (WindowEventType); keep e.data
        # fallback for safety across versions.
        etype = getattr(e, "type", None)
        is_close = etype == ft.WindowEventType.CLOSE or getattr(e, "data", None) == "close"
        if is_close:
            if not self._ready:
                # Not yet fully initialized - do a clean quit
                self._quit()
                return
            self._hide_to_tray()

    def _build_layout(self):
        self._sidebar = self._build_sidebar()
        self._content = ft.Container(
            expand=True,
            bgcolor=COLORS["bg"],
            padding=20,
        )
        self._footer = self._build_footer()

        self.page.add(
            ft.Column(
                [
                    ft.Row(
                        [
                            self._sidebar,
                            ft.VerticalDivider(width=1, color=COLORS["border"]),
                            self._content,
                        ],
                        expand=True,
                        spacing=0,
                    ),
                    ft.Divider(height=1, color=COLORS["border"]),
                    self._footer,
                ],
                expand=True,
                spacing=0,
            )
        )

    # ---- Sidebar ------------------------------------------------------------

    def _build_sidebar(self):
        items = [
            ("main", ft.Icons.HOME, "Главная"),
            ("lists", ft.Icons.LIST, "Списки"),
            ("service", ft.Icons.SETTINGS, "Сервис"),
        ]

        self._nav_buttons = {}

        def make_btn(key, icon, tooltip):
            is_active = key == "main"
            btn = ft.Container(
                content=ft.Icon(
                    icon,
                    color=COLORS["text"] if is_active else COLORS["text_muted"],
                    size=22,
                ),
                width=44,
                height=44,
                bgcolor=COLORS["sidebar_active"] if is_active else None,
                border_radius=10,
                alignment=ft.Alignment.CENTER,
                tooltip=tooltip,
                animate=ft.Animation(200, ft.AnimationCurve.EASE_IN_OUT),
                on_click=lambda e, k=key: self._select_tab(k),
                on_hover=self._on_nav_hover,
            )
            self._nav_buttons[key] = btn
            return btn

        nav_items = [
            ft.Container(
                content=ft.Text(
                    "Z",
                    size=20,
                    weight=ft.FontWeight.BOLD,
                    color=COLORS["accent_hover"],
                ),
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding.only(top=16, bottom=20),
            )
        ]
        for key, icon, tooltip in items:
            nav_items.append(
                ft.Container(
                    content=make_btn(key, icon, tooltip),
                    alignment=ft.Alignment.CENTER,
                    padding=ft.Padding.symmetric(vertical=4),
                )
            )

        return ft.Container(
            width=SIDEBAR_W,
            bgcolor=COLORS["sidebar"],
            padding=ft.Padding.symmetric(horizontal=14),
            content=ft.Column(nav_items, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        )

    def _on_nav_hover(self, e: ft.HoverEvent):
        ctrl = e.control
        if ctrl.bgcolor == COLORS["sidebar_active"]:
            return
        ctrl.bgcolor = COLORS["sidebar_active"] if e.data == "true" else None
        ctrl.update()

    def _select_tab(self, key: str):
        for k, btn in self._nav_buttons.items():
            active = k == key
            btn.content.color = COLORS["text"] if active else COLORS["text_muted"]
            btn.bgcolor = COLORS["sidebar_active"] if active else None
            btn.update()

        if key == "main":
            self._content.content = self._main_tab
        elif key == "lists":
            self._content.content = self._lists_tab
            self._load_lists()
        elif key == "service":
            self._content.content = self._service_tab
            self._refresh_svc()
            self._sync_filters()

        self._content.update()

    # ---- Footer -------------------------------------------------------------

    def _build_footer(self):
        self._status_text = ft.Text(
            "Готов к работе",
            size=12,
            color=COLORS["text_muted"],
        )
        return ft.Container(
            height=32,
            bgcolor=COLORS["footer"],
            padding=ft.Padding.symmetric(horizontal=12),
            content=ft.Row(
                [
                    self._status_text,
                    ft.Container(expand=True),
                    ft.Text(
                        "made by Levenartu",
                        size=10,
                        color=COLORS["text_faint"],
                        opacity=0.6,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def set_status(self, text: str, kind: str = "info"):
        prefixes = ("ℹ️", "✅", "❌", "⏳", "⚠️")
        icons = {"info": "ℹ️", "success": "✅", "error": "❌", "loading": "⏳", "warn": "⚠️"}
        if not any(text.startswith(p) for p in prefixes):
            text = f"{icons.get(kind, 'ℹ️')} {text}"
        colors = {
            "info": COLORS["text_muted"],
            "success": COLORS["success"],
            "error": COLORS["danger"],
            "loading": COLORS["accent_hover"],
            "warn": COLORS["warning"],
        }
        self._status_text.value = text
        self._status_text.color = colors.get(kind, COLORS["text_muted"])
        self._status_text.update()

    # ---- Path setup ---------------------------------------------------------

    def _show_path_setup(self):
        card = self._make_card(
            "Настройка пути",
            "Укажите корневую папку zapret (bin\\winws.exe)",
            ft.Container(
                content=ft.ElevatedButton(
                    "Выбрать папку",
                    width=BTN_W,
                    height=42,
                    style=ft.ButtonStyle(
                        bgcolor=COLORS["accent"],
                        color=COLORS["text"],
                        shape=ft.RoundedRectangleBorder(radius=10),
                    ),
                    on_click=lambda _: self._pick_folder("setup"),
                ),
                alignment=ft.Alignment.CENTER,
                padding=20,
            ),
        )
        self._content.content = ft.Container(
            content=card,
            alignment=ft.Alignment.CENTER,
        )
        self._content.update()

    def _pick_folder(self, mode: str):
        self._folder_picker_mode = mode
        # Flet 0.85+: get_directory_path() is an async method that returns the
        # selected path directly (the old on_result event no longer exists).
        self.page.run_task(self._pick_folder_async)

    async def _pick_folder_async(self):
        try:
            path = await self._folder_picker.get_directory_path(
                dialog_title="Выберите корневую папку zapret"
            )
        except Exception as exc:
            self.set_status(f"Не удалось открыть диалог: {exc}", "error")
            return
        self._on_folder_picked(path)

    def _on_folder_picked(self, path: str | None):
        if not path:
            return
        p = Path(path)
        if not is_valid_zapret_root(p):
            self.set_status("В папке нет bin\\winws.exe", "error")
            return
        self._config.set_zapret_root(p)
        self._zapret_root = p.resolve()
        self.set_status(f"Путь сохранён: {p}", "success")

        if self._folder_picker_mode == "setup":
            relaunch_as_admin()
            self._init_app()
        else:
            self._engine = ZapretEngine(self._zapret_root)
            # Re-init tray with new engine so menu callbacks work correctly
            self._setup_tray()
            self._refresh_bats()
            self._apply_state(ProcessState.STOPPED)
            self._select_tab("main")

    # ---- Common widgets ------------------------------------------------------

    def _make_card(self, title: str, subtitle: str = "", body_content=None):
        widgets = [
            ft.Text(title, size=14, weight=ft.FontWeight.BOLD, color=COLORS["text"]),
        ]
        if subtitle:
            widgets.append(
                ft.Text(subtitle, size=11, color=COLORS["text_muted"])
            )
        if body_content:
            widgets.append(
                ft.Container(content=body_content, padding=ft.Padding.only(top=12))
            )
        return ft.Container(
            content=ft.Column(widgets, spacing=4),
            bgcolor=COLORS["card"],
            border_radius=12,
            border=ft.Border.all(width=1, color=COLORS["border"]),
            padding=16,
        )

    def _gradient_button(self, text: str, on_click, width=BTN_W, height=48, color=COLORS["text"]):
        return ft.Container(
            content=ft.Text(
                text,
                size=15,
                weight=ft.FontWeight.BOLD,
                color=color,
                text_align=ft.TextAlign.CENTER,
            ),
            width=width,
            height=height,
            border_radius=10,
            gradient=ft.LinearGradient(
                colors=["#6366f1", "#8b5cf6", "#ec4899"],
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
            ),
            alignment=ft.Alignment.CENTER,
            on_click=on_click,
            animate=ft.Animation(200, ft.AnimationCurve.EASE_IN_OUT),
            on_hover=self._btn_hover,
        )

    def _btn_hover(self, e: ft.HoverEvent):
        e.control.opacity = 0.85 if e.data == "true" else 1.0
        e.control.update()

    def _secondary_button(self, text: str, on_click, width=BTN_W, height=40):
        return ft.Container(
            content=ft.Text(
                text,
                size=13,
                color=COLORS["text"],
                text_align=ft.TextAlign.CENTER,
            ),
            width=width,
            height=height,
            border_radius=10,
            bgcolor=COLORS["sidebar_active"],
            border=ft.Border.all(width=1, color=COLORS["border"]),
            alignment=ft.Alignment.CENTER,
            on_click=on_click,
            animate=ft.Animation(150, ft.AnimationCurve.EASE_IN_OUT),
            on_hover=self._btn_hover,
        )

    # ---- Init ---------------------------------------------------------------

    def _init_app(self):
        assert self._zapret_root
        self._engine = ZapretEngine(self._zapret_root)
        self._setup_tray()
        self._build_tabs()
        self._apply_bats(scan_bat_configs(self._zapret_root))
        self._apply_state(ProcessState.STOPPED)
        self._ready = True
        self._select_tab("main")
        self._poll()
        self.set_status("Zapret Interface готов", "success")

    def _build_tabs(self):
        self._main_tab = self._build_main_tab()
        self._lists_tab = self._build_lists_tab()
        self._service_tab = self._build_service_tab()

    # ---- Main tab -----------------------------------------------------------

    def _build_main_tab(self):
        self._bat_dropdown = ft.Dropdown(
            width=400,
            height=44,
            bgcolor=COLORS["sidebar"],
            border_color=COLORS["border"],
            color=COLORS["text"],
            border_radius=8,
            text_size=13,
            hint_text="Выберите .bat конфиг",
            options=[],
            filled=True,
            fill_color=COLORS["sidebar"],
            focused_border_color=COLORS["accent"],
        )

        refresh_btn = ft.IconButton(
            icon=ft.Icons.REFRESH,
            icon_color=COLORS["text_muted"],
            icon_size=20,
            tooltip="Обновить список",
            on_click=lambda _: self._refresh_bats(),
        )

        dropdown_row = ft.Row(
            [self._bat_dropdown, refresh_btn],
            spacing=8,
        )

        strategy_card = self._make_card(
            "Стратегия",
            "Выберите .bat из корня zapret",
            dropdown_row,
        )

        self._btn_toggle = self._gradient_button(
            "Запустить обход", self._on_toggle, width=BTN_W, height=48
        )
        self._btn_restart = self._secondary_button(
            "Перезапустить обход", self._on_restart, width=BTN_W, height=40
        )

        actions = ft.Column(
            [self._btn_toggle, ft.Container(height=8), self._btn_restart],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
        actions_container = ft.Container(
            content=actions,
            alignment=ft.Alignment.CENTER,
            padding=20,
        )

        self._status_dot = ft.Container(
            width=12,
            height=12,
            border_radius=6,
            bgcolor=COLORS["danger"],
            shadow=ft.BoxShadow(
                spread_radius=1,
                blur_radius=8,
                color=COLORS["danger"],
                offset=ft.Offset(0, 0),
            ),
        )
        self._status_label = ft.Text(
            "Остановлен",
            size=14,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text"],
        )

        status_row = ft.Row(
            [self._status_dot, self._status_label],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        status_card = self._make_card("Статус", body_content=status_row)

        path = str(self._zapret_root)
        if len(path) > 60:
            path = "…" + path[-57:]

        env_content = ft.Column(
            [
                ft.Text(path, size=10, color=COLORS["text_muted"]),
                ft.Text(
                    "Администратор" if is_user_admin() else "Нужны права администратора",
                    size=10,
                    color=COLORS["success"] if is_user_admin() else COLORS["warning"],
                ),
                ft.Container(
                    content=ft.TextButton(
                        "Сменить папку",
                        style=ft.ButtonStyle(
                            color=COLORS["text_muted"],
                            text_style=ft.TextStyle(size=10),
                        ),
                        on_click=lambda _: self._change_folder(),
                    ),
                    padding=ft.Padding.only(top=6),
                ),
            ],
            spacing=2,
        )
        env_card = self._make_card("Окружение", body_content=env_content)

        return ft.Column(
            [
                ft.Text("Zapret Interface", size=24, weight=ft.FontWeight.BOLD, color=COLORS["text"]),
                ft.Text("Advanced Bypass Control", size=12, color=COLORS["text_muted"]),
                ft.Container(height=18),
                strategy_card,
                ft.Container(height=12),
                actions_container,
                ft.Container(height=12),
                status_card,
                ft.Container(height=12),
                env_card,
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # ---- Lists tab ----------------------------------------------------------

    def _build_lists_tab(self):
        self._lists_field = ft.TextField(
            multiline=True,
            min_lines=20,
            expand=True,
            bgcolor=COLORS["card"],
            border_color=COLORS["border"],
            color=COLORS["text"],
            border_radius=10,
            text_size=13,
            hint_text="Список сайтов...",
            hint_style=ft.TextStyle(color=COLORS["text_faint"]),
            filled=True,
            fill_color=COLORS["card"],
            focused_border_color=COLORS["accent"],
            cursor_color=COLORS["text"],
            selection_color=COLORS["accent"],
        )

        save_btn = ft.ElevatedButton(
            "Сохранить изменения",
            width=BTN_W,
            height=40,
            style=ft.ButtonStyle(
                bgcolor=COLORS["accent"],
                color=COLORS["text"],
                shape=ft.RoundedRectangleBorder(radius=10),
            ),
            on_click=lambda _: self._save_lists(),
        )

        return ft.Column(
            [
                ft.Text("Списки сайтов", size=20, weight=ft.FontWeight.BOLD, color=COLORS["text"]),
                ft.Container(height=8),
                self._lists_field,
                ft.Container(height=8),
                save_btn,
            ],
            expand=True,
        )

    # ---- Service tab --------------------------------------------------------

    def _build_service_tab(self):
        self._svc_label = ft.Text("—", size=11, color=COLORS["text_muted"])

        svc_body = ft.Column(
            [
                self._svc_label,
                ft.Container(height=6),
                ft.ElevatedButton(
                    "Установить автозапуск (как службу)",
                    width=400,
                    height=38,
                    style=ft.ButtonStyle(
                        bgcolor=COLORS["accent"],
                        color=COLORS["text"],
                        elevation=0,
                        shape=ft.RoundedRectangleBorder(radius=8),
                    ),
                    on_click=lambda _: self._install_svc(),
                ),
                ft.ElevatedButton(
                    "Удалить службу из системы",
                    width=400,
                    height=38,
                    style=ft.ButtonStyle(
                        bgcolor=COLORS["sidebar"],
                        color=COLORS["text"],
                        elevation=0,
                        shape=ft.RoundedRectangleBorder(radius=8),
                        side=ft.BorderSide(1, COLORS["border"]),
                    ),
                    on_click=lambda _: self._remove_svc(),
                ),
            ],
            spacing=8,
        )
        svc_card = self._make_card("Служба Windows", "Автозапуск при загрузке системы", svc_body)

        game_options = [ft.DropdownOption(text=v, key=k) for k, v in GAME_FILTER_MODES.items()]
        self._game_dropdown = ft.Dropdown(
            label="Game Filter",
            options=game_options,
            width=400,
            height=44,
            bgcolor=COLORS["sidebar"],
            border_color=COLORS["border"],
            color=COLORS["text"],
            border_radius=8,
            on_select=self._on_game,
            filled=True,
            fill_color=COLORS["sidebar"],
            focused_border_color=COLORS["accent"],
        )

        self._ipset_switch = ft.Switch(
            label="IPSet Filter",
            value=False,
            active_track_color=COLORS["accent_hover"],
            on_change=self._on_ipset,
            label_text_style=ft.TextStyle(color=COLORS["text"]),
        )

        filter_body = ft.Column([self._game_dropdown, self._ipset_switch], spacing=8)
        _dbg_log("gui.py:_build_service_tab", "service tab controls built", {}, "B")
        filter_card = self._make_card("Фильтры", body_content=filter_body)

        tools = [
            ("Обновить IP-сеты (Update IPsets)", self._update_ipsets),
            ("Обновить Hosts (Update Hosts)", self._update_hosts),
            ("Очистить кэш Discord", self._clear_discord),
            ("Запустить диагностику системы", self._run_diag),
        ]
        tools_body = ft.Column(
            [
                ft.TextButton(
                    text,
                    width=400,
                    style=ft.ButtonStyle(
                        color=COLORS["text"],
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                        shape=ft.RoundedRectangleBorder(radius=8),
                        bgcolor=COLORS["sidebar"],
                        side=ft.BorderSide(1, COLORS["border"]),
                    ),
                    on_click=lambda _, c=cmd: c(),
                )
                for text, cmd in tools
            ],
            spacing=6,
        )
        tools_card = self._make_card("Инструменты", body_content=tools_body)

        self._diag_log = ft.TextField(
            multiline=True,
            min_lines=10,
            read_only=True,
            value="",
            bgcolor=COLORS["bg"],
            border_color=COLORS["border"],
            color=COLORS["text_muted"],
            border_radius=8,
            text_size=11,
            hint_text="Ожидание запуска диагностики…",
            hint_style=ft.TextStyle(color=COLORS["text_faint"]),
            filled=True,
            fill_color=COLORS["bg"],
            focused_border_color=COLORS["border"],
            cursor_color=COLORS["text_muted"],
            selection_color=COLORS["accent"],
        )
        log_card = self._make_card("Лог диагностики", body_content=self._diag_log)

        return ft.Column(
            [
                ft.Text("Сервис и утилиты", size=20, weight=ft.FontWeight.BOLD, color=COLORS["text"]),
                ft.Container(height=12),
                svc_card,
                ft.Container(height=10),
                filter_card,
                ft.Container(height=10),
                tools_card,
                ft.Container(height=10),
                log_card,
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # ---- Background runner --------------------------------------------------

    def _run_bg(self, func, on_success=None, on_error=None):
        def worker():
            try:
                result = func()
                if on_success:
                    self._loop.call_soon_threadsafe(on_success, result)
            except Exception as exc:
                if on_error:
                    self._loop.call_soon_threadsafe(on_error, exc)
        threading.Thread(target=worker, daemon=True).start()

    # ---- Bats / state -------------------------------------------------------

    def _refresh_bats(self, _=None):
        root = self._zapret_root
        self._run_bg(lambda: scan_bat_configs(root), on_success=self._apply_bats)

    def _apply_bats(self, configs: list[Path]):
        self._bat_map = {p.name: p for p in configs} or {"general.bat": self._zapret_root / "general.bat"}
        names = list(self._bat_map.keys())
        self._bat_dropdown.options = [ft.DropdownOption(text=n) for n in names]
        self._bat_dropdown.value = "general.bat" if "general.bat" in names else names[0]
        if self._ready:
            self._bat_dropdown.update()

    def _bat(self) -> Path:
        n = self._bat_dropdown.value
        if n not in self._bat_map:
            raise FileNotFoundError(f"Нет конфигурации: {n}")
        return self._bat_map[n]

    def _apply_state(self, state: ProcessState):
        self._last_state = state
        # Guard: widgets may not exist yet if called before _build_tabs
        if not hasattr(self, "_btn_toggle"):
            return
        if state == ProcessState.RUNNING:
            self._btn_toggle.content.value = "Остановить обход"
            self._btn_toggle.gradient = ft.LinearGradient(
                colors=["#ef4444", "#f87171"],
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
            )
            self._status_dot.bgcolor = COLORS["success"]
            self._status_dot.shadow = ft.BoxShadow(
                spread_radius=1,
                blur_radius=8,
                color=COLORS["success"],
                offset=ft.Offset(0, 0),
            )
            self._status_label.value = "Обход активен"
            self._bat_dropdown.disabled = True
        elif state == ProcessState.CRASHED:
            self._btn_toggle.content.value = "Запустить обход"
            self._btn_toggle.gradient = ft.LinearGradient(
                colors=["#6366f1", "#8b5cf6", "#ec4899"],
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
            )
            self._status_dot.bgcolor = COLORS["danger"]
            self._status_dot.shadow = ft.BoxShadow(
                spread_radius=1,
                blur_radius=8,
                color=COLORS["danger"],
                offset=ft.Offset(0, 0),
            )
            self._status_label.value = "Остановлен / Ошибка"
            self._bat_dropdown.disabled = False
        else:
            self._btn_toggle.content.value = "Запустить обход"
            self._btn_toggle.gradient = ft.LinearGradient(
                colors=["#6366f1", "#8b5cf6", "#ec4899"],
                begin=ft.Alignment.TOP_LEFT,
                end=ft.Alignment.BOTTOM_RIGHT,
            )
            self._status_dot.bgcolor = COLORS["danger"]
            self._status_dot.shadow = ft.BoxShadow(
                spread_radius=1,
                blur_radius=8,
                color=COLORS["danger"],
                offset=ft.Offset(0, 0),
            )
            self._status_label.value = "Остановлен"
            self._bat_dropdown.disabled = False

        if self._ready:
            self._btn_toggle.update()
            self._status_dot.update()
            self._status_label.update()
            self._bat_dropdown.update()
            self._tray_refresh()

    # ---- Actions ------------------------------------------------------------

    def _on_toggle(self, _):
        eng = self._engine
        if eng.process.is_running:
            self.set_status("Остановка обхода…", "loading")
            self._run_bg(
                eng.process.stop,
                on_success=lambda _: (
                    self._apply_state(ProcessState.STOPPED),
                    self.set_status("Обход остановлен", "success"),
                ),
            )
            return

        bat = self._bat()
        self.set_status("Запуск winws.exe…", "loading")

        def task():
            eng.process.start(bat)
            return eng.process.get_state()

        def ok(st):
            if st != ProcessState.RUNNING:
                eng.process.stop()
                self._apply_state(ProcessState.CRASHED)
                self.set_status("winws.exe не удалось запустить", "error")
                return
            self._apply_state(ProcessState.RUNNING)
            self.set_status("Обход активен", "success")

        def err(e):
            eng.process.stop()
            self._apply_state(ProcessState.CRASHED)
            self.set_status(str(e), "error")

        self._run_bg(task, on_success=ok, on_error=err)

    def _on_restart(self, _):
        bat = self._bat()
        self.set_status("Перезапуск обхода…", "loading")

        def task():
            self._engine.process.restart(bat)
            return self._engine.process.get_state()

        def ok(st):
            if st != ProcessState.RUNNING:
                self._apply_state(ProcessState.CRASHED)
                self.set_status("Перезапуск не удался", "error")
                return
            self._apply_state(ProcessState.RUNNING)
            self.set_status("Обход перезапущен", "success")

        def err(e):
            self._apply_state(ProcessState.CRASHED)
            self.set_status(str(e), "error")

        self._run_bg(task, on_success=ok, on_error=err)

    def _poll(self):
        if self._quitting or not self._ready or self._engine is None:
            return
        prev = self._last_state

        def ok(st):
            if st != prev:
                self._apply_state(st)
                if st == ProcessState.CRASHED and prev == ProcessState.RUNNING:
                    c = self._engine.process.last_exit_code
                    self.set_status(
                        f"winws.exe завершился (код {c})" if c is not None else "winws.exe завершился",
                        "error",
                    )
            self._loop.call_later(STATUS_POLL_MS / 1000.0, self._poll)

        def err(_):
            self._loop.call_later(STATUS_POLL_MS / 1000.0, self._poll)

        self._run_bg(self._engine.process.get_state, on_success=ok, on_error=err)

    # ---- Lists --------------------------------------------------------------

    def _load_lists(self):
        self.set_status("Загрузка списка…", "loading")
        self._run_bg(
            self._engine.read_domain_list,
            on_success=lambda t: (
                setattr(self._lists_field, "value", t),
                self._lists_field.update(),
                self.set_status("Список загружен", "info"),
            ),
        )

    def _save_lists(self, _=None):
        text = self._lists_field.value or ""
        self.set_status("Сохранение…", "loading")
        self._run_bg(
            lambda: self._engine.write_domain_list(text),
            on_success=lambda _: self.set_status("Список сохранён", "success"),
            on_error=lambda e: self.set_status(str(e), "error"),
        )

    # ---- Service / filters ----------------------------------------------------

    def _refresh_svc(self):
        self._run_bg(
            self._engine.service.get_status_text,
            on_success=lambda t: (
                setattr(self._svc_label, "value", t),
                self._svc_label.update(),
            ),
        )

    def _sync_filters(self):
        def on_mode(mode):
            self._game_dropdown.value = mode
            self._game_dropdown.update()

        def on_ipset(enabled):
            self._ipset_switch.value = enabled
            self._ipset_switch.update()

        self._run_bg(self._engine.filters.get_game_filter_mode, on_success=on_mode)
        self._run_bg(self._engine.filters.is_ipset_filter_enabled, on_success=on_ipset)

    def _on_game(self, e):
        mode = e.control.value or "disabled"
        self.set_status("Применение Game Filter…", "loading")

        def ok(m):
            self.set_status(f"{m}. Перезапустите обход.", "success")

        def err(e):
            self._sync_filters()
            self.set_status(str(e), "error")

        self._run_bg(lambda: self._engine.filters.set_game_filter_mode(mode), on_success=ok, on_error=err)

    def _on_ipset(self, e):
        en = bool(e.control.value)
        self.set_status("Применение IPSet…", "loading")

        def ok(m):
            self.set_status(m, "success")

        def err(e):
            self._sync_filters()
            self.set_status(str(e), "error")

        self._run_bg(lambda: self._engine.filters.set_ipset_filter_enabled(en), on_success=ok, on_error=err)

    def _install_svc(self, _=None):
        bat = self._bat()
        self.set_status("Установка службы…", "loading")

        def ok(m):
            self._refresh_svc()
            self.set_status(m, "success")

        self._run_bg(
            lambda: self._engine.service.install(bat),
            on_success=ok,
            on_error=lambda e: self.set_status(str(e), "error"),
        )

    def _remove_svc(self, _=None):
        self.set_status("Удаление службы…", "loading")

        def ok(m):
            self._refresh_svc()
            self.set_status(m, "success")

        self._run_bg(
            self._engine.service.remove,
            on_success=ok,
            on_error=lambda e: self.set_status(str(e), "error"),
        )

    def _update_ipsets(self, _=None):
        self.set_status("Обновление IP-сетов…", "loading")
        self._run_bg(
            self._engine.tools.update_ipsets,
            on_success=lambda m: self.set_status(m, "success"),
            on_error=lambda e: self.set_status(str(e), "error"),
        )

    def _update_hosts(self, _=None):
        self.set_status("Проверка hosts…", "loading")

        def ok(m):
            self.set_status(m, "warn" if "требует" in m.lower() else "success")

        self._run_bg(
            self._engine.tools.update_hosts,
            on_success=ok,
            on_error=lambda e: self.set_status(str(e), "error"),
        )

    def _clear_discord(self, _=None):
        self.set_status("Очистка кэша Discord…", "loading")
        self._run_bg(
            clear_discord_cache,
            on_success=lambda m: self.set_status(m, "success"),
            on_error=lambda e: self.set_status(str(e), "error"),
        )

    def _run_diag(self, _=None):
        self._log_clear()
        self.set_status("Диагностика системы…", "loading")

        def on_line(line: str):
            self._loop.call_soon_threadsafe(self._log_write, line)

        def task():
            return self._engine.tools.run_diagnostics(on_line=on_line)

        self._run_bg(
            task,
            on_success=lambda _: self.set_status("Диагностика завершена", "success"),
            on_error=lambda e: self.set_status(str(e), "error"),
        )

    def _log_write(self, line: str):
        self._diag_log.value += line + "\n"
        self._diag_log.update()

    def _log_clear(self):
        self._diag_log.value = ""
        self._diag_log.update()

    def _change_folder(self, _=None):
        if self._engine and self._engine.process.is_running:
            self.set_status("Сначала остановите обход", "warn")
            return
        self._pick_folder("change")

    # ---- Tray ---------------------------------------------------------------

    def _tray_image(self):
        img = Image.new("RGBA", (64, 64), (15, 16, 18, 255))
        d = ImageDraw.Draw(img)
        color = (129, 140, 248)
        d.line([(15, 10), (49, 10)], fill=color, width=6)
        d.line([(49, 10), (15, 54)], fill=color, width=6)
        d.line([(15, 54), (49, 54)], fill=color, width=6)
        return img

    def _tray_menu(self):
        import pystray
        return pystray.Menu(
            pystray.MenuItem("Развернуть", self._tray_open, default=True),
            pystray.MenuItem(
                "Остановить обход",
                self._tray_stop,
                visible=lambda _: self._engine and self._engine.process.is_running,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self._tray_exit),
        )

    def _setup_tray(self):
        # Stop any existing tray icon before creating a new one (prevents double icon)
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
            self._tray_thread = None
        try:
            import pystray
            self._tray_icon = pystray.Icon(
                "zapret",
                self._tray_image(),
                "Zapret Interface",
                menu=self._tray_menu(),
            )
        except ImportError:
            self._tray_icon = None

    def _tray_refresh(self):
        if self._tray_icon:
            self._tray_icon.menu = self._tray_menu()

    def _tray_open(self, *_, **__):
        self._loop.call_soon_threadsafe(self._show_window)

    def _show_window(self):
        self.page.window.visible = True
        self.page.update()

    def _tray_stop(self, *_, **__):
        def a():
            self._engine.process.stop()
            self._apply_state(ProcessState.STOPPED)
            self.set_status("Обход остановлен", "success")
        self._loop.call_soon_threadsafe(a)

    def _tray_exit(self, *_, **__):
        self._loop.call_soon_threadsafe(self._quit)

    def _hide_to_tray(self):
        self.page.window.visible = False
        self.page.update()
        if self._tray_icon is None:
            return
        # Only start the tray thread if it's not already alive
        if self._tray_thread is not None and self._tray_thread.is_alive():
            return
        self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        self._tray_thread.start()

    def _quit(self):
        self._quitting = True
        if self._engine:
            self._engine.process.stop()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.page.window.prevent_close = False
        self.page.window.close()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(page: ft.Page):
    _dbg_log("gui.py:main", "main() started", {}, "A")
    ZapretGUI(page)
    _dbg_log("gui.py:main", "ZapretGUI initialized", {}, "A")


if __name__ == "__main__":
    ft.app(target=main, view=ft.AppView.FLET_APP)
