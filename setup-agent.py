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
# Работает тихо (без окна), один на все сессии. Пишет ТОЛЬКО ошибки в лог.
import http.server, http.client, json, time, sys, os

UPSTREAM_HOST = "__HOST__"
UPSTREAM_PORT = __PORT_UP__
USE_HTTPS     = __HTTPS__
PORT          = __PORT__
LOG           = r"__LOG__"
MARK          = "x-anthropic-billing-header"

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

HOP = ("transfer-encoding", "connection", "content-length", "content-encoding")

def _conn():
    cls = http.client.HTTPSConnection if USE_HTTPS else http.client.HTTPConnection
    return cls(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _h(self):
        n = int(self.headers.get("Content-Length") or 0)
        body, _ = scrub(self.rfile.read(n) if n else b"")
        fh = {k: v for k, v in self.headers.items() if k.lower() not in ("host","content-length","accept-encoding")}
        fh["Host"] = UPSTREAM_HOST
        conn = resp = None
        for attempt in range(1, 4):
            try:
                conn = _conn(); conn.request(self.command, self.path, body=body, headers=fh); resp = conn.getresponse()
            except Exception as e:
                log("%s %s -> conn err: %r" % (time.strftime("%H:%M:%S"), self.path, e))
                try: self.send_error(502, "upstream error")
                except Exception: pass
                try: conn.close()
                except Exception: pass
                return
            if resp.status in (429, 503) and attempt < 3:
                try: resp.read()
                except Exception: pass
                conn.close(); time.sleep(3); continue
            break
        ctype = resp.getheader("content-type") or ""
        if "event-stream" in ctype and resp.status == 200:
            # ПОТОКОВАЯ передача SSE по мере поступления (иначе длинная генерация -> таймаут)
            self.send_response(200)
            for k, v in resp.getheaders():
                if k.lower() not in HOP: self.send_header(k, v)
            self.send_header("Connection", "close"); self.end_headers()
            self.close_connection = True
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk: break
                    self.wfile.write(chunk); self.wfile.flush()
            except Exception as e:
                log("%s stream err: %r" % (time.strftime("%H:%M:%S"), e))
            finally:
                try: conn.close()
                except Exception: pass
        else:
            try: rb = resp.read()
            except Exception as e:
                log("%s read err: %r" % (time.strftime("%H:%M:%S"), e)); rb = b""
            if resp.status != 200:
                log("%s %s -> %s %s : %s" % (time.strftime("%H:%M:%S"), self.path, resp.status, resp.reason, rb[:180].decode(errors="replace")))
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP: self.send_header(k, v)
            self.send_header("Content-Length", str(len(rb))); self.end_headers()
            try: self.wfile.write(rb)
            except Exception: pass
            try: conn.close()
            except Exception: pass
    do_POST = _h; do_GET = _h; do_PUT = _h
    def log_message(self, *a): pass

class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        if sys.exc_info()[0] in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError): return
        super().handle_error(request, client_address)

if __name__ == "__main__":
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 500000:
            open(LOG, "w", encoding="utf-8").close()  # не разрастаться бесконечно
    except Exception: pass
    log("=== proxy started %s  (->%s://%s:%s)  пишутся только ошибки ===" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), "https" if USE_HTTPS else "http", UPSTREAM_HOST, UPSTREAM_PORT))
    try:
        Srv(("127.0.0.1", PORT), H).serve_forever()
    except OSError as e:
        log("=== proxy НЕ стартовал (порт занят? %s) ===" % e)  # уже запущен другой экземпляр — это норм
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
set "CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192"
set "CLAUDE_CODE_MAX_CONTEXT_TOKENS={CTX}"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
set "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1"
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
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
export CLAUDE_CODE_MAX_CONTEXT_TOKENS={CTX}
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1
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
    s = {
        "env": env,
        "model": "sonnet",                 # дефолт -> слот Sonnet (= default model)
        "fallbackModel": [low],            # запасная, если основная недоступна
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

    ctx = str(windows.get(model) or 200000)   # реальное окно модели из /v1/models
    pyexe = sys.executable
    if os.name == "nt":
        launcher = HOME / "launch.cmd"
        write(launcher, LAUNCH_CMD.replace("{PYTHON}", pyexe).replace("{PORT}", str(PROXY_PORT)).replace("{KEY}", key).replace("{SMALL}", small).replace("{CTX}", ctx))
        install_menu_windows(launcher)
    else:
        launcher = HOME / "launch.command"
        write(launcher, LAUNCH_SH.replace("{PYTHON}", pyexe).replace("{PORT}", str(PROXY_PORT)).replace("{KEY}", key).replace("{SMALL}", small).replace("{CTX}", ctx), executable=True)
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
