"""Движок восстановления: подписки, история просмотров, распаковка видео.

Подписки оформляются тем же внутренним запросом (youtubei/v1/subscription/write),
который выполняет сама страница YouTube при нажатии «Подписаться» — из контекста
страницы в браузере пользователя, с его cookies. Никаких API-ключей.

История просмотров: у YouTube нет ни API, ни иного санкционированного способа
записать историю. Единственный работающий приём — открыть видео в залогиненном
браузере и дать ему проиграться несколько секунд: YouTube сам добавит его в
историю. Делаем это автоматически, подряд, с паузами.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import urllib.parse

from . import chrome
from .state import State

# JS: подписаться на канал из контекста страницы YouTube
_SUBSCRIBE_JS = """
const id = %(id)s;
if (!window.ytcfg) return {ok:false, error:'no_ytcfg'};
const key = ytcfg.get('INNERTUBE_API_KEY');
const ctx = ytcfg.get('INNERTUBE_CONTEXT');
const cName = ytcfg.get('INNERTUBE_CONTEXT_CLIENT_NAME');
const cVer = ytcfg.get('INNERTUBE_CONTEXT_CLIENT_VERSION');
const m = document.cookie.match(/SAPISID=([^;]+)/);
if (!m) return {ok:false, error:'not_logged_in'};
const SAP = m[1];
const t = Math.floor(Date.now()/1000);
async function sha1hex(s){
  const b = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
}
const auth = 'SAPISIDHASH ' + t + '_' + await sha1hex(SAP + ' ' + location.origin + ' ' + t);
const r = await fetch('/youtubei/v1/subscription/write?key=' + key, {
  method: 'POST', credentials: 'include',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': auth,
    'X-Youtube-Client-Name': String(cName),
    'X-Youtube-Client-Version': cVer
  },
  body: JSON.stringify({context: ctx, channelIds: [id], params: '', actions: ['ACTION_SUBSCRIBE']})
});
const j = await r.json().catch(()=>({}));
if (!r.ok) return {ok:false, error: 'HTTP ' + r.status + (j.error ? ' ' + (j.error.message||'') : '')};
return {ok:true};
"""

# JS: проверка, что пользователь залогинен на youtube.com
_LOGIN_CHECK_JS = """
(() => {
  if (!window.ytcfg) return {page: location.href, logged_in: false};
  const m = document.cookie.match(/SAPISID=([^;]+)/);
  return {page: location.href, logged_in: !!(m && ytcfg.get('LOGGED_IN'))};
})()
"""

# JS: проверить доступность видео через oembed
_AVAIL_JS = """
(async () => {
  const target = 'https://www.youtube.com/watch?v=' + %(id)s;
  const r = await fetch('https://www.youtube.com/oembed?url=' + encodeURIComponent(target) + '&format=json', {credentials:'omit'});
  if (r.ok) return {ok:true};
  if (r.status === 401 || r.status === 403) return {ok:false, reason:'restricted'};
  return {ok:false, reason:'unavailable'};
})()
"""

# JS: статус плеера на странице просмотра
_PLAYER_STATE_JS = """
(() => {
  const p = document.querySelector('.html5-video-player');
  const v = document.querySelector('video.html5-main-video');
  return {
    ad: p ? p.classList.contains('ad-showing') : null,
    t: v ? v.currentTime : null,
    dur: v ? v.duration : null,
    paused: v ? v.paused : null
  };
})()
"""

_SKIP_AD_JS = """
(() => {
  const btn = document.querySelector('.ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button, .ytp-ad-skip-button-slot button');
  if (btn) { btn.click(); return true; }
  return false;
})()
"""


def _js_template(tpl: str, **kw) -> str:
    return tpl % {k: json.dumps(v) for k, v in kw.items()}


class RestoreEngine:
    def __init__(self, state: State, data_dir: str):
        self.state = state
        self.data_dir = data_dir
        self._stop = threading.Event()
        self._subs_thread: threading.Thread | None = None
        self._history_thread: threading.Thread | None = None
        self._videos_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Браузер
    # ------------------------------------------------------------------

    def browser_start(self) -> str:
        chrome._load_port(self.data_dir)
        profile = self.data_dir + "/chrome-profile"
        msg = chrome.start_browser(profile)
        # дождаться вкладки youtube и проверить логин
        time.sleep(3)
        self.browser_refresh_status()
        return msg

    def browser_refresh_status(self) -> dict:
        """Проверить, что браузер жив и пользователь залогинен."""
        result = {"running": False, "logged_in": None, "message": ""}
        # Chrome при первом запуске профиля может перезапуститься — ждём
        last_err = None
        for _ in range(10):
            try:
                return self._browser_status_once(result)
            except Exception as e:
                last_err = e
                time.sleep(1.5)
        result["message"] = "Браузер не запущен или недоступен."
        result["error"] = str(last_err)
        self.state.update(lambda d: d["browser"].update(**result))
        return result

    def _browser_status_once(self, result) -> dict:
        try:
            page, cdp = chrome.get_page_target()
            with cdp:
                chrome.wait_page_ready(cdp, timeout=20)
                info = cdp.evaluate(_LOGIN_CHECK_JS, timeout=15) or {}
            url = info.get("page", "")
            if "consent" in url:
                result["message"] = ("Откройте окно браузера и подтвердите "
                                     "запрос YouTube (страница согласия).")
            result["running"] = True
            result["logged_in"] = bool(info.get("logged_in"))
            if result["logged_in"]:
                result["message"] = "Браузер подключён, вход выполнен."
        except Exception as e:
            result["message"] = "Браузер не запущен или недоступен."
            result["error"] = str(e)
        self.state.update(lambda d: d["browser"].update(**result))
        return result

    # ------------------------------------------------------------------
    # Подписки
    # ------------------------------------------------------------------

    def start_subscriptions(self, ids: list[str]):
        if self.state.get()["worker"]["subs_running"]:
            return False, "Подписки уже восстанавливаются"
        self._stop.clear()
        t = threading.Thread(target=self._subs_worker, args=(ids,), daemon=True)
        self._subs_thread = t
        t.start()
        return True, "Запущено"

    def _subs_worker(self, ids: list[str]):
        st = self.state
        st.update(lambda d: d["worker"].update(subs_running=True))
        try:
            page, cdp = chrome.get_page_target()
        except Exception as e:
            st.message("Не удалось подключиться к браузеру: %s" % e)
            st.update(lambda d: d["worker"].update(subs_running=False))
            return
        with cdp:
            chrome.wait_page_ready(cdp, timeout=30)
            info = cdp.evaluate(_LOGIN_CHECK_JS) or {}
            if not info.get("logged_in"):
                st.message("Сначала войдите в НОВЫЙ Google-аккаунт в открывшемся окне браузера.")
                st.update(lambda d: d["worker"].update(subs_running=False))
                return
            done = 0
            for cid in ids:
                if self._stop.is_set():
                    st.message("Остановлено пользователем.")
                    break
                st.update(lambda d, c=cid: d["subs"][c].update(status="subscribing"))
                try:
                    res = cdp.evaluate_async(_js_template(_SUBSCRIBE_JS, id=cid), timeout=30)
                    ok = bool(res and res.get("ok"))
                    err = "" if ok else str((res or {}).get("error", "unknown"))
                    status = "done" if ok else "error"
                    if err == "not_logged_in":
                        st.message("Выход из аккаунта — остановлено. Войдите заново и продолжите.")
                        st.update(lambda d, c=cid: d["subs"][c].update(status="pending"))
                        break
                except Exception as e:
                    ok, err, status = False, str(e)[:300], "error"
                st.update(lambda d, c=cid, s=status, e=err, dd=done: (
                    d["subs"][c].update(status=s, error=e),
                    d["worker"].update(last_message="Подписки: %d/%d" % (dd + 1, len(ids)))))
                done += 1
                time.sleep(1.0)
        st.update(lambda d: d["worker"].update(subs_running=False,
                                               last_message="Подписки: готово (%d/%d)" % (done, len(ids))))

    # ------------------------------------------------------------------
    # История просмотров
    # ------------------------------------------------------------------

    def start_history(self, ids: list[str], seconds: int = 10, delay: float = 2.0):
        if self.state.get()["worker"]["history_running"]:
            return False, "История уже восстанавливается"
        self._stop.clear()
        t = threading.Thread(target=self._history_worker,
                             args=(ids, seconds, delay), daemon=True)
        self._history_thread = t
        t.start()
        return True, "Запущено"

    def stop_workers(self):
        self._stop.set()

    def _history_worker(self, ids: list[str], seconds: int, delay: float):
        st = self.state
        st.update(lambda d: d["worker"].update(history_running=True))
        try:
            page, cdp = chrome.get_page_target()
        except Exception as e:
            st.message("Не удалось подключиться к браузеру: %s" % e)
            st.update(lambda d: d["worker"].update(history_running=False))
            return
        with cdp:
            chrome.wait_page_ready(cdp, timeout=30)
            info = cdp.evaluate(_LOGIN_CHECK_JS) or {}
            if not info.get("logged_in"):
                st.message("Сначала войдите в НОВЫЙ Google-аккаунт в браузере (вкладка YouTube).")
                st.update(lambda d: d["worker"].update(history_running=False))
                return
            done = 0
            for vid in ids:
                if self._stop.is_set():
                    st.message("Остановлено пользователем (можно продолжить).")
                    break
                try:
                    self._watch_one(cdp, vid, seconds)
                    status, err = "done", ""
                except Exception as e:
                    status, err = "error", str(e)[:300]
                st.update(lambda d, v=vid, s=status, e=err, dd=done: (
                    d["history"][v].update(status=s, error=e),
                    d["worker"].update(last_message="История: %d/%d" % (dd + 1, len(ids)))))
                done += 1
                time.sleep(delay)
        final = ("История: остановлено (%d/%d)" if self._stop.is_set()
                 else "История: готово (%d/%d)") % (done, len(ids))
        st.update(lambda d: d["worker"].update(history_running=False,
                                               last_message=final))

    def _watch_one(self, cdp, vid: str, seconds: int):
        st = self.state
        # 1) доступность
        try:
            avail = cdp.evaluate_async(_js_template(_AVAIL_JS, id=vid), timeout=20)
            if avail and not avail.get("ok"):
                reason = avail.get("reason", "unavailable")
                st.update(lambda d, v=vid, r=reason: d["history"][v].update(
                    status="unavailable" if r == "unavailable" else "error",
                    error="недоступно" if r == "unavailable" else "ограничено"))
                return
        except Exception:
            pass  # если проверка упала — всё равно попробуем открыть
        # 2) открыть страницу
        url = "https://www.youtube.com/watch?v=" + vid
        cdp.call("Page.navigate", {"url": url}, timeout=30)
        chrome.wait_page_ready(cdp, timeout=30)
        time.sleep(2.5)
        # 3) реклама: жмём «пропустить», ждём окончания (максимум 45 с)
        deadline = time.time() + 45
        while time.time() < deadline:
            if self._stop.is_set():
                return
            try:
                s = cdp.evaluate(_PLAYER_STATE_JS, timeout=10) or {}
            except Exception:
                s = {}
            if not s.get("ad"):
                break
            try:
                cdp.evaluate(_SKIP_AD_JS, timeout=5)
            except Exception:
                pass
            time.sleep(1.5)
        # 4) включить воспроизведение без звука
        try:
            cdp.evaluate_async("""
              const v = document.querySelector('video.html5-main-video');
              if (!v) return {ok:false};
              v.muted = true;
              try { await v.play(); } catch(e) { return {ok:false, err:String(e)}; }
              return {ok:true};
            """, timeout=15)
        except Exception:
            pass
        # 5) ждать, пока проиграется `seconds` секунд контента
        deadline = time.time() + max(60, seconds * 6)
        while time.time() < deadline:
            if self._stop.is_set():
                return
            try:
                s = cdp.evaluate(_PLAYER_STATE_JS, timeout=10) or {}
            except Exception:
                s = {}
            if not s.get("ad") and isinstance(s.get("t"), (int, float)) and s["t"] >= seconds:
                break
            time.sleep(1.5)
        time.sleep(1.0)

    # ------------------------------------------------------------------
    # Видео (распаковка)
    # ------------------------------------------------------------------

    def start_videos(self, bundle):
        if self.state.get()["videos"].get("running"):
            return False, "Уже идёт распаковка"
        t = threading.Thread(target=self._videos_worker, args=(bundle,), daemon=True)
        self._videos_thread = t
        t.start()
        return True, "Запущено"

    def _videos_worker(self, bundle):
        st = self.state
        dest = self.data_dir + "/videos"
        st.update(lambda d: d["videos"].update(running=True, done=0, current="",
                                               total=len(bundle.video_members())))

        def progress(i, total, name):
            st.update(lambda d: d["videos"].update(done=i, current=name,
                                                   total=total))

        try:
            done = bundle.extract_videos(dest, progress=progress,
                                         should_stop=self._stop.is_set)
            st.update(lambda d: d["videos"].update(
                running=False, current="",
                files=sorted(f for f, _ in done),
                last_message="Видео распакованы: %d" % len(done)))
        except Exception as e:
            st.update(lambda d: d["videos"].update(running=False,
                                                   last_message="Ошибка распаковки: %s" % e))
            traceback.print_exc()
