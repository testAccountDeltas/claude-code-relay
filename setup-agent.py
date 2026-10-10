#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CliRelay Agent — установщик Claude Code в паре с CliRelay (или любым CLIProxyAPI-шлюзом).

Что делает:
  * спрашивает URL сервера и API-ключ;
  * ставит локальный фикс-прокси (вырезает служебный system-блок
    `x-anthropic-billing-header`, из-за которого шлюз отдаёт 429/503);
  * создаёт изолированный профиль Claude Code (не трогает твою обычную установку/подписку);
  * добавляет в контекстное меню ПАПКИ пункт «Запустить агента здесь»,
    который запускает `claude --dangerously-skip-permissions` в этой папке через шлюз;
  * настраивает автозапуск прокси при входе (Windows: ключ Run; macOS: LaunchAgent),
    чтобы он поднимался сам — в т.ч. для Claude Desktop, у которого нет лаунчера;
  * Windows и macOS.

Запуск:   python setup-agent.py
Удалить:  python setup-agent.py --uninstall

Требуется: Python 3.8+ и установленный Claude Code (`claude` в PATH).
"""
import os, sys, json, socket, argparse, urllib.request, urllib.error
from pathlib import Path
from urllib.parse import urlparse

APP = "clirelay-agent"
MENU_LABEL = "Запустить агента здесь"
PROXY_PORT = 8899
HOME = Path.home() / f".{APP}"

# ───────────────────────── шаблоны файлов ─────────────────────────

PROXY_PY = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Фикс-прокси: Claude Code -> сюда -> шлюз. Вырезает system-блок
# "x-anthropic-billing-header", на котором Gemini-путь шлюза отдаёт 429/503.
# Стримит SSE, логирует запросы/ответы и отслеживает аномалии (thinking без текста).
import http.server, http.client, json, time, sys, os, threading

UPSTREAM_HOST = "__HOST__"
UPSTREAM_PORT = __PORT_UP__
USE_HTTPS     = __HTTPS__
PORT          = __PORT__
LOG           = r"__LOG__"
MARK          = "x-anthropic-billing-header"
HOP           = ("transfer-encoding", "connection", "content-length", "content-encoding")
EMPTY_RETRY_MAX = 2  # сколько раз переиграть пустой ход (thinking-only, без текста/тулов)

# Ротация лога: как только файл перевалил за LOG_MAX_BYTES, его сдвигают в .1 (старый .1 -> .2,
# .2 -> .3, .3 удаляется) и пишут в свежий LOG. Не больше LOG_BACKUPS бэкапов — старые утилизируются.
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 3
_log_lock = threading.Lock()
def _rotate_log():
    try:
        if not os.path.exists(LOG) or os.path.getsize(LOG) < LOG_MAX_BYTES: return
        oldest = f"{LOG}.{LOG_BACKUPS}"
        if os.path.exists(oldest): os.remove(oldest)
        for i in range(LOG_BACKUPS - 1, 0, -1):
            src = f"{LOG}.{i}"
            if os.path.exists(src): os.replace(src, f"{LOG}.{i + 1}")
        os.replace(LOG, f"{LOG}.1")
    except Exception: pass

def log(s):
    try:
        with _log_lock:
            _rotate_log()
            with open(LOG, "a", encoding="utf-8") as f: f.write(s + "\n")
    except Exception: pass

def scrub(body):
    try: j = json.loads(body)
    except Exception: return body, False
    sysv = j.get("system"); changed = False
    if isinstance(sysv, list):
        new = [b for b in sysv if not (isinstance(b, dict) and MARK in str(b.get("text","")))]
        if len(new) != len(sysv): j["system"] = new; changed = True
    elif isinstance(sysv, str) and MARK in sysv:
        j["system"] = "\n".join(l for l in sysv.splitlines() if MARK not in l); changed = True
    return (json.dumps(j).encode("utf-8"), True) if changed else (body, False)

# Ремап claude-имён -> модели шлюза (для Claude Desktop: он требует Anthropic-вид имени,
# поэтому в inferenceModels задаём claude-* + supports1m, а gemini подставляем здесь).
# Claude Code не затрагивается — он шлёт gemini-* (префикс не 'claude-').
MODEL_MAP = [("claude-opus","gemini-3.8-flash-high"),("claude-fable","gemini-3.8-flash-high"),("claude-mythos","gemini-3.8-flash-high"),("claude-sonnet","gemini-3.8-flash-medium"),("claude-haiku","gemini-3.8-flash-low")]
def remap_model(body):
    try:
        j = json.loads(body); m = j.get("model","")
        for pref, tgt in MODEL_MAP:
            if isinstance(m,str) and m.startswith(pref):
                j["model"] = tgt
                return json.dumps(j).encode("utf-8"), (m, tgt)
    except Exception: pass
    return body, None

# «Уровень мышления». Claude Code шлёт thinking:{type:"adaptive"}, а уровень усилия — в
# ОТДЕЛЬНОМ поле output_config.effort (это /effort в CLI: low/medium/high/xhigh/max). Оно
# приходит В КАЖДОМ запросе и это ЯВНЫЙ выбор пользователя -> ПРИОРИТЕТ. Gemini-путь шлюза
# output_config НЕ читает, поэтому /effort был мёртв — переводим его сами в бюджет-суффикс
# (в шлюзе суффикс приоритетнее adaptive). EFFORT_MAP: /effort->бюджет; THINK_BY_TIER — запас,
# если effort нет. agy: low=1000/medium=4000; high ограничили до 24576 (agy -1=безлимит).
EFFORT_MAP    = {"low": "(1000)", "medium": "(4000)", "high": "(24576)", "xhigh": "(24576)", "max": "(24576)"}
THINK_BY_TIER = [("-high", "(24576)"), ("-medium", "(4000)"), ("-low", "(1000)")]
THINK_FORCE   = ""        # "" = по effort/тиру; иначе форс всем, напр. "(4000)"
THINK_DEFAULT = "(4000)"  # ни effort, ни тир не распознаны
def set_thinking(body):
    try:
        j = json.loads(body)
        m = j.get("model"); th = j.get("thinking")
        if not (isinstance(m, str) and m and not m.endswith(")")
                and isinstance(th, dict) and th.get("type") in ("adaptive", "enabled")):
            return body, None
        if THINK_FORCE:
            sfx, src = THINK_FORCE, "force"
        else:
            eff = (j.get("output_config") or {}).get("effort")
            if isinstance(eff, str) and eff.lower() in EFFORT_MAP:
                sfx, src = EFFORT_MAP[eff.lower()], "effort:" + eff.lower()
            else:
                sfx, src = THINK_DEFAULT, "default"
                for tier, s in THINK_BY_TIER:
                    if m.endswith(tier): sfx, src = s, "tier" + tier; break
        if not sfx:
            return body, None
        j["model"] = m + sfx
        return json.dumps(j).encode("utf-8"), (m, j["model"], src)
    except Exception:
        return body, None

_NUDGE = ("IMPORTANT: Produce a visible response now — either a textual answer or a tool "
          "call. Do NOT end your turn with only internal thinking and no output.")
def _nudge(body):
    try:
        j = json.loads(body)
        sysv = j.get("system")
        if isinstance(sysv, list): j["system"] = sysv + [{"type": "text", "text": _NUDGE}]
        elif isinstance(sysv, str): j["system"] = sysv + "\n\n" + _NUDGE
        else: j["system"] = _NUDGE
        return json.dumps(j).encode("utf-8")
    except Exception:
        return body

# Fallback-текст, если даже ретраи дали пустой ход (приём auto_heal у agy):
EMPTY_FALLBACK_TEXT = "(Пустой ответ модели восстановлен прокси — продолжи или повтори запрос.)"
def _continue(body):
    """Дописывает user-ход 'Continue.' — подталкивает модель выдать видимый ответ."""
    try:
        j = json.loads(body)
        msgs = j.get("messages")
        if isinstance(msgs, list):
            j["messages"] = msgs + [{"role": "user", "content": "Continue."}]
            return json.dumps(j).encode("utf-8")
    except Exception:
        pass
    return body

def summarize_req(body):
    try:
        j = json.loads(body)
        model = j.get("model", "?")
        stream = j.get("stream", False)
        msgs = j.get("messages", [])
        m_cnt = len(msgs)
        max_tok = j.get("max_tokens")
        thinking = j.get("thinking")
        th_s = ""
        if isinstance(thinking, dict):
            th_s = f" th={thinking.get('type')}:{thinking.get('budget_tokens')}"
        return f"model={model} msgs={m_cnt} stream={stream} max_tok={max_tok}{th_s}"
    except Exception:
        return f"raw_len={len(body)}"

class SSETracker:
    def __init__(self):
        self.buf = ""
        self.block_types = []
        self.text_chars = 0
        self.thinking_chars = 0
        self.tools = []
        self.stop_reason = None
        self.usage = None
        self.event_count = 0
        self.errors = []

    def feed(self, chunk_bytes):
        self.buf += chunk_bytes.decode("utf-8", errors="replace")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"): continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]": continue
            self.event_count += 1
            try:
                ev = json.loads(payload)
                t = ev.get("type")
                if t == "content_block_start":
                    cb = ev.get("content_block", {})
                    btype = cb.get("type")
                    self.block_types.append(btype)
                    if btype == "tool_use": self.tools.append(cb.get("name"))
                elif t == "content_block_delta":
                    d = ev.get("delta", {})
                    dtype = d.get("type")
                    if dtype == "text_delta": self.text_chars += len(d.get("text", ""))
                    elif dtype == "thinking_delta": self.thinking_chars += len(d.get("thinking", ""))
                elif t == "message_delta":
                    d = ev.get("delta", {})
                    if "stop_reason" in d: self.stop_reason = d.get("stop_reason")
                    if "usage" in ev: self.usage = ev.get("usage")
                elif t == "error":
                    self.errors.append(str(ev.get("error")))
            except Exception: pass

    def summary(self):
        parts = []
        if self.thinking_chars: parts.append(f"thinking={self.thinking_chars}c")
        if self.text_chars: parts.append(f"text={self.text_chars}c")
        if self.tools: parts.append(f"tools={','.join(str(t) for t in self.tools)}")
        if self.stop_reason: parts.append(f"stop={self.stop_reason}")
        if self.usage and self.usage.get("output_tokens") is not None:
            parts.append(f"out_tok={self.usage.get('output_tokens')}")
        if self.errors: parts.append(f"errs={self.errors}")
        res = " ".join(parts) if parts else "no_blocks"
        if self.thinking_chars > 0 and self.text_chars == 0 and not self.tools and self.stop_reason == "end_turn":
            res += " [!!! EMPTY_VISIBLE_OUTPUT: thinking-only, 0 text, end_turn !!!]"
        return res

_CONNECT_TIMEOUT = 4    # короткий таймаут на connect+TLS: мёртвый SYN (10060) отваливается за ~4с, не за 21с
_READ_TIMEOUT    = 600  # после установки соединения — длинный таймаут на чтение SSE-стрима
def _conn():
    cls = http.client.HTTPSConnection if USE_HTTPS else http.client.HTTPConnection
    return cls(UPSTREAM_HOST, UPSTREAM_PORT, timeout=_CONNECT_TIMEOUT)

# Пул keep-alive соединений: переиспользуем открытые TCP+TLS вместо нового на
# каждый запрос. Обрыв (connect timeout) случается ИМЕННО при установке нового
# коннекта, когда сервер на миг теряет SYN под нагрузкой CPU; меньше новых
# коннектов -> меньше шансов словить обрыв. Ретрай ниже страхует остальное.
_pool = []; _pool_lock = threading.Lock(); _POOL_MAX = 8
# НЕ реюзаем соединение, простоявшее дольше этого: http.client не замечает idle-close
# от caddy -> reuse протухшего отдавал мусор (HTTP 200 "not a Message"/оборванный стрим).
# Короткий порог ловит выигрыш на tool-loop, но не даёт реюзать залежавшееся; протухшие
# не копятся (отбрасываются по возрасту при выдаче — то, что раньше лечил рестарт).
_POOL_IDLE_MAX = 10.0
def _acquire():
    now = time.time()
    with _pool_lock:
        while _pool:
            conn, ts = _pool.pop()
            if now - ts <= _POOL_IDLE_MAX: return conn, True
            try: conn.close()
            except Exception: pass
    return _conn(), False
def _release(conn, resp, ok):
    if conn is None: return
    if ok and resp is not None and not getattr(resp, "will_close", True):
        with _pool_lock:
            if len(_pool) < _POOL_MAX: _pool.append((conn, time.time())); return
    try: conn.close()
    except Exception: pass

# --- SSE-хелперы для recovery пустого хода ---
def _split_frames(buf):
    frames = []
    while "\n\n" in buf:
        frame, buf = buf.split("\n\n", 1); frames.append(frame)
    return frames, buf
def _parse_frame(frame):
    for line in frame.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            p = line[5:].strip()
            if not p or p == "[DONE]": return None, None
            try: obj = json.loads(p); return obj.get("type"), obj
            except Exception: return None, None
    return None, None
def _write_frame_obj(wfile, obj):
    wfile.write((f"event: {obj.get('type','')}\ndata: {json.dumps(obj, separators=(',',':'))}\n\n").encode("utf-8")); wfile.flush()
def _write_raw(wfile, frame):
    wfile.write((frame + "\n\n").encode("utf-8")); wfile.flush()

def _open_upstream(command, path, body, fh, rid, t0):
    """Открывает апстрим с ретраями (блип связи + 429/503). (conn, resp) или (None, None)."""
    attempt = 0
    while True:
        c, reused = _acquire()
        try:
            if not reused:
                c.connect()                       # форсим connect с коротким таймаутом (_CONNECT_TIMEOUT)
                c.sock.settimeout(_READ_TIMEOUT)  # после установки — длинный таймаут на чтение стрима
            c.request(command, path, body=body, headers=fh)
            resp = c.getresponse()
        except Exception as e:
            try: c.close()
            except Exception: pass
            if reused: continue
            attempt += 1
            dur = time.time() - t0
            log(f"[{time.strftime('%H:%M:%S')}] #{rid} !! conn err after {dur:.1f}s (att {attempt}): {e!r}")
            if attempt < 5: time.sleep(0.5); continue
            return None, None
        if resp.status in (429, 503) and attempt < 3:
            try:
                rb = resp.read()
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} -- attempt {attempt+1} -> {resp.status} {resp.reason} ({rb[:120].decode(errors='replace')}), retry in 3s")
            except Exception: pass
            _release(c, resp, ok=True); attempt += 1; time.sleep(3); continue
        return c, resp

def _stream_one(resp, wfile, offset, suppress_start):
    """Стримит один ответ ЖИВЬЁМ, придерживая терминальные кадры; при offset>0
    переиндексирует content_block_*; при suppress_start глушит message_start."""
    st = {"saw_text": False, "saw_tool": False, "saw_thinking": False,
          "stop_reason": None, "max_index": -1, "held": [], "usage": None, "err": None}
    buf = ""
    try:
        while True:
            chunk = resp.read(8192)
            if not chunk: break
            buf += chunk.decode("utf-8", "replace")
            frames, buf = _split_frames(buf)
            for frame in frames:
                if not frame.strip(): continue
                typ, obj = _parse_frame(frame)
                if typ is None: _write_raw(wfile, frame); continue
                if typ == "message_start":
                    if not suppress_start: _write_raw(wfile, frame)
                    continue
                if typ in ("content_block_start", "content_block_delta", "content_block_stop"):
                    idx = obj.get("index", 0)
                    st["max_index"] = max(st["max_index"], idx + offset)
                    if typ == "content_block_start":
                        bt = (obj.get("content_block") or {}).get("type")
                        if bt == "tool_use": st["saw_tool"] = True
                        elif bt == "thinking": st["saw_thinking"] = True
                    elif typ == "content_block_delta":
                        d = obj.get("delta") or {}
                        if d.get("type") == "text_delta" and (d.get("text") or "").strip(): st["saw_text"] = True
                        elif d.get("type") == "thinking_delta": st["saw_thinking"] = True
                    if offset: obj["index"] = idx + offset; _write_frame_obj(wfile, obj)
                    else: _write_raw(wfile, frame)
                    continue
                if typ == "message_delta":
                    d = obj.get("delta") or {}
                    if "stop_reason" in d: st["stop_reason"] = d.get("stop_reason")
                    st["usage"] = obj.get("usage"); st["held"].append(frame); continue
                if typ == "message_stop":
                    st["held"].append(frame); continue
                _write_raw(wfile, frame)
    except Exception as e:
        st["err"] = e
    return st

_req_id = 0

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _h(self):
        global _req_id
        _req_id += 1
        rid = _req_id
        t0 = time.time()
        ts = time.strftime("%H:%M:%S")

        if "/v1/v1/" in self.path:
            self.path = self.path.replace("/v1/v1/", "/v1/", 1)  # base_url задан с лишним /v1
        n = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(n) if n else b""
        body, scrubbed = scrub(raw_body)
        body, remapped = remap_model(body)
        body, think = set_thinking(body)
        meta = summarize_req(body) if "/v1/messages" in self.path else f"bytes={n}"
        rm = f" remap={remapped[0]}->{remapped[1]}" if remapped else ""
        tc = f" think={think[1]}[{think[2]}]" if think else ""
        log(f"[{ts}] #{rid} >> {self.command} {self.path} (scrub={scrubbed}){rm}{tc} {meta}")

        fh = {k: v for k, v in self.headers.items()
              if k.lower() not in ("host","content-length","accept-encoding",
                                    "connection","keep-alive","proxy-connection","te","upgrade")}
        fh["Host"] = UPSTREAM_HOST

        conn, resp = _open_upstream(self.command, self.path, body, fh, rid, t0)
        if resp is None:
            try: self.send_error(502, "upstream error")
            except Exception: pass
            return

        ctype = resp.getheader("content-type") or ""
        if "event-stream" in ctype and resp.status == 200:
            # ПОТОКОВАЯ передача SSE + recovery пустого хода: стримим содержимое живьём
            # (thinking виден сразу), но придерживаем терминальные кадры. Если ход вышел
            # пустым (0 текста, 0 тулов, end_turn) — переоткрываем апстрим и доклеиваем
            # ответ в тот же поток. Ошибка парсинга/склейки -> отдаём придержанное как есть.
            self.send_response(200)
            for k, v in resp.getheaders():
                if k.lower() not in HOP: self.send_header(k, v)
            self.send_header("Connection", "close"); self.end_headers()
            self.close_connection = True

            cur_conn, cur_resp = conn, resp
            offset, suppress, eretry = 0, False, 0
            final_st = None
            while True:
                st = _stream_one(cur_resp, self.wfile, offset, suppress)
                _release(cur_conn, cur_resp, ok=(st["err"] is None))
                empty = (not st["saw_text"] and not st["saw_tool"] and st["stop_reason"] == "end_turn")
                if st["err"] is None and empty and eretry < EMPTY_RETRY_MAX:
                    eretry += 1
                    log(f"[{time.strftime('%H:%M:%S')}] #{rid} .. EMPTY turn -> splice-retry {eretry}/{EMPTY_RETRY_MAX}")
                    nb = _continue(body if eretry < EMPTY_RETRY_MAX else _nudge(body))
                    cur_conn, cur_resp = _open_upstream(self.command, self.path, nb, fh, rid, t0)
                    if cur_resp is None or "event-stream" not in (cur_resp.getheader("content-type") or ""):
                        if cur_resp is not None: _release(cur_conn, cur_resp, ok=False)
                        final_st = st; break
                    offset = st["max_index"] + 1; suppress = True; continue
                final_st = st; break

            injected = False
            if final_st and final_st["err"] is None and not final_st["saw_text"] and not final_st["saw_tool"]:
                try:
                    idx = final_st["max_index"] + 1
                    _write_frame_obj(self.wfile, {"type":"content_block_start","index":idx,"content_block":{"type":"text","text":""}})
                    _write_frame_obj(self.wfile, {"type":"content_block_delta","index":idx,"delta":{"type":"text_delta","text":EMPTY_FALLBACK_TEXT}})
                    _write_frame_obj(self.wfile, {"type":"content_block_stop","index":idx})
                    injected = True
                except Exception: pass
            try:
                for frame in (final_st["held"] if final_st else []): _write_raw(self.wfile, frame)
            except Exception: pass
            dur = time.time() - t0
            log(f"[{time.strftime('%H:%M:%S')}] #{rid} << SSE done ({dur:.1f}s, empty_retries={eretry}, fallback={injected}, "
                f"text={final_st['saw_text'] if final_st else '?'}, tool={final_st['saw_tool'] if final_st else '?'}, "
                f"stop={final_st['stop_reason'] if final_st else '?'}"
                + (f", stream_err={final_st['err']!r}" if final_st and final_st['err'] else "") + ")")
        else:
            read_ok = True
            try: rb = resp.read()
            except Exception as e:
                read_ok = False
                dur = time.time() - t0
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} !! read err after {dur:.1f}s: {e!r}"); rb = b""
            dur = time.time() - t0
            if resp.status != 200:
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} << {resp.status} {resp.reason} ({dur:.1f}s, {len(rb)}B): {rb[:200].decode(errors='replace')}")
            else:
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} << 200 OK ({dur:.1f}s, {len(rb)}B)")
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP: self.send_header(k, v)
            self.send_header("Content-Length", str(len(rb))); self.end_headers()
            try: self.wfile.write(rb)
            except Exception: pass
            _release(conn, resp, ok=read_ok)
    do_POST = _h; do_GET = _h; do_PUT = _h
    def log_message(self, *a): pass

class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        if sys.exc_info()[0] in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError): return
        super().handle_error(request, client_address)

if __name__ == "__main__":
    log("=== proxy started %s  (->%s://%s:%s) (detailed logging + SSE tracking) ===" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), "https" if USE_HTTPS else "http", UPSTREAM_HOST, UPSTREAM_PORT))
    try:
        Srv(("127.0.0.1", PORT), H).serve_forever()
    except OSError as e:
        log("=== proxy НЕ стартовал (порт занят? %s) ===" % e)
'''

ENSURE_PY = r'''#!/usr/bin/env python3
# Поднимает фикс-прокси, если он ещё не запущен. Вызывается лаунчером.
import socket, subprocess, sys, os, time
HOME = os.path.join(os.path.expanduser("~"), ".clirelay-agent")
PORT = __PORT__
def up():
    try:
        s = socket.create_connection(("127.0.0.1", PORT), 0.5); s.close(); return True
    except Exception: return False
if not up():
    proxy = os.path.join(HOME, "relay-proxy.py")
    if os.name == "nt":
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pyw): pyw = sys.executable
        subprocess.Popen([pyw, proxy], creationflags=0x08000000)  # CREATE_NO_WINDOW
    else:
        subprocess.Popen([sys.executable, proxy], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(25):
        if up(): break
        time.sleep(0.3)
'''

LAUNCH_CMD = r'''@echo off
chcp 65001 >nul
set "AGENT_HOME=%USERPROFILE%\.clirelay-agent"
"{PYTHON}" "%AGENT_HOME%\ensure-proxy.py"
set "CLAUDE_CODE_SIMPLE=1"
set "CLAUDE_CONFIG_DIR=%AGENT_HOME%\claude-home"
set "ANTHROPIC_BASE_URL=http://127.0.0.1:{PORT}"
set "ANTHROPIC_API_KEY={KEY}"
REM Модель задаётся через settings.json (+ modelOverrides) — переключается в сессии: /model
set "ANTHROPIC_SMALL_FAST_MODEL={SMALL}"
set "CLAUDE_CODE_MAX_CONTEXT_TOKENS={CTX}"
set "CLAUDE_CODE_AUTO_COMPACT_WINDOW={COMPACT}"
REM потолок вывода как у нативного agy (~65536) — длинные ответы/мышление не режутся
set "CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000"
set "CLAUDE_CODE_ATTRIBUTION_HEADER=0"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
if not "%~1"=="" cd /d "%~1"
claude --dangerously-skip-permissions
'''

LAUNCH_SH = r'''#!/bin/bash
AGENT_HOME="$HOME/.clirelay-agent"
"{PYTHON}" "$AGENT_HOME/ensure-proxy.py"
export CLAUDE_CODE_SIMPLE=1
export CLAUDE_CONFIG_DIR="$AGENT_HOME/claude-home"
export ANTHROPIC_BASE_URL="http://127.0.0.1:{PORT}"
export ANTHROPIC_API_KEY="{KEY}"
# Модель задаётся через settings.json (+ modelOverrides) — переключается в сессии: /model
export ANTHROPIC_SMALL_FAST_MODEL="{SMALL}"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS={CTX}
export CLAUDE_CODE_AUTO_COMPACT_WINDOW={COMPACT}
# потолок вывода как у нативного agy (~65536) — длинные ответы/мышление не режутся
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
TARGET="$1"
[ -n "$TARGET" ] && cd "$TARGET"
exec claude --dangerously-skip-permissions
'''

# macOS Quick Action (Service) — принимает папки, запускает лаунчер в Terminal.
WFLOW_INFO = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>NSServices</key><array><dict>
    <key>NSMenuItem</key><dict><key>default</key><string>{LABEL}</string></dict>
    <key>NSMessage</key><string>runWorkflowAsService</string>
    <key>NSRequiredContext</key><dict><key>NSApplicationIdentifier</key><string>com.apple.finder</string></dict>
    <key>NSSendFileTypes</key><array><string>public.folder</string></array>
  </dict></array>
</dict></plist>
'''

WFLOW_DOC = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>AMApplicationBuild</key><string>521</string>
  <key>AMApplicationVersion</key><string>2.10</string>
  <key>AMDocumentVersion</key><string>2</string>
  <key>actions</key><array><dict>
    <key>action</key><dict>
      <key>AMAccepts</key><dict><key>Container</key><string>List</string><key>Optional</key><true/><key>Types</key><array><string>com.apple.cocoa.string</string></array></dict>
      <key>AMActionVersion</key><string>2.0.3</string>
      <key>AMProvides</key><dict><key>Container</key><string>List</string><key>Types</key><array><string>com.apple.cocoa.string</string></array></dict>
      <key>ActionBundlePath</key><string>/System/Library/Automator/Run Shell Script.action</string>
      <key>ActionName</key><string>Run Shell Script</string>
      <key>ActionParameters</key><dict>
        <key>COMMAND_STRING</key><string>open -a Terminal "$HOME/.clirelay-agent/launch.command"
# передаём выбранную папку запущенному лаунчеру через временный файл
for f in "$@"; do echo "$f" &gt; "$HOME/.clirelay-agent/.last-folder"; done</string>
        <key>CheckedForUserDefaultShell</key><true/>
        <key>inputMethod</key><integer>1</integer>
        <key>shell</key><string>/bin/bash</string>
        <key>source</key><string></string>
      </dict>
      <key>BundleIdentifier</key><string>com.apple.Automator.RunShellScript</string>
      <key>Class Name</key><string>RunShellScriptAction</string>
      <key>InputUUID</key><string>00000000-0000-0000-0000-000000000001</string>
      <key>UUID</key><string>00000000-0000-0000-0000-000000000002</string>
      <key>CanShowSelectedItemsWhenRun</key><false/>
      <key>CanShowWhenRun</key><true/>
    </dict>
    <key>isViewVisible</key><true/>
  </dict></array>
  <key>connectors</key><dict/>
  <key>workflowMetaData</key><dict>
    <key>serviceInputTypeIdentifier</key><string>com.apple.Automator.fileSystemObject.folder</string>
    <key>serviceOutputTypeIdentifier</key><string>com.apple.Automator.nothing</string>
    <key>serviceApplicationBundleID</key><string>com.apple.finder</string>
    <key>applicationBundleIDsByPath</key><dict/>
    <key>workflowTypeIdentifier</key><string>com.apple.Automator.servicesMenu</string>
  </dict>
</dict></plist>
'''

# ───────────────────────── помощники ─────────────────────────

def ask(prompt, default=None):
    s = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return s or (default or "")

def fetch_models(base, key):
    """-> (список id, {id: реальное контекстное окно})."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        ids, wins = [], {}
        for m in d.get("data", []):
            ids.append(m["id"])
            w = m.get("context_window") or m.get("max_context_window") or m.get("max_input_tokens")
            if w: wins[m["id"]] = int(w)
        return ids, wins
    except Exception:
        return [], {}

