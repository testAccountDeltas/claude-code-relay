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

def log(s):
    try:
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

def _conn():
    cls = http.client.HTTPSConnection if USE_HTTPS else http.client.HTTPConnection
    return cls(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)

# Пул keep-alive соединений: переиспользуем открытые TCP+TLS вместо нового на
# каждый запрос. Обрыв (connect timeout) случается ИМЕННО при установке нового
# коннекта, когда сервер на миг теряет SYN под нагрузкой CPU; меньше новых
# коннектов -> меньше шансов словить обрыв. Ретрай ниже страхует остальное.
_pool = []; _pool_lock = threading.Lock(); _POOL_MAX = 8
def _acquire():
    with _pool_lock:
        if _pool: return _pool.pop(), True
    return _conn(), False
def _release(conn, resp, ok):
    if conn is None: return
    if ok and resp is not None and not getattr(resp, "will_close", True):
        with _pool_lock:
            if len(_pool) < _POOL_MAX: _pool.append(conn); return
    try: conn.close()
    except Exception: pass

_req_id = 0

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _h(self):
        global _req_id
        _req_id += 1
        rid = _req_id
        t0 = time.time()
        ts = time.strftime("%H:%M:%S")

        n = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(n) if n else b""
        body, scrubbed = scrub(raw_body)
        meta = summarize_req(body) if "/v1/messages" in self.path else f"bytes={n}"
        log(f"[{ts}] #{rid} >> {self.command} {self.path} (scrub={scrubbed}) {meta}")

        fh = {k: v for k, v in self.headers.items()
              if k.lower() not in ("host","content-length","accept-encoding",
                                    "connection","keep-alive","proxy-connection","te","upgrade")}
        fh["Host"] = UPSTREAM_HOST
        conn = resp = None
        attempt = 0
        while True:
            c, reused = _acquire()
            try:
                c.request(self.command, self.path, body=body, headers=fh)
                resp = c.getresponse()
            except Exception as e:
                try: c.close()
                except Exception: pass
                if reused: continue  # протухшее keep-alive соединение — молча переоткрываем
                attempt += 1
                dur = time.time() - t0
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} !! conn err after {dur:.1f}s (att {attempt}): {e!r}")
                if attempt < 3:
                    time.sleep(2); continue  # блип связи — повторяем сами, клиент не увидит ошибку
                try: self.send_error(502, "upstream error")
                except Exception: pass
                return
            if resp.status in (429, 503) and attempt < 3:
                try:
                    rb = resp.read()
                    log(f"[{time.strftime('%H:%M:%S')}] #{rid} -- attempt {attempt+1} -> {resp.status} {resp.reason} ({rb[:120].decode(errors='replace')}), retry in 3s")
                except Exception: pass
                _release(c, resp, ok=True); resp = None; attempt += 1; time.sleep(3); continue
            conn = c
            break

        ctype = resp.getheader("content-type") or ""
        if "event-stream" in ctype and resp.status == 200:
            # ПОТОКОВАЯ передача SSE по мере поступления
            self.send_response(200)
            for k, v in resp.getheaders():
                if k.lower() not in HOP: self.send_header(k, v)
            self.send_header("Connection", "close"); self.end_headers()
            self.close_connection = True
            tracker = SSETracker()
            total_bytes = chunks = 0
            stream_err = None
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk: break
                    total_bytes += len(chunk); chunks += 1
                    tracker.feed(chunk)
                    self.wfile.write(chunk); self.wfile.flush()
            except Exception as e:
                stream_err = e
            finally:
                _release(conn, resp, ok=(stream_err is None))  # поток дочитан -> можно переиспользовать
            dur = time.time() - t0
            summary = tracker.summary()
            if stream_err:
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} << SSE BROKEN ({dur:.1f}s, {total_bytes}B, {chunks} chunks): err={stream_err!r} | {summary}")
            else:
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} << SSE 200 OK ({dur:.1f}s, {total_bytes}B, {chunks} chunks): {summary}")
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
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 5000000:
            open(LOG, "w", encoding="utf-8").close()  # не разрастаться бесконечно
    except Exception: pass
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
    small = m_low

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

    print("\n=== Готово ===")
    print(f"  Профиль/файлы: {HOME}")
    print(f"  Модель: {model}  |  шлюз: {base}")
    print(f"  Лог ошибок шлюза (тихий прокси пишет сюда): {HOME / 'relay-proxy.log'}")
    print("  Правый клик по папке ->", MENU_LABEL, "-> Claude Code стартует в ней с --dangerously-skip-permissions.")
    print("  Прокси работает ТИХО, без окна, один на все сессии (сколько бы папок ни открыл).")
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
    if os.name == "nt": uninstall_menu_windows()
    else: uninstall_menu_macos()
    if HOME.exists(): shutil.rmtree(HOME, ignore_errors=True)
    print("CliRelay Agent удалён (меню + файлы). Профиль Claude Code тоже удалён.")
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    sys.exit(do_uninstall() if a.uninstall else do_install())
