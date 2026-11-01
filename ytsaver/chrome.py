"""Поиск и запуск Chrome/Edge/Brave с remote debugging + минимальный CDP-клиент.

Chrome >= 136 запрещает remote debugging для основного профиля, поэтому
инструмент всегда использует собственный профиль в папке приложения:
пользователь один раз логинится в новый Google-аккаунт в открывшемся окне.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

_DEBUG_PORT = 9333
_PORT_FILE_NAME = "debug-port.txt"


def _set_port(port: int, profile_dir: str | None = None):
    global _DEBUG_PORT
    _DEBUG_PORT = port
    if profile_dir:
        try:
            with open(os.path.join(os.path.dirname(profile_dir), _PORT_FILE_NAME), "w") as f:
                f.write(str(port))
        except OSError:
            pass


def _load_port(data_dir: str | None = None):
    """Восстановить порт работающего браузера (если сервер перезапускался)."""
    if data_dir:
        p = os.path.join(data_dir, _PORT_FILE_NAME)
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    port = int(f.read().strip())
                    if debug_port_alive():
                        globals()["_DEBUG_PORT"] = port
                        return
            except (OSError, ValueError):
                pass


# ---------------------------------------------------------------------------
# Поиск браузера
# ---------------------------------------------------------------------------

def find_browser() -> str | None:
    """Найти исполняемый файл Chromium-браузера."""
    candidates = []

    def add(p):
        if p and os.path.isfile(p):
            candidates.append(p)

    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    for name, rels in (
        ("chrome.exe", [
            r"Google\Chrome\Application\chrome.exe",
            r"Google\Chrome Beta\Application\chrome.exe",
        ]),
        ("msedge.exe", [
            r"Microsoft\Edge\Application\msedge.exe",
        ]),
        ("brave.exe", [
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ]),
        ("vivaldi.exe", [
            r"Vivaldi\Application\vivaldi.exe",
        ]),
        ("opera.exe", [
            r"Opera\opera.exe",
        ]),
    ):
        for rel in rels:
            for base in (pf, pf86, local):
                add(os.path.join(base, rel))

    # реестр Windows
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        add(val)
                except OSError:
                    pass
        except ImportError:
            pass

    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Запуск / остановка
# ---------------------------------------------------------------------------

def kill_profile_instances(profile_dir: str):
    """Закрыть браузерные процессы, использующие наш отладочный профиль
    (не трогает основной браузер пользователя)."""
    profile_dir = os.path.abspath(profile_dir)
    if sys.platform == "win32":
        esc = profile_dir.replace("'", "''")
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" "
              "| Where-Object { $_.CommandLine -match 'remote-debugging-port' "
              f"-and $_.CommandLine -like '*{esc}*' }} "
              "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30)
    else:
        subprocess.run(["pkill", "-f", profile_dir], capture_output=True)


def start_browser(profile_dir: str) -> str:
    """Запустить браузер с отладочным портом. Вернуть строку статуса."""
    if debug_port_alive():
        _set_port(_DEBUG_PORT)
        return "Браузер уже запущен (порт %d)" % _DEBUG_PORT
    # старый экземпляр с тем же профилем перехватил бы запуск — закрываем
    kill_profile_instances(profile_dir)
    time.sleep(1.5)
    port = _DEBUG_PORT
    exe = os.environ.get("YTSAVER_BROWSER") or find_browser()
    if not exe or not os.path.isfile(exe):
        raise RuntimeError(
            "Не найден Chrome/Edge/Brave. Установите Google Chrome или укажите "
            "путь в переменной окружения YTSAVER_BROWSER.")
    os.makedirs(profile_dir, exist_ok=True)
    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--autoplay-policy=no-user-gesture-required",
        "--start-maximized",
        "https://www.youtube.com/",
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _set_port(port, profile_dir)
    # Chrome при первом старте профиля может перезапуститься — ждём подольше
    deadline = time.time() + 45
    time.sleep(3)
    while time.time() < deadline:
        try:
            _http_json(f"http://127.0.0.1:{port}/json/version")
            return f"Браузер запущен: {os.path.basename(exe)} (порт {port})"
        except Exception:
            time.sleep(1.0)
    raise RuntimeError("Браузер запустился, но отладочный порт не отвечает")


def _http_json(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def debug_port_alive() -> bool:
    try:
        _http_json(f"http://127.0.0.1:{_DEBUG_PORT}/json/version")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CDP-клиент
# ---------------------------------------------------------------------------

class CDP:
    """Минимальный клиент Chrome DevTools Protocol (одна вкладка)."""

    def __init__(self, ws_url: str):
        import websocket
        self._ws = websocket.create_connection(ws_url, timeout=30,
                                               suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self._id += 1
        msg_id = self._id
        self._ws.settimeout(timeout)
        self._ws.send(json.dumps({"id": msg_id, "method": method,
                                  "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self._ws.recv()
            if not isinstance(raw, (str, bytes)):
                continue
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
        raise TimeoutError(f"{method}: таймаут")

    def evaluate(self, expression: str, timeout: float = 30, await_promise: bool = False):
        """Выполнить JS на странице, вернуть распакованное значение."""
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }, timeout=timeout)
        r = res.get("result", {})
        if res.get("exceptionDetails"):
            desc = res["exceptionDetails"].get("exception", {}).get("description", "JS error")
            raise RuntimeError(desc)
        return r.get("value")

    def evaluate_async(self, expression: str, timeout: float = 60):
        """Выполнить выражение, возвращающее Promise (или тело async-функции
        с ранними return — определяется автоматически)."""
        stripped = expression.lstrip()
        if stripped.startswith("(async"):
            wrapped = expression
        else:
            wrapped = "(async () => { " + expression + " })()"
        return self.evaluate(wrapped, timeout=timeout, await_promise=True)

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_tab(url: str) -> dict:
    """Открыть новую вкладку, вернуть её target."""
    return _http_json(f"http://127.0.0.1:{_DEBUG_PORT}/json/new?{url}", method="PUT")


def list_tabs() -> list[dict]:
    return _http_json(f"http://127.0.0.1:{_DEBUG_PORT}/json/list")


def get_page_target(url_prefix: str = "https://www.youtube.com"):
    """Найти (или создать) вкладку youtube.com. Вернуть (target, cdp)."""
    tabs = list_tabs()
    page = None
    for t in tabs:
        if t.get("type") == "page" and t.get("url", "").startswith(url_prefix):
            page = t
            break
    if page is None:
        page = open_tab(url_prefix + "/")
        time.sleep(2)
    cdp = CDP(page["webSocketDebuggerUrl"])
    return page, cdp


def wait_page_ready(cdp: CDP, timeout: float = 30):
    """Дождаться, пока вкладка загрузится (document.readyState == complete)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = cdp.evaluate("document.readyState", timeout=10)
            if state == "complete":
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False