def pick_default_model(models):
    for pref in ("gemini-3.8-flash-medium", "gemini-3.8-flash-low"):
        if pref in models: return pref
    for m in models:
        if "flash" in m and "gemini" in m: return m
    return models[0] if models else "gemini-3.8-flash-medium"

def model_slots(default_model, models):
    """Три слота пикатора: Opus=high, Sonnet=default, Haiku=low (по варианту суффикса)."""
    base = default_model
    for suf in ("-high", "-medium", "-low", "-tiered", "-extra-low"):
        if base.endswith(suf): base = base[: -len(suf)]; break
    def var(suf, fb):
        c = base + suf
        return c if (not models or c in models) else fb
    return var("-high", default_model), default_model, var("-low", default_model)

def pick_extra(models, used):
    """Доп. именованный пункт (ANTHROPIC_CUSTOM_MODEL_OPTION): предпочитаем pro-модель."""
    for m in (models or []):
        if m not in used and "pro" in m: return m
    for m in (models or []):
        if m not in used: return m
    return None

def build_settings(default_model, models):
    """Переопределяем встроенные слоты пикера на модели шлюза через env —
    в /model они показываются ПО ИМЕНИ (gemini-...), переключаются в сессии.
    (способ через ANTHROPIC_DEFAULT_*_MODEL + ANTHROPIC_CUSTOM_MODEL_OPTION)."""
    high, med, low = model_slots(default_model, models)
    env = {
        "ANTHROPIC_DEFAULT_OPUS_MODEL":   high,   # слот Opus   -> high
        "ANTHROPIC_DEFAULT_SONNET_MODEL": med,    # слот Sonnet -> default
        "ANTHROPIC_DEFAULT_HAIKU_MODEL":  low,    # слот Haiku  -> low
    }
    extra = pick_extra(models, {high, med, low})
    if extra:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = extra
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = extra
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"] = extra
    fb = [med] + ([low] if low and low != med else [])   # сперва medium, потом low
    s = {
        "env": env,
        "model": "sonnet",                 # дефолт -> слот Sonnet (= default model, medium)
        "fallbackModel": fb,               # при проблемах основной — на medium, затем low
        "skipDangerousModePermissionPrompt": True,
    }
    return s, (high, med, low, extra)

