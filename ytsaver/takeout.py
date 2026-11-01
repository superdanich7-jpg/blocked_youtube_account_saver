"""Парсинг архивов Google Takeout (YouTube и YouTube Music).

Читает данные прямо из zip-архивов (в том числе многочастных), чинит
кодировку имён файлов (Takeout не всегда ставит UTF-8 флаг) и разбирает:
  - подписки/подписки.csv
  - история/история-просмотров.html
  - история/история_поиска.html
  - список видеофайлов
"""

from __future__ import annotations

import csv
import glob
import html
import io
import os
import re
import urllib.parse
from dataclasses import dataclass

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}

TAKEOUT_GLOB = "takeout*.zip"


# ---------------------------------------------------------------------------
# Доступ к zip-архивам
# ---------------------------------------------------------------------------

def _fix_zip_name(name: str) -> str:
    """Takeout часто не ставит флаг UTF-8 у имён с кириллицей — чиним."""
    try:
        fixed = name.encode("cp437").decode("utf-8")
        if fixed and "\N{REPLACEMENT CHARACTER}" not in fixed:
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


class TakeoutBundle:
    """Набор zip-частей выгрузки Takeout в одной папке."""

    def __init__(self, folder: str):
        self.folder = folder
        self.paths = sorted(glob.glob(os.path.join(folder, TAKEOUT_GLOB)))
        if not self.paths:
            raise FileNotFoundError(
                f"В папке {folder!r} не найдено архивов takeout*.zip"
            )
        self._handles = {}
        # индекс: нормализованное имя -> список (zip_path, raw_name)
        self._index = {}
        self._open_all()

    def _open_all(self) -> None:
        import zipfile

        for path in self.paths:
            try:
                zf = zipfile.ZipFile(path)
            except zipfile.BadZipFile:
                continue
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = _fix_zip_name(info.filename)
                self._index.setdefault(name, []).append((path, info.filename))

    def names(self):
        return sorted(self._index.keys())

    def open(self, name: str) -> io.BytesIO:
        """Открыть файл из архива. Пробует все zip-части (в многочастных
        выгрузках центральный каталог бывает не в той части, где данные)."""
        candidates = self._index.get(name)
        if not candidates:
            raise KeyError(name)
        import zipfile

        last_err = None
        for zip_path, raw in candidates:
            try:
                zf = self._handles.get(zip_path)
                if zf is None:
                    zf = zipfile.ZipFile(zip_path)
                    self._handles[zip_path] = zf
                with zf.open(raw) as fh:
                    return io.BytesIO(fh.read())
            except Exception as e:  # BadZipFile/CRC — файл лежит в другой части
                last_err = e
        raise IOError(f"Не удалось прочитать {name!r}: {last_err}")

    def read_text(self, name: str) -> str:
        return self.open(name).read().decode("utf-8", errors="replace")

    # -- видео ------------------------------------------------------------

    def video_members(self):
        """Имена видеофайлов внутри Takeout (папка «видео»/«videos»)."""
        out = []
        for name in self._index:
            base = name.rsplit("/", 1)[-1]
            ext = os.path.splitext(base)[1].lower()
            if ext in VIDEO_EXTS and ("/видео/" in name or "/videos/" in name):
                out.append(name)
        return sorted(out)

    def extract_videos(self, dest: str, progress=None, should_stop=None):
        """Распаковать видео в dest. Возвращает список (имя_файла, исходный_путь)."""
        os.makedirs(dest, exist_ok=True)
        done = []
        members = self.video_members()
        import zipfile

        for i, name in enumerate(members):
            if should_stop and should_stop():
                break
            base = name.rsplit("/", 1)[-1]
            safe = re.sub(r'[<>:"/|?*\x00-\x1f]', "_", base).strip() or "video.mp4"
            target = os.path.join(dest, safe)
            if progress:
                progress(i + 1, len(members), safe)
            if os.path.exists(target) and os.path.getsize(target) > 0:
                done.append((safe, name))
                continue
            for zip_path, raw in self._index[name]:
                try:
                    zf = self._handles.get(zip_path)
                    if zf is None:
                        zf = zipfile.ZipFile(zip_path)
                        self._handles[zip_path] = zf
                    with zf.open(raw) as src, open(target + ".part", "wb") as out:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                    os.replace(target + ".part", target)
                    done.append((safe, name))
                    break
                except Exception:
                    continue
            else:
                if os.path.exists(target + ".part"):
                    os.remove(target + ".part")
        return done


# ---------------------------------------------------------------------------
# Подписки
# ---------------------------------------------------------------------------

@dataclass
class Sub:
    channel_id: str
    url: str
    title: str


def find_member(bundle: TakeoutBundle, *keywords: str):
    """Найти файл в архиве по подстрокам в пути (независимо от языка/папки)."""
    for name in bundle.names():
        low = name.lower()
        if all(k.lower() in low for k in keywords):
            return name
    return None


