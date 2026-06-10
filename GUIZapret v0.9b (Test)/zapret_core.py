"""
Zapret Engine — чистая бизнес-логика без зависимостей от GUI.

Отвечает за: пути, процессы winws.exe, парсинг .bat, службу Windows,
фильтры, системные утилиты и очистку кэша Discord.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Callable

CREATE_NO_WINDOW = 0x08000000

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

IPSET_URL = (
    "https://raw.githubusercontent.com/Flowseal/zapret-discord-youtube/"
    "refs/heads/main/.service/ipset-service.txt"
)
HOSTS_URL = (
    "https://raw.githubusercontent.com/Flowseal/zapret-discord-youtube/"
    "refs/heads/main/.service/hosts"
)

GAME_FILTER_MODES: dict[str, str] = {
    "disabled": "Выключен",
    "all": "TCP и UDP",
    "tcp": "Только TCP",
    "udp": "Только UDP",
}

RESTART_DELAY_SEC = 1.5


class ProcessState(Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    CRASHED = "crashed"


# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------


def get_app_dir() -> Path:
    """Каталог приложения (.py / .pyw / скомпилированный .exe)."""
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(os.path.abspath(sys.argv[0])))
    return Path(os.path.dirname(os.path.abspath(__file__)))


def is_valid_zapret_root(path: Path | str) -> bool:
    return (Path(path) / "bin" / "winws.exe").is_file()


class ZapretConfig:
    """Сохранение пути к корню zapret."""

    CONFIG_NAME = "zapret_config.json"

    def __init__(self) -> None:
        self.app_dir = get_app_dir()
        self.config_path = self.app_dir / self.CONFIG_NAME

    def load(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: dict) -> None:
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_saved_root(self) -> Path | None:
        raw = self.load().get("zapret_root")
        if not raw:
            return None
        path = Path(raw)
        return path.resolve() if is_valid_zapret_root(path) else None

    def set_zapret_root(self, path: Path) -> None:
        if not is_valid_zapret_root(path):
            raise ValueError(f"Некорректная папка zapret: {path}")
        data = self.load()
        data["zapret_root"] = str(path.resolve())
        self.save(data)

    def _candidate_roots(self) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        for base in (self.app_dir, *self.app_dir.parents[:5]):
            try:
                resolved = base.resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
        return out

    def resolve_zapret_root(self) -> Path | None:
        saved = self.get_saved_root()
        if saved:
            return saved
        for candidate in self._candidate_roots():
            if is_valid_zapret_root(candidate):
                return candidate
        return None


# ---------------------------------------------------------------------------
# Парсинг .bat
# ---------------------------------------------------------------------------


def scan_bat_configs(zapret_root: Path) -> list[Path]:
    configs: list[Path] = []
    for bat in sorted(zapret_root.glob("*.bat"), key=lambda p: p.name.lower()):
        if bat.name.lower() == "service.bat":
            continue
        try:
            text = bat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"winws\.exe", text, re.IGNORECASE):
            configs.append(bat)
    return configs


def _read_game_filter_values(zapret_root: Path) -> tuple[str, str]:
    flag = zapret_root / "utils" / "game_filter.enabled"
    if not flag.exists():
        return "12", "12"
    mode = flag.read_text(encoding="utf-8", errors="replace").strip().lower()
    if mode == "all":
        return "1024-65535", "1024-65535"
    if mode == "tcp":
        return "1024-65535", "12"
    return "12", "1024-65535"


def _join_bat_continuations(content: str) -> str:
    lines: list[str] = []
    buf = ""
    for raw in content.splitlines():
        line = raw.rstrip()
        if line.endswith("^"):
            buf += line[:-1].rstrip() + " "
            continue
        buf += line
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    return "\n".join(lines)


def _expand_bat_variables(content: str, zapret_root: Path) -> str:
    base = str(zapret_root.resolve())
    if not base.endswith(os.sep):
        base += os.sep
    bin_p = str(zapret_root / "bin")
    if not bin_p.endswith(os.sep):
        bin_p += os.sep
    lists_p = str(zapret_root / "lists")
    if not lists_p.endswith(os.sep):
        lists_p += os.sep
    gt, gu = _read_game_filter_values(zapret_root)
    text = content.replace("%~dp0", base)
    text = text.replace("%BIN%", bin_p)
    text = text.replace("%LISTS%", lists_p)
    text = text.replace("%GameFilterTCP%", gt)
    text = text.replace("%GameFilterUDP%", gu)
    text = text.replace("%GameFilter%", gt)
    return text


def _split_cmdline_args(s: str) -> list[str]:
    args: list[str] = []
    cur: list[str] = []
    in_q = False
    qc = ""
    for ch in s:
        if ch in ('"', "'"):
            if not in_q:
                in_q, qc = True, ch
            elif ch == qc:
                in_q, qc = False, ""
            else:
                cur.append(ch)
        elif ch.isspace() and not in_q:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur))
    return args


def parse_winws_args_from_bat(bat_path: Path, zapret_root: Path) -> list[str]:
    if not bat_path.exists():
        raise FileNotFoundError(f"Не найден: {bat_path}")
    raw = bat_path.read_text(encoding="utf-8", errors="replace")
    expanded = _expand_bat_variables(_join_bat_continuations(raw), zapret_root)
    m = re.search(r'winws\.exe"\s+(.+)', expanded, re.IGNORECASE | re.DOTALL)
    if not m:
        m = re.search(r"winws\.exe\s+(.+)", expanded, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError(f"В {bat_path.name} не найден winws.exe")
    return _split_cmdline_args(m.group(1).strip())


def _run_hidden(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _download_url(url: str, dest: Path, timeout: int = 30) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if curl.is_file():
        r = _run_hidden([str(curl), "-L", "-s", "-o", str(dest), url], timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "Ошибка curl")
        return
    req = urllib.request.Request(url, headers={"User-Agent": "ZapretEngine/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())


# ---------------------------------------------------------------------------
# Процесс winws.exe
# ---------------------------------------------------------------------------


class ZapretProcessManager:
    def __init__(self, zapret_root: Path) -> None:
        self.zapret_root = zapret_root.resolve()
        self._process: subprocess.Popen | None = None
        self._pid: int | None = None
        self._user_stopped = False
        self._last_exit_code: int | None = None

    @property
    def is_running(self) -> bool:
        return self.get_state() == ProcessState.RUNNING

    @property
    def last_exit_code(self) -> int | None:
        return self._last_exit_code

    def _pid_alive(self, pid: int) -> bool:
        if psutil is not None:
            return psutil.pid_exists(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def get_state(self) -> ProcessState:
        if self._process is not None:
            code = self._process.poll()
            if code is None:
                if self._pid and not self._pid_alive(self._pid):
                    self._last_exit_code = -1
                    self._process = None
                    self._pid = None
                    return ProcessState.CRASHED if not self._user_stopped else ProcessState.STOPPED
                return ProcessState.RUNNING
            self._last_exit_code = code
            self._process = None
            self._pid = None
            return ProcessState.STOPPED if self._user_stopped else ProcessState.CRASHED
        if self._pid and self._pid_alive(self._pid):
            return ProcessState.RUNNING
        if self._pid:
            self._pid = None
            return ProcessState.STOPPED if self._user_stopped else ProcessState.CRASHED
        return ProcessState.STOPPED

    def start(self, bat_path: Path) -> None:
        if self.is_running:
            return
        exe = self.zapret_root / "bin" / "winws.exe"
        if not exe.is_file():
            raise FileNotFoundError(f"Не найден: {exe}")
        args = parse_winws_args_from_bat(bat_path, self.zapret_root)
        self._user_stopped = False
        self._last_exit_code = None
        self._process = subprocess.Popen(
            [str(exe), *args],
            cwd=str(self.zapret_root / "bin"),
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._pid = self._process.pid
        time.sleep(0.8)
        code = self._process.poll()
        if code is not None:
            self._last_exit_code = code
            self._process = None
            self._pid = None
            raise RuntimeError(f"winws.exe завершился сразу (код {code})")

    def stop(self) -> None:
        self._user_stopped = True
        if self._process and self._process.poll() is None:
            self._process.kill()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self._pid and self._pid_alive(self._pid):
            try:
                if psutil is not None:
                    psutil.Process(self._pid).kill()
                else:
                    _run_hidden(["taskkill", "/F", "/PID", str(self._pid)])
            except Exception:
                pass
        self._process = None
        self._pid = None
        _run_hidden(["taskkill", "/F", "/IM", "winws.exe"])

    def restart(self, bat_path: Path, delay: float = RESTART_DELAY_SEC) -> None:
        """Остановка, пауза и повторный запуск с тем же .bat."""
        self.stop()
        time.sleep(delay)
        self.start(bat_path)


# ---------------------------------------------------------------------------
# Служба Windows
# ---------------------------------------------------------------------------


class ServiceManager:
    SERVICE_NAME = "zapret"

    def __init__(self, zapret_root: Path) -> None:
        self.zapret_root = zapret_root.resolve()

    def is_installed(self) -> bool:
        return _run_hidden(["sc", "query", self.SERVICE_NAME]).returncode == 0

    def get_status_text(self) -> str:
        if not self.is_installed():
            return "Служба не установлена"
        out = _run_hidden(["sc", "query", self.SERVICE_NAME]).stdout.upper()
        return "Служба установлена и запущена" if "RUNNING" in out else "Служба установлена, но остановлена"

    def _tcp_enable(self) -> None:
        chk = _run_hidden(["netsh", "interface", "tcp", "show", "global"])
        if "enabled" not in chk.stdout.lower():
            _run_hidden(["netsh", "interface", "tcp", "set", "global", "timestamps=enabled"])

    def install(self, bat_path: Path) -> str:
        exe = self.zapret_root / "bin" / "winws.exe"
        args = parse_winws_args_from_bat(bat_path, self.zapret_root)
        self._tcp_enable()
        _run_hidden(["net", "stop", self.SERVICE_NAME])
        _run_hidden(["sc", "delete", self.SERVICE_NAME])
        bin_val = f'\\"{exe}\\" {subprocess.list2cmdline(args)}'
        cr = _run_hidden(["sc", "create", self.SERVICE_NAME, f"binPath={bin_val}", "DisplayName=zapret", "start=auto"])
        if cr.returncode != 0:
            raise RuntimeError(cr.stderr.strip() or "Не удалось создать службу")
        _run_hidden(["sc", "description", self.SERVICE_NAME, "Zapret DPI bypass software"])
        st = _run_hidden(["sc", "start", self.SERVICE_NAME])
        if st.returncode != 0:
            raise RuntimeError(st.stderr.strip() or "Служба не запустилась")
        _run_hidden(
            ["reg", "add", r"HKLM\System\CurrentControlSet\Services\zapret",
             "/v", "zapret-discord-youtube", "/t", "REG_SZ", "/d", bat_path.stem, "/f"]
        )
        return f"Служба установлена ({bat_path.name})"

    def remove(self) -> str:
        parts: list[str] = []
        if self.is_installed():
            _run_hidden(["net", "stop", self.SERVICE_NAME])
            if _run_hidden(["sc", "delete", self.SERVICE_NAME]).returncode == 0:
                parts.append("Служба zapret удалена из системы")
            else:
                parts.append("Ошибка удаления службы zapret")
        else:
            parts.append("Служба zapret не была установлена")
        _run_hidden(["taskkill", "/F", "/IM", "winws.exe"])
        for svc in ("WinDivert", "WinDivert14"):
            if _run_hidden(["sc", "query", svc]).returncode == 0:
                _run_hidden(["net", "stop", svc])
                _run_hidden(["sc", "delete", svc])
                parts.append(f"Удалена служба {svc}")
        return ". ".join(parts)


# ---------------------------------------------------------------------------
# Фильтры
# ---------------------------------------------------------------------------


class FilterSettings:
    DUMMY_IP = "203.0.113.113/32"

    def __init__(self, zapret_root: Path) -> None:
        self.zapret_root = zapret_root.resolve()
        self.game_flag = self.zapret_root / "utils" / "game_filter.enabled"
        self.ipset_file = self.zapret_root / "lists" / "ipset-all.txt"
        self.ipset_backup = self.zapret_root / "lists" / "ipset-all.txt.backup"

    def get_game_filter_mode(self) -> str:
        if not self.game_flag.exists():
            return "disabled"
        mode = self.game_flag.read_text(encoding="utf-8", errors="replace").strip().lower()
        return mode if mode in GAME_FILTER_MODES else "all"

    def set_game_filter_mode(self, mode: str) -> str:
        if mode not in GAME_FILTER_MODES:
            raise ValueError(f"Неизвестный режим: {mode}")
        self.game_flag.parent.mkdir(parents=True, exist_ok=True)
        if mode == "disabled":
            if self.game_flag.exists():
                self.game_flag.unlink()
        else:
            self.game_flag.write_text(f"{mode}\n", encoding="utf-8")
        return f"Game Filter: {GAME_FILTER_MODES[mode]}"

    def is_ipset_filter_enabled(self) -> bool:
        return self._ipset_status() == "loaded"

    def _ipset_status(self) -> str:
        if not self.ipset_file.exists():
            return "any"
        lines = [ln.strip() for ln in self.ipset_file.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines:
            return "any"
        if len(lines) == 1 and lines[0] == self.DUMMY_IP:
            return "none"
        return "loaded"

    def set_ipset_filter_enabled(self, enabled: bool) -> str:
        self.ipset_file.parent.mkdir(parents=True, exist_ok=True)
        status = self._ipset_status()
        if enabled:
            if status == "loaded":
                return "IPSet Filter уже включён"
            if not self.ipset_backup.exists():
                raise FileNotFoundError("Нет резервной копии ipset-all.txt. Сначала обновите IP-сеты.")
            if self.ipset_file.exists():
                self.ipset_file.unlink()
            self.ipset_backup.rename(self.ipset_file)
            return "IPSet Filter включён"
        if status == "none":
            return "IPSet Filter уже выключен"
        if status == "loaded":
            if self.ipset_backup.exists():
                self.ipset_backup.unlink()
            self.ipset_file.rename(self.ipset_backup)
        self.ipset_file.write_text(f"{self.DUMMY_IP}\n", encoding="utf-8")
        return "IPSet Filter выключен"


# ---------------------------------------------------------------------------
# Системные утилиты
# ---------------------------------------------------------------------------


class SystemTools:
    def __init__(self, zapret_root: Path) -> None:
        self.zapret_root = zapret_root.resolve()

    def update_ipsets(self) -> str:
        dest = self.zapret_root / "lists" / "ipset-all.txt"
        _download_url(IPSET_URL, dest)
        return "IP-сеты успешно обновлены"

    def update_hosts(self) -> str:
        temp = Path(os.environ.get("TEMP", ".")) / "zapret_hosts.txt"
        hosts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
        _download_url(HOSTS_URL, temp)
        lines = [ln.strip() for ln in temp.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines:
            raise RuntimeError("Скачанный файл hosts пуст")
        hosts_text = hosts.read_text(encoding="utf-8", errors="replace") if hosts.exists() else ""
        if lines[0] not in hosts_text or lines[-1] not in hosts_text:
            return f"Hosts требует обновления. Файл скачан: {temp}"
        temp.unlink(missing_ok=True)
        return "Файл hosts актуален"

    def run_diagnostics(self, on_line: Callable[[str], None] | None = None) -> str:
        log: list[str] = []

        def emit(level: str, msg: str) -> None:
            icons = {"ok": "[OK]", "warn": "[!]", "err": "[X]", "info": "[i]"}
            line = f"{icons.get(level, '-')} {msg}"
            log.append(line)
            if on_line:
                on_line(line)

        emit("info", "=== Диагностика Zapret ===")
        bfe = _run_hidden(["sc", "query", "BFE"])
        emit("ok" if "RUNNING" in bfe.stdout.upper() else "err",
             "Base Filtering Engine работает" if "RUNNING" in bfe.stdout.upper() else "BFE не запущен (критично)")

        proxy = _run_hidden(["reg", "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyEnable"])
        if "0x1" in proxy.stdout:
            srv = _run_hidden(["reg", "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyServer"])
            emit("warn", f"Включён системный прокси: {srv.stdout.strip() or '?'}")
        else:
            emit("ok", "Системный прокси выключен")

        tcp = _run_hidden(["netsh", "interface", "tcp", "show", "global"])
        if "enabled" in tcp.stdout.lower():
            emit("ok", "TCP timestamps включены")
        else:
            en = _run_hidden(["netsh", "interface", "tcp", "set", "global", "timestamps=enabled"])
            emit("ok" if en.returncode == 0 else "err", "TCP timestamps включены" if en.returncode == 0 else "Не удалось включить TCP timestamps")

        task = _run_hidden(["tasklist", "/FI", "IMAGENAME eq AdguardSvc.exe"])
        emit("err" if "AdguardSvc.exe" in task.stdout else "ok",
             "Adguard найден — конфликт с Discord" if "AdguardSvc.exe" in task.stdout else "Adguard не найден")

        svc_all = _run_hidden(["sc", "query", "type=", "service", "state=", "all"])
        for label, pat in (("Killer", "killer"), ("Intel Connectivity", "intel"), ("SmartByte", "smartbyte")):
            emit("warn" if pat in svc_all.stdout.lower() else "ok",
                 f"{label} — возможный конфликт" if pat in svc_all.stdout.lower() else f"{label} — OK")

        sys_files = list((self.zapret_root / "bin").glob("*.sys"))
        emit("ok" if sys_files else "err",
             f"WinDivert: {sys_files[0].name}" if sys_files else "WinDivert64.sys не найден")

        winws = _run_hidden(["tasklist", "/FI", "IMAGENAME eq winws.exe"])
        emit("ok" if "winws.exe" in winws.stdout else "warn",
             "winws.exe запущен" if "winws.exe" in winws.stdout else "winws.exe не запущен")

        wd = _run_hidden(["sc", "query", "WinDivert"])
        if "RUNNING" in wd.stdout.upper() and "winws.exe" not in winws.stdout:
            emit("warn", "WinDivert активен без winws — удаление…")
            _run_hidden(["net", "stop", "WinDivert"])
            _run_hidden(["sc", "delete", "WinDivert"])
            emit("ok", "WinDivert удалён")

        z = _run_hidden(["sc", "query", "zapret"])
        if z.returncode == 0:
            emit("info", f"Служба zapret: {'RUNNING' if 'RUNNING' in z.stdout.upper() else 'STOPPED'}")
        else:
            emit("info", "Служба zapret не установлена")

        hosts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
        if hosts.exists():
            ht = hosts.read_text(encoding="utf-8", errors="replace").lower()
            if "youtube.com" in ht or "youtu.be" in ht:
                emit("warn", "hosts содержит записи YouTube")

        emit("info", "=== Диагностика завершена ===")
        return "\n".join(log)


# ---------------------------------------------------------------------------
# Очистка кэша Discord
# ---------------------------------------------------------------------------


def clear_discord_cache() -> str:
    """Завершает Discord и очищает папки кэша в %APPDATA%\\Discord."""
    for proc in ("discord.exe", "Discord.exe"):
        _run_hidden(["taskkill", "/F", "/IM", proc])

    appdata = Path(os.environ.get("APPDATA", ""))
    if not appdata.is_dir():
        raise RuntimeError("Переменная APPDATA не найдена")

    cache_dirs = [
        appdata / "Discord" / "Cache",
        appdata / "Discord" / "Code Cache",
        appdata / "Discord" / "GPUCache",
    ]
    cleared = 0
    for folder in cache_dirs:
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True, exist_ok=True)
            cleared += 1
    return f"Кэш Discord очищен ({cleared} папок)"


# ---------------------------------------------------------------------------
# Фасад движка
# ---------------------------------------------------------------------------


class ZapretEngine:
    """Единая точка входа к бизнес-логике для GUI."""

    def __init__(self, zapret_root: Path) -> None:
        self.zapret_root = zapret_root.resolve()
        self.process = ZapretProcessManager(self.zapret_root)
        self.service = ServiceManager(self.zapret_root)
        self.filters = FilterSettings(self.zapret_root)
        self.tools = SystemTools(self.zapret_root)
        self.list_general = self.zapret_root / "lists" / "list-general.txt"

    def read_domain_list(self) -> str:
        if self.list_general.exists():
            return self.list_general.read_text(encoding="utf-8", errors="replace")
        return ""

    def write_domain_list(self, content: str) -> None:
        self.list_general.parent.mkdir(parents=True, exist_ok=True)
        self.list_general.write_text(content, encoding="utf-8")


def run_in_background(
    func: Callable,
    on_success: Callable | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Запуск задачи в daemon-потоке (вызывается из GUI)."""

    def worker() -> None:
        try:
            result = func()
            if on_success:
                on_success(result)
        except Exception as exc:
            if on_error:
                on_error(exc)

    threading.Thread(target=worker, daemon=True).start()


def get_base_dir() -> Path:
    return get_app_dir()