def build_desktop_import(key, port):
    """Конфиг для импорта в Claude Desktop (у него нет лаунчера/выбора моделей — он
    принимает такой gateway-JSON). Имена ДОЛЖНЫ выглядеть «по-Anthropic» (claude-*),
    поэтому берём claude-* — наш прокси ремапит их в gemini (MODEL_MAP). Уровень
    мышления в Desktop задаётся его собственным /effort (output_config.effort) — прокси
    уважает его так же, как в Claude Code. Ключ/порт подставляются из install-time."""
    return {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": f"http://127.0.0.1:{port}/v1",
        "inferenceCredentialKind": "static",
        "inferenceGatewayApiKey": key,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": [
            {"name": "claude-opus-4-8",   "anthropicFamilyTier": "opus",   "supports1m": True, "isFamilyDefault": True},
            {"name": "claude-sonnet-4-5", "anthropicFamilyTier": "sonnet", "supports1m": True, "isFamilyDefault": True},
            {"name": "claude-haiku-4-5",  "anthropicFamilyTier": "haiku",  "supports1m": True, "isFamilyDefault": True},
        ],
        "modelDiscoveryEnabled": False,
        "modelPrefer1mContext": True,
        "defaultModelEffort": "high",
    }

def write(path: Path, text: str, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable and os.name != "nt":
        os.chmod(path, 0o755)

# ───────────────────────── контекстное меню ─────────────────────────

def install_menu_windows(launcher: Path):
    import winreg
    cmd = f'"{launcher}" "%V"'
    for root in (r"Directory\shell", r"Directory\Background\shell"):
        base = rf"Software\Classes\{root}\CliRelayAgent"
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, base)
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, MENU_LABEL)
        winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, "cmd.exe")
        c = winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\command")
        winreg.SetValueEx(c, None, 0, winreg.REG_SZ, cmd)
    print("  [+] Пункт в контекстном меню добавлен (правый клик по папке / в пустом месте папки).")