def parse_subscriptions(bundle: TakeoutBundle):
    name = find_member(bundle, "подписки", ".csv")
    if name is None:
        name = find_member(bundle, "subscriptions", ".csv")
    if name is None:
        return []
    text = bundle.read_text(name)
    subs = []
    for row in csv.DictReader(io.StringIO(text)):
        cid = (row.get("Идентификатор канала") or row.get("Channel ID") or "").strip()
        url = (row.get("URL канала") or row.get("Channel URL") or "").strip()
        title = (row.get("Название канала") or row.get("Channel Title") or "").strip()
        if cid:
            subs.append(Sub(channel_id=cid, url=url, title=title))
    return subs


# ---------------------------------------------------------------------------
# История просмотров / поиска
# ---------------------------------------------------------------------------

@dataclass
class HistoryEntry:
    kind: str            # "watch" | "search"
    video_id: str | None
    title: str
    url: str
    date_raw: str
    order: int           # позиция в файле (от новых к старым)
    iso_date: str | None = None


_ENTRY_RE = re.compile(
    r'<div class="content-cell[^"]*mdl-typography--body-1"[^>]*>(.*?)</div>', re.S)
_A_RE = re.compile(r'<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", s)).strip()


_MONTHS_RU = {
    "янв": 1, "февр": 2, "мар": 3, "апр": 4, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сент": 9, "окт": 10, "нояб": 11, "дек": 12,
}
_MONTHS_EN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _try_iso_date(raw: str):
    """Best-effort разбор даты («14 нояб. 2025 г., 15:21:17 UTC» / «Nov 14, 2025…»)."""
    raw = raw.strip().rstrip("·").strip()
    m = re.search(
        r"(\d{1,2})\s*([а-яё]+)\.?\s*(\d{4})\s*г?\.?,?\s*"
        r"(?:(\d{1,2}):(\d{2})(?::(\d{2}))?)?", raw, re.I)
    if m:
        d, mon, y, hh, mm, ss = m.groups()
        key = mon[:4].lower()
        mon_num = None
        for k, v in _MONTHS_RU.items():
            if key.startswith(k[:3]):
                mon_num = v
                break
        if mon_num is None:
            return None
        return "%s-%02d-%02dT%s:%s:%s" % (
            y, mon_num, int(d), (hh or "0").zfill(2), mm, ss or "00")
    m = re.search(r"([A-Za-z]{3})\s+(\d{1,2}),\s*(\d{4})", raw)
    if m and m.group(1).lower() in _MONTHS_EN:
        return "%s-%02d-%02d" % (m.group(3), _MONTHS_EN[m.group(1).lower()], int(m.group(2)))
    return None


def _video_id_from_url(url: str):
    url = html.unescape(url)
    for pattern in (r"[?&]v=([A-Za-z0-9_-]{6,})",
                    r"youtu\.be/([A-Za-z0-9_-]{6,})",
                    r"shorts/([A-Za-z0-9_-]{6,})"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def parse_watch_history(bundle: TakeoutBundle):
    name = find_member(bundle, "просмотров", ".html")
    if name is None:
        name = find_member(bundle, "watch", "history", ".html")
    if name is None:
        return []
    text = bundle.read_text(name)
    entries = []
    order = 0
    for block in _ENTRY_RE.findall(text):
        links = _A_RE.findall(block)
        tail = html.unescape(_TAG_RE.sub("\n", block))
        lines = [l.strip() for l in tail.split("\n") if l.strip()]
        date_raw = lines[-1] if lines else ""
        for url, inner in links:
            vid = _video_id_from_url(url)
            if not vid:
                continue
            title = _clean(inner) or "(без названия)"
            entries.append(HistoryEntry(
                kind="watch", video_id=vid, title=title,
                url=html.unescape(url), date_raw=date_raw, order=order,
                iso_date=_try_iso_date(date_raw),
            ))
            order += 1
    return entries


def parse_search_history(bundle: TakeoutBundle):
    name = find_member(bundle, "поиска", ".html")
    if name is None:
        name = find_member(bundle, "search", "history", ".html")
    if name is None:
        return []
    text = bundle.read_text(name)
    entries = []
    order = 0
    for block in _ENTRY_RE.findall(text):
        if "search?" not in block and "search_query" not in block:
            continue
        tail = html.unescape(_TAG_RE.sub("\n", block))
        lines = [l.strip() for l in tail.split("\n") if l.strip()]
        query = ""
        m = re.search(r"search_query=([^&\"<]+)", block)
        if m:
            query = _clean(html.unescape(urllib.parse.unquote(m.group(1))))
        elif lines:
            query = lines[1] if len(lines) > 1 else lines[0]
        date_raw = lines[-1] if lines else ""
        entries.append(HistoryEntry(
            kind="search", video_id=None, title=query,
            url="https://www.youtube.com/results?search_query=" + query,
            date_raw=date_raw, order=order,
            iso_date=_try_iso_date(date_raw),
        ))
        order += 1
    return entries
