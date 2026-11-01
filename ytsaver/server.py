"""Локальный веб-сервер с интерфейсом YouTube Saver."""

from __future__ import annotations

import html
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .restore import RestoreEngine
from .state import State


def make_handler(engine: RestoreEngine, state: State, bundle, data_dir: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "YTSaver/1.0"

        def log_message(self, *a):  # тише в консоль
            pass

        # -- утилиты -------------------------------------------------------

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _html(self, text: str, code: int = 200):
            self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        # -- GET -----------------------------------------------------------

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/" or path == "/index.html":
                return self._html(UI_HTML)
            if path == "/api/overview":
                return self._json(self.overview())
            if path == "/api/export/history.json":
                return self._json([e for e in self._history_export()])
            if path == "/api/export/history.csv":
                rows = ["video_id,title,url,date,iso_date,status"]
                for e in self._history_export():
                    rows.append(",".join(_csv_field(x) for x in (
                        e["video_id"], e["title"], e["url"], e["date_raw"],
                        e["iso_date"] or "", e["status"])))
                return self._send(200, ("\n".join(rows)).encode("utf-8-sig"),
                                  "text/csv; charset=utf-8")
            if path == "/api/export/search.csv":
                rows = ["query,date,iso_date"]
                for e in state.get()["search"]:
                    rows.append(",".join(_csv_field(x) for x in (
                        e["title"], e["date_raw"], e["iso_date"] or "")))
                return self._send(200, ("\n".join(rows)).encode("utf-8-sig"),
                                  "text/csv; charset=utf-8")
            if path == "/archive":
                return self._html(archive_page(state))
            if path == "/memories":
                return self._html(memories_page(data_dir))
            if path.startswith("/videos/"):
                return self.serve_video(path)
            self._send(404, b"not found", "text/plain")

        def serve_video(self, path):
            rel = urllib.parse.unquote(path[len("/videos/"):])
            full = os.path.normpath(os.path.join(data_dir, "videos", rel))
            if not full.startswith(os.path.normpath(os.path.join(data_dir, "videos"))) \
                    or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            size = os.path.getsize(full)
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and "=" in rng:
                _, _, r = rng.partition("=")
                a, _, b = r.partition("-")
                if a:
                    start = int(a)
                if b:
                    end = min(int(b), size - 1)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1024 * 512, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _history_export(self):
            data = state.get()
            out = []
            for e in data["history"].values():
                out.append({
                    "video_id": e["video_id"], "title": e["title"],
                    "url": e["url"], "date_raw": e["date_raw"],
                    "iso_date": e.get("iso_date"), "status": e["status"],
                })
            out.sort(key=lambda x: x["iso_date"] or "0", reverse=True)
            return out

        def overview(self):
            data = state.get()
            counts = {"done": 0, "pending": 0, "error": 0, "unavailable": 0,
                      "watching": 0, "total": 0}
            for e in data["history"].values():
                counts[e["status"]] = counts.get(e["status"], 0) + 1
                counts["total"] += 1
            subs = sorted(data["subs"].values(), key=lambda s: s["title"].lower())
            sub_counts = {}
            for s in data["subs"].values():
                sub_counts[s["status"]] = sub_counts.get(s["status"], 0) + 1
            return {
                "browser": data["browser"],
                "worker": data["worker"],
                "videos": data["videos"],
                "subs": subs,
                "sub_counts": sub_counts,
                "history_counts": counts,
                "search_count": len(data["search"]),
            }

        # -- POST ----------------------------------------------------------

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            body = self._body()
            if path == "/api/browser/start":
                try:
                    msg = engine.browser_start()
                    return self._json({"ok": True, "message": msg})
                except Exception as e:
                    return self._json({"ok": False, "message": str(e)}, 500)
            if path == "/api/browser/check":
                r = engine.browser_refresh_status()
                return self._json({"ok": True, **r})
            if path == "/api/subs/restore":
                ids = body.get("ids") or [k for k, v in state.get()["subs"].items()
                                          if v["status"] in ("pending", "error")]
                if not ids:
                    return self._json({"ok": False, "message": "Нет каналов для подписки"})
                ok, msg = engine.start_subscriptions(ids)
                return self._json({"ok": ok, "message": msg})
            if path == "/api/history/start":
                data = state.get()
                if body.get("ids"):
                    ids = body["ids"]
                else:
                    ids = [k for k, v in data["history"].items()
                           if v["status"] in ("pending", "error")]
                    # от новых к старым — как в оригинальной истории
                    ids.sort(key=lambda k: -data["history"][k]["order"])
                seconds = int(body.get("seconds", 10))
                delay = float(body.get("delay", 2.0))
                ok, msg = engine.start_history(ids, seconds, delay)
                return self._json({"ok": ok, "message": msg, "count": len(ids)})
            if path == "/api/stop":
                engine.stop_workers()
                return self._json({"ok": True, "message": "Останавливаем…"})
            if path == "/api/videos/extract":
                if bundle is None:
                    return self._json({"ok": False, "message": "Архивы не найдены"})
                ok, msg = engine.start_videos(bundle)
                return self._json({"ok": ok, "message": msg})
            if path == "/api/state/reset-history":
                state.update(lambda d: [v.update(status="pending", error="")
                                        for v in d["history"].values()])
                return self._json({"ok": True})
            self._json({"ok": False, "message": "unknown endpoint"}, 404)

    return Handler


def _csv_field(v) -> str:
    v = str(v or "")
    if any(c in v for c in ",\"\n"):
        v = '"' + v.replace('"', '""') + '"'
    return v


# ---------------------------------------------------------------------------
# Страница-архив истории (локальные «воспоминания»)
# ---------------------------------------------------------------------------

def archive_page(state: State) -> str:
    data = state.get()
    watches = [e for e in data["history"].values()]
    watches.sort(key=lambda e: e["iso_date"] or "", reverse=True)
    searches = [e for e in data["search"]]
    searches.sort(key=lambda e: e["iso_date"] or "", reverse=True)

    rows = []
    for e in watches:
        date = e["iso_date"] or e["date_raw"]
        thumb = f"https://i.ytimg.com/vi/{e['video_id']}/mqdefault.jpg"
        status_badge = _STATUS_RU.get(e["status"], e["status"])
        rows.append(
            f'<div class="card {_esc(e["status"])}">'
            f'<img loading="lazy" src="{thumb}" alt="">'
            f'<div><a href="{_esc(e["url"])}" target="_blank" rel="noopener">'
            f'{_esc(e["title"])}</a>'
            f'<div class="meta">{_esc(date)} · {status_badge}</div></div></div>')
    srows = []
    for e in searches:
        srows.append(f'<li>{_esc(e["title"])} <span class="meta">{_esc(e["iso_date"] or e["date_raw"])}</span></li>')

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>История YouTube — архив</title><style>
body{{background:#0f0f0f;color:#f1f1f1;font-family:Roboto,Arial,sans-serif;max-width:900px;margin:0 auto;padding:20px}}
h1{{font-size:22px}} h2{{font-size:18px;margin-top:32px}}
.card{{display:flex;gap:12px;padding:8px;border-radius:8px;align-items:center;margin-bottom:4px}}
.card:hover{{background:#1f1f1f}}
.card img{{width:160px;height:90px;object-fit:cover;border-radius:6px;background:#222}}
.card a{{color:#f1f1f1;text-decoration:none;font-size:15px}}
.card a:hover{{text-decoration:underline}}
.meta{{color:#aaa;font-size:12px;margin-top:3px}}
.card.done .meta::before{{content:"✓ ";color:#4caf50}}
.card.unavailable img{{opacity:.25}}
ol{{color:#ddd}} li{{margin-bottom:6px}}
.back{{color:#3ea6ff;text-decoration:none}}
</style></head><body>
<a class="back" href="/">← Вернуться в панель</a>
<h1>Архив истории просмотров ({len(watches)})</h1>
<p class="meta">От новых к старым. Записи со статусом «недоступно» уже удалены с YouTube или скрыты.</p>
{''.join(rows)}
<h2>История поиска ({len(searches)})</h2>
<ol>{''.join(srows)}</ol>
</body></html>"""


_STATUS_RU = {
    "done": "восстановлено", "pending": "не восстановлено",
    "error": "ошибка", "unavailable": "недоступно",
    "watching": "сейчас", "subscribing": "сейчас",
}


def memories_page(data_dir: str) -> str:
    vdir = os.path.join(data_dir, "videos")
    items = []
    if os.path.isdir(vdir):
        for f in sorted(os.listdir(vdir)):
            if os.path.splitext(f)[1].lower() not in {".mp4", ".webm", ".mov", ".mkv", ".m4v"}:
                continue
            safe = urllib.parse.quote(f)
            items.append(
                f'<div class="vid"><h3>{html.escape(os.path.splitext(f)[0])}</h3>'
                f'<video controls preload="metadata" src="/videos/{safe}"></video></div>')
    inner = "".join(items) or "<p>Видео ещё не распакованы — запустите распаковку на главной странице.</p>"
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Мои видео — воспоминания</title><style>
body{{background:#0f0f0f;color:#f1f1f1;font-family:Roboto,Arial,sans-serif;max-width:800px;margin:0 auto;padding:20px}}
h1{{font-size:22px}} .vid{{margin-bottom:32px}} h3{{font-size:15px;font-weight:500}}
video{{width:100%;border-radius:10px;background:#000}}
.back{{color:#3ea6ff;text-decoration:none}}
</style></head><body>
<a class="back" href="/">← Вернуться в панель</a>
<h1>Мои опубликованные видео ({len(items)})</h1>
{inner}
</body></html>"""


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

UI_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>YouTube Saver — восстановление канала</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0f0f0f;--card:#1c1c1c;--line:#333;--txt:#f1f1f1;--dim:#aaa;--acc:#3ea6ff;--ok:#4caf50;--err:#ff5c5c}
*{box-sizing:border-box} body{background:var(--bg);color:var(--txt);font-family:Roboto,'Segoe UI',Arial,sans-serif;margin:0;padding:24px;max-width:960px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px} h2{font-size:18px;margin:28px 0 10px}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
button{background:var(--acc);color:#000;border:0;border-radius:18px;padding:9px 18px;font-size:14px;font-weight:600;cursor:pointer;margin-right:8px;margin-top:6px}
button:disabled{opacity:.4;cursor:default}
button.ghost{background:transparent;color:var(--acc);border:1px solid var(--acc)}
button.danger{background:transparent;color:var(--err);border:1px solid var(--err)}
.status{font-size:13px;color:var(--dim);margin-top:8px;white-space:pre-wrap}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;background:#333;margin-left:6px}
.badge.ok{background:#1b3a1e;color:var(--ok)} .badge.err{background:#3a1b1b;color:var(--err)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
td a{color:var(--txt);text-decoration:none} td a:hover{color:var(--acc)}
input[type=checkbox]{accent-color:var(--acc)}
.bar{height:6px;background:#333;border-radius:3px;overflow:hidden;margin-top:8px}
.bar>div{height:100%;background:var(--acc);transition:width .4s}
.row{display:flex;gap:16px;flex-wrap:wrap} .row>.panel{flex:1;min-width:260px}
.kpi{font-size:26px;font-weight:700} .kpi small{font-size:12px;color:var(--dim);font-weight:400}
a.link{color:var(--acc);text-decoration:none}
label.opt{color:var(--dim);font-size:13px;margin-right:12px}
input[type=number]{width:70px;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:4px 6px}
#log{font-size:12px;color:var(--dim);max-height:120px;overflow-y:auto}
.note{font-size:12px;color:var(--dim);line-height:1.5}
</style></head><body>
<h1>YouTube Saver</h1>
<div class="sub">Восстановление подписок и истории просмотров из Google Takeout · <a class="link" href="/archive" target="_blank">архив истории</a> · <a class="link" href="/memories" target="_blank">мои видео</a></div>

<div class="panel">
  <b>1. Браузер</b>
  <div class="note">Запустится отдельное окно Chrome со своим профилем. Войдите в нём в ваш <b>новый</b> Google-аккаунт (один раз). Данные профиля остаются в папке программы.</div>
  <button onclick="api('/api/browser/start',{},this)">Запустить браузер</button>
  <button class="ghost" onclick="api('/api/browser/check',{},this)">Проверить вход</button>
  <div class="status" id="browserStatus">—</div>
</div>

<div class="panel">
  <b>2. Подписки</b> <span class="badge" id="subsBadge"></span>
  <div class="note" style="margin-top:6px">Сервис оформит подписки тем же запросом, что и кнопка «Подписаться» на сайте — из вашего браузера, без API-ключей. Снимите галочки с каналов, которые не нужны.</div>
  <button onclick="restoreSubs(this)">Восстановить выбранные</button>
  <button class="danger" onclick="api('/api/stop',{},this)">Стоп</button>
  <div style="max-height:300px;overflow-y:auto;margin-top:10px">
  <table id="subsTable"></table></div>
</div>

<div class="panel">
  <b>3. История просмотров</b>
  <div class="row" style="margin:10px 0">
    <div class="panel" style="margin:0"><div class="kpi" id="kTotal">0<small> всего</small></div></div>
    <div class="panel" style="margin:0"><div class="kpi" id="kDone">0<small> восстановлено</small></div></div>
    <div class="panel" style="margin:0"><div class="kpi" id="kBad">0<small> недоступно</small></div></div>
    <div class="panel" style="margin:0"><div class="kpi" id="kErr">0<small> ошибок</small></div></div>
  </div>
  <div class="bar"><div id="hBar" style="width:0%"></div></div>
  <div class="note" style="margin-top:10px">YouTube не даёт записать историю напрямую (даже через официальный API), поэтому сервис открывает каждое видео на несколько секунд в вашем браузере — YouTube сам добавляет его в историю. Недоступные (удалённые/скрытые) видео пропускаются. Примерно 15 секунд на видео; можно останавливать и продолжать.</div>
  <label class="opt">секунд на видео: <input type="number" id="secInput" value="10" min="5" max="60"></label>
  <button onclick="startHistory(this)">Восстановить историю</button>
  <button class="danger" onclick="api('/api/stop',{},this)">Стоп</button>
</div>

<div class="panel">
  <b>4. Опубликованные видео</b> <span class="badge" id="vidBadge"></span>
  <div class="note" style="margin-top:6px">Распаковать ваши загруженные видео из архива в папку <code>data/videos</code> (останутся как воспоминания, смотрятся на странице «мои видео»).</div>
  <button onclick="api('/api/videos/extract',{},this)">Распаковать видео</button>
  <div class="bar"><div id="vBar" style="width:0%"></div></div>
  <div class="status" id="vidStatus"></div>
</div>

<div class="panel">
  <b>Экспорт</b>
  <div class="note" style="margin-top:6px">Локальные копии данных — на случай, если понадобится своя обработка.</div>
  <a class="link" href="/api/export/history.json"><button class="ghost">история (JSON)</button></a>
  <a class="link" href="/api/export/history.csv"><button class="ghost">история (CSV)</button></a>
  <a class="link" href="/api/export/search.csv"><button class="ghost">поиск (CSV)</button></a>
</div>

<div class="panel"><b>Журнал</b><div id="log"></div></div>

<script>
let S=null, logLines=[];
async function api(url, body, btn){
  if(btn) btn.disabled=true;
  try{
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
    const j=await r.json();
    if(j.message) log(j.message);
    refresh();
    return j;
  } finally { if(btn) btn.disabled=false; }
}
function log(t){ logLines.unshift(new Date().toLocaleTimeString()+'  '+t); logLines=logLines.slice(0,40);
  document.getElementById('log').textContent=logLines.join('\\n'); }
async function restoreSubs(btn){
  const ids=[...document.querySelectorAll('#subsTable input:checked')].map(c=>c.dataset.id);
  if(!ids.length){log('Выберите хотя бы один канал');return;}
  await api('/api/subs/restore',{ids},btn);
}
async function startHistory(btn){
  const seconds=parseInt(document.getElementById('secInput').value||'10');
  await api('/api/history/start',{seconds,delay:2},btn);
}
function subsTable(subs){
  const t=document.getElementById('subsTable');
  t.innerHTML=subs.map(s=>`<tr>
    <td style="width:28px"><input type="checkbox" data-id="${s.channel_id}" ${s.status==='done'?'disabled':''} ${s.status==='done'?'':'checked'}></td>
    <td><a href="https://www.youtube.com/channel/${s.channel_id}" target="_blank">${esc(s.title)}</a>
      ${s.status!=='pending'?`<span class="badge ${s.status==='done'?'ok':s.status==='error'?'err':''}">${stRu(s.status)}</span>`:''}
      ${s.error?`<div class="note">${esc(s.error)}</div>`:''}</td></tr>`).join('');
}
function stRu(s){return {pending:'—',subscribing:'подписываем…',done:'готово',error:'ошибка',watching:'смотрим…',unavailable:'недоступно'}[s]||s;}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function refresh(){
  try{
    const r=await fetch('/api/overview'); S=await r.json();
  }catch(e){ return; }
  const B=S.browser;
  document.getElementById('browserStatus').textContent =
    (B.running? 'Браузер: запущен. ' : 'Браузер: не запущен. ')
    + (B.logged_in===true ? '✓ Вход выполнен.' : B.logged_in===false ? '✗ Вход не выполнен — войдите в окне браузера.' : '')
    + (B.message? ' '+B.message : '');
  const sc=S.sub_counts||{};
  document.getElementById('subsBadge').textContent =
    `готово ${sc.done||0} / всего ${S.subs.length}`;
  subsTable(S.subs);
  const c=S.history_counts;
  document.getElementById('kTotal').innerHTML=c.total+ '<small> всего</small>';
  document.getElementById('kDone').innerHTML=c.done+'<small> восстановлено</small>';
  document.getElementById('kBad').innerHTML=c.unavailable+'<small> недоступно</small>';
  document.getElementById('kErr').innerHTML=c.error+'<small> ошибок</small>';
  document.getElementById('hBar').style.width =
    (c.total? Math.round(100*(c.done+c.unavailable)/c.total):0)+'%';
  const V=S.videos;
  document.getElementById('vidBadge').textContent = V.total? `${V.done}/${V.total}` : 'нет данных';
  document.getElementById('vBar').style.width=(V.total? Math.round(100*V.done/V.total):0)+'%';
  document.getElementById('vidStatus').textContent = V.current||'';
  if(S.worker.last_message) log(S.worker.last_message);
}
refresh(); setInterval(refresh, 2500);
</script></body></html>"""


def start_server(engine: RestoreEngine, state: State, bundle, data_dir: str, port: int):
    handler = make_handler(engine, state, bundle, data_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