def uninstall_menu_windows():
    import winreg
    for root in (r"Directory\shell", r"Directory\Background\shell"):
        base = rf"Software\Classes\{root}\CliRelayAgent"
        for sub in (base + r"\command", base):
            try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except FileNotFoundError: pass

def install_menu_macos():
    svc = Path.home() / "Library" / "Services" / f"{MENU_LABEL}.workflow" / "Contents"
    write(svc / "Info.plist", WFLOW_INFO.replace("{LABEL}", MENU_LABEL))
    write(svc / "document.wflow", WFLOW_DOC)
    print("  [+] Quick Action установлен. Правый клик по папке -> Быстрые действия (Quick Actions) ->", MENU_LABEL)
    print("      Если не появился сразу: System Settings -> Keyboard -> Keyboard Shortcuts -> Services, включить пункт.")

def uninstall_menu_macos():
    import shutil
    p = Path.home() / "Library" / "Services" / f"{MENU_LABEL}.workflow"
    if p.exists(): shutil.rmtree(p, ignore_errors=True)

# ───────────────────────── автозапуск прокси при логине ─────────────────────────
# Нужен, чтобы прокси поднимался сам (особенно для Claude Desktop — у него нет лаунчера,
# а Claude Code поднимает прокси сам при запуске через меню). Windows: ключ HKCU..\Run
# (pythonw -> ensure-proxy.py, идемпотентно, без окна). macOS: LaunchAgent (RunAtLoad +
# KeepAlive — launchd сам стартует при логине и перезапускает, если упал).
AUTOSTART_NAME = "CliRelayProxy"
LAUNCH_AGENT_LABEL = "com.clirelay.proxy"

