"""YouTube Saver — восстановление подписок и истории из Google Takeout.

Запуск:
    python main.py [папка_с_архивами_takeout] [--port 8765]

По умолчанию архивы ищутся в папке, из которой запущена программа.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube Saver")
    ap.add_argument("folder", nargs="?", default=".",
                    help="папка с архивами takeout*.zip (по умолчанию — текущая)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true",
                    help="не открывать веб-интерфейс автоматически")
    args = ap.parse_args()

    app_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(app_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(args.folder, exist_ok=True) if args.folder != "." else None

    from ytsaver.takeout import (TakeoutBundle, parse_search_history,
                                 parse_subscriptions, parse_watch_history)
    from ytsaver.state import State
    from ytsaver.restore import RestoreEngine
    from ytsaver import server

    try:
        bundle = TakeoutBundle(args.folder)
    except FileNotFoundError as e:
        print("ОШИБКА:", e)
        print("Положите архивы takeout-*.zip рядом с main.py или укажите папку:")
        print("    python main.py C:\\path\\to\\zips")
        return 1

    print("Найдены архивы:", ", ".join(os.path.basename(p) for p in bundle.paths))

    # разбор данных (кэш разбора — отдельный на каждую папку с архивами)
    state = State(os.path.join(data_dir, "state.json"))
    import hashlib
    folder_key = hashlib.md5(os.path.abspath(args.folder).encode()).hexdigest()[:8]
    cache_path = os.path.join(data_dir, f"parsed_{folder_key}.json")
    parsed = _load_or_parse(cache_path, bundle, parse_subscriptions,
                            parse_watch_history, parse_search_history)

    _seed_state(state, parsed)

    engine = RestoreEngine(state, data_dir)
    httpd = server.start_server(engine, state, bundle, data_dir, args.port)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Панель управления: {url}  (Ctrl+C — выход)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("\nВыход.")
    httpd.shutdown()
    return 0


def _load_or_parse(cache_path, bundle, *parsers):
    import json
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    subs = [s.__dict__ for s in parsers[0](bundle)]
    watches = [e.__dict__ for e in parsers[1](bundle)]
    searches = [e.__dict__ for e in parsers[2](bundle)]
    parsed = {"subs": subs, "watch": watches, "search": searches}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False)
    print(f"Разобрано: подписок {len(subs)}, просмотров {len(watches)} "
          f"({len(set(e['video_id'] for e in watches))} уникальных), поисков {len(searches)}")
    return parsed


def _seed_state(state, parsed):
    def upd(d):
        for s in parsed["subs"]:
            if s["channel_id"] not in d["subs"]:
                d["subs"][s["channel_id"]] = {
                    "channel_id": s["channel_id"], "title": s["title"],
                    "url": s["url"], "status": "pending", "error": "",
                }
        for e in parsed["watch"]:
            vid = e["video_id"]
            if vid not in d["history"]:
                d["history"][vid] = {
                    "video_id": vid, "title": e["title"], "url": e["url"],
                    "date_raw": e["date_raw"], "iso_date": e.get("iso_date"),
                    "order": e["order"], "status": "pending", "error": "",
                }
        if not d["search"]:
            d["search"] = parsed["search"]
        if d["videos"]["total"] == 0 and parsed.get("videos_total"):
            pass
    state.update(upd)


if __name__ == "__main__":
    sys.exit(main())
