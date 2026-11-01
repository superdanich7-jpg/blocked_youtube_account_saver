"""Потокобезопасное состояние приложения с сохранением в JSON."""

from __future__ import annotations

import json
import os
import threading
import time


class State:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict = {}
        self.load()

    def load(self):
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        self._data = json.load(f)
                except Exception:
                    self._data = {}
            self._data.setdefault("subs", {})
            self._data.setdefault("history", {})
            self._data.setdefault("search", [])
            self._data.setdefault("videos", {"total": 0, "done": 0,
                                             "current": "", "running": False,
                                             "files": []})
            self._data.setdefault("browser", {"running": False, "logged_in": None,
                                              "message": ""})
            self._data.setdefault("worker", {"subs_running": False,
                                             "history_running": False,
                                             "history_stop": False,
                                             "last_message": ""})

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    # -- атомарные операции -------------------------------------------------

    def get(self):
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def update(self, fn):
        """fn(data) — мутация под блокировкой, затем сохранение."""
        with self._lock:
            fn(self._data)
            self._data["_ts"] = time.time()
            self.save()

    def message(self, text: str):
        self.update(lambda d: d["worker"].update(last_message=text))