def _pythonw(pyexe: str) -> str:
    pyw = pyexe.replace("python.exe", "pythonw.exe")
    return pyw if os.path.exists(pyw) else pyexe

def install_autostart_windows(ensure_py: Path, pyexe: str):
    import winreg
    cmd = f'"{_pythonw(pyexe)}" "{ensure_py}"'
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run")
    winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
    print("  [+] Автозапуск прокси при входе в Windows добавлен (ключ Run:", AUTOSTART_NAME + ").")

def uninstall_autostart_windows():
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, AUTOSTART_NAME)
    except (FileNotFoundError, OSError):
        pass

def install_autostart_macos(proxy_py: Path, pyexe: str):
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    err_log = str(HOME / "launchd.err.log")
    plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{pyexe}</string>
    <string>{proxy_py}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{err_log}</string>
</dict>
</plist>
'''
    write(plist_path, plist)
    os.system(f'launchctl unload "{plist_path}" 2>/dev/null')
    os.system(f'launchctl load -w "{plist_path}"')
    print("  [+] Автозапуск прокси (LaunchAgent) установлен и загружен:", plist_path)

def uninstall_autostart_macos():
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    if plist_path.exists():
        os.system(f'launchctl unload "{plist_path}" 2>/dev/null')
        try: plist_path.unlink()
        except Exception: pass

def start_proxy_now(pyexe: str):
    """Поднять прокси сразу после установки (чтобы работал без перелогина)."""
    import subprocess
    ensure_py = str(HOME / "ensure-proxy.py")
    try:
        if os.name == "nt":
            subprocess.Popen([_pythonw(pyexe), ensure_py], creationflags=0x08000000)  # CREATE_NO_WINDOW
        else:
            subprocess.Popen([pyexe, ensure_py], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass

# ───────────────────────── основной сценарий ─────────────────────────

def do_install():
    print("=== Установка CliRelay Agent (Claude Code через шлюз) ===\n")
    if os.system("claude --version >%s 2>&1" % ("NUL" if os.name=="nt" else "/dev/null")) != 0:
        print("  [!] Claude Code (`claude`) не найден в PATH. Установи его и запусти снова.")
        print("      npm i -g @anthropic-ai/claude-code  (или brew install --cask claude-code)")
        return 1

    url = ask("URL шлюза (напр. https://your-gateway.example.com)")
    key = ask("API-ключ (sk-...)")
    if not url or not key:
        print("  [!] Нужны и URL, и ключ."); return 1

    u = urlparse(url if "://" in url else "https://" + url)
    use_https = (u.scheme != "http")
    host = u.hostname
    port = u.port or (443 if use_https else 80)
    if not host:
        print("  [!] Не разобрал URL."); return 1
    base = f"{'https' if use_https else 'http'}://{host}:{port}"

    print("\n  Получаю список моделей...")
    models, windows = fetch_models(base, key)
    model = pick_default_model(models)
    if models:
        print(f"  Найдено моделей: {len(models)}. Модель по умолчанию: {model}")
        m = ask("Другая модель по умолчанию? (Enter — оставить)", model)
        model = m
    else:
        print(f"  Список не получен (проверь URL/ключ позже). Беру {model}.")

    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "claude-home").mkdir(exist_ok=True)
    log_path = str(HOME / "relay-proxy.log")

    # settings.json: слоты пикера -> модели шлюза по именам (переключение /model в сессии)
    settings, (m_high, m_med, m_low, m_extra) = build_settings(model, models)
    write(HOME / "claude-home" / "settings.json", json.dumps(settings, indent=2, ensure_ascii=False))
    # small/fast слот (заголовки/служебные вызовы) — самая дешёвая 'lite'-модель, если есть
    # (так делает нативный agy: служебное гонит на flash-lite), иначе low-тир.
    small = next((x for x in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"] if x in models),
                 next((x for x in sorted(models) if "lite" in x.lower() and "image" not in x.lower()), m_low))

    proxy_src = (PROXY_PY
                 .replace("__HOST__", host)
                 .replace("__PORT_UP__", str(port))
                 .replace("__HTTPS__", "True" if use_https else "False")
                 .replace("__PORT__", str(PROXY_PORT))
                 .replace("__LOG__", log_path))
    write(HOME / "relay-proxy.py", proxy_src)
    write(HOME / "ensure-proxy.py", ENSURE_PY.replace("__PORT__", str(PROXY_PORT)))
    write(HOME / "config.json", json.dumps(
        {"url": base, "model": model, "proxy_port": PROXY_PORT}, indent=2, ensure_ascii=False))
    # конфиг для импорта в Claude Desktop (claude-* имена -> ремап в gemini нашим прокси)
    desktop_import = HOME / "claude-desktop-import.json"
    write(desktop_import, json.dumps(build_desktop_import(key, PROXY_PORT), indent=2, ensure_ascii=False))

    raw_ctx = int(windows.get(model) or 200000)
    # Оставляем запас под системные промпты и инструменты (~50k), чтобы не упираться в жесткий лимит шлюза
    ctx_val = min(raw_ctx, 1000000) if raw_ctx >= 1000000 else raw_ctx
    compact_val = int(ctx_val * 0.88)   # авто-компакт срабатывает заранее (на 88% контекста)
    ctx = str(ctx_val)
    compact_str = str(compact_val)
    pyexe = sys.executable
    if os.name == "nt":
        launcher = HOME / "launch.cmd"
        write(launcher, (LAUNCH_CMD
                         .replace("{PYTHON}", pyexe)
                         .replace("{PORT}", str(PROXY_PORT))
                         .replace("{KEY}", key)
                         .replace("{SMALL}", small)
                         .replace("{CTX}", ctx)
                         .replace("{COMPACT}", compact_str)))
        install_menu_windows(launcher)
        install_autostart_windows(HOME / "ensure-proxy.py", pyexe)
    else:
        launcher = HOME / "launch.command"
        write(launcher, (LAUNCH_SH
                         .replace("{PYTHON}", pyexe)
                         .replace("{PORT}", str(PROXY_PORT))
                         .replace("{KEY}", key)
                         .replace("{SMALL}", small)
                         .replace("{CTX}", ctx)
                         .replace("{COMPACT}", compact_str)), executable=True)
        try: os.chmod(HOME / "config.json", 0o600)
        except Exception: pass
        install_menu_macos()
        install_autostart_macos(HOME / "relay-proxy.py", pyexe)

    start_proxy_now(pyexe)   # поднять прокси сразу (на macOS уже поднял launchd)

    print("\n=== Готово ===")
    print(f"  Профиль/файлы: {HOME}")
    print(f"  Модель: {model}  |  шлюз: {base}")
    print(f"  Лог ошибок шлюза (тихий прокси пишет сюда): {HOME / 'relay-proxy.log'}")
    print("  Правый клик по папке ->", MENU_LABEL, "-> Claude Code стартует в ней с --dangerously-skip-permissions.")
    print("  Прокси работает ТИХО, без окна, один на все сессии (сколько бы папок ни открыл).")
    print("  Автозапуск прокси при входе настроен (Windows: ключ Run; macOS: LaunchAgent) —")
    print("    он поднимается сам, в т.ч. для Claude Desktop (у него нет лаунчера). Запущен уже сейчас.")
    print(f"  Claude Desktop: импортируй {desktop_import}")
    print("    (Desktop -> настройки стороннего gateway -> вставить этот JSON; уровень мышления — его /effort).")
    print("\n  Модели в пикере /model (по именам, переключение прямо в сессии):")
    print(f"      Opus   -> {m_high}")
    print(f"      Sonnet -> {m_med}   (дефолт)")
    print(f"      Haiku  -> {m_low}")
    if m_extra: print(f"      +доп.  -> {m_extra}")
    print("      Любая другая: /model -> Custom -> впиши id из", base + "/v1/models")
    print("  Поменять привязки:", HOME / "claude-home" / "settings.json", "(блок env: ANTHROPIC_DEFAULT_*_MODEL).")
    print("  Удалить всё:  python setup-agent.py --uninstall")
    return 0

def do_uninstall():
    import shutil
    if os.name == "nt":
        uninstall_menu_windows(); uninstall_autostart_windows()
    else:
        uninstall_menu_macos(); uninstall_autostart_macos()
    if HOME.exists(): shutil.rmtree(HOME, ignore_errors=True)
    print("CliRelay Agent удалён (меню + автозапуск + файлы). Профиль Claude Code тоже удалён.")
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    sys.exit(do_uninstall() if a.uninstall else do_install())
