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
import http.server, http.client, json, time, sys, os, threading, codecs, random

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

# Gemini плохо справляется со специализированными multi-field тулами (Grep(pattern,path,...),
# Glob(pattern,path)) — путает value с description/query, роняя InputValidationError
# ("required parameter pattern is missing"). Подтверждено на том же Gemini в опенкоде
# (deny grep/glob -> 3/3 без ошибок, см. memory opencode-tool-call-errors) и живьём здесь.
# Режем их из tools КАЖДЫЙ раз безусловно — модель уходит на один свободный Bash-тул
# (single command string), с которым Gemini работает надёжно. Безусловно и детерминированно
# -> не бьёт по prefix-кэшу (в отличие от условных nudge, см. _inject_user_nudge выше).
DROP_SEARCH_TOOLS = {"Grep", "Glob"}
if os.name == "nt":
    SEARCH_HINT = ("The Grep and Glob tools are not available. To search file contents or find files, "
                   "use the Bash tool with a single PowerShell command, e.g. "
                   "Select-String -Path 'dir\\*.ext' -Pattern 'text' -SimpleMatch -Recurse  or  "
                   "Get-ChildItem -Recurse -Filter '*.ext'. Never call a search tool without all required "
                   "parameters. IMPORTANT: this runs through a Bash shell wrapping powershell -Command "
                   "'...' — if the PowerShell command contains a $ (variables like $_, $false, "
                   "$($_.Path)), wrap the ENTIRE -Command argument in single quotes, not double quotes, "
                   "otherwise Bash expands the $ before PowerShell ever sees it (e.g. $_.Path silently "
                   "becomes garbage, -CaseSensitive:$false becomes -CaseSensitive: with a missing argument). "
                   "If searching for or printing any non-ASCII text (Cyrillic, etc.), start the command with "
                   "$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new(); — otherwise "
                   "output piped back through Bash comes out garbled and needs a retry.")
else:
    SEARCH_HINT = ("The Grep and Glob tools are not available. To search file contents or find files, "
                   "use the Bash tool with a single shell command, e.g. grep -rn \"pattern\" path  or  "
                   "find . -name '*.ext'  (rg if available). Never call a search tool without all required "
                   "parameters.")
def strip_search_tools(body):
    try:
        j = json.loads(body)
        t = j.get("tools")
        if not isinstance(t, list): return body, False
        new = [x for x in t if not (isinstance(x, dict) and x.get("name") in DROP_SEARCH_TOOLS)]
        if len(new) == len(t): return body, False
        j["tools"] = new
        sysv = j.get("system")
        if isinstance(sysv, list): j["system"] = sysv + [{"type": "text", "text": SEARCH_HINT}]
        elif isinstance(sysv, str): j["system"] = sysv + "\n\n" + SEARCH_HINT
        else: j["system"] = SEARCH_HINT
        return json.dumps(j).encode("utf-8"), True
    except Exception:
        return body, False

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
# если effort нет. Цифры — нативная шкала agy.EXE (effortToThinkingLevel, agyCli/docs/
# 17_exact_defaults...md): minimal=1024, low=2048, medium=8192, high=16384, xhigh=32768,
# max=65536. low=2048 совпадает с нижней границей "отравленной зоны" < 2048 у Gemini 3.x
# (Antigravity Manager, changelog v4.7.5) — не случайность, подтверждает цифры.
EFFORT_MAP    = {"low": "(2048)", "medium": "(8192)", "high": "(16384)", "xhigh": "(32768)", "max": "(65536)", "ultracode": "(65536)"}
THINK_BY_TIER = [("-high", "(16384)"), ("-medium", "(8192)"), ("-low", "(2048)")]
THINK_FORCE   = ""        # "" = по effort/тиру; иначе форс всем, напр. "(8192)"
THINK_DEFAULT = "(8192)"  # ни effort, ни тир не распознаны
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

# Обрезка огромных СТАРЫХ tool_result по центру — по мотивам agy.EXE OutputTruncationDecorator
# (agyCli/docs/17_exact_defaults...md: OutputByteLimit=65536 байт, маркер "truncated from the
# middle", полный вывод сохраняется на диск). У agy это работает безопасно, потому что ЕГО
# Read-тул режет вывод ДО того, как модель его увидит, и модель может дочитать по offset;
# у нас такого канала переполучения нет — поэтому режем ТОЛЬКО messages[:-1] (всё, кроме самого
# свежего хода), никогда не трогая tool_result, на который модель прямо сейчас отвечает. Старые
# результаты — чистая context-гигиена, свежие — неприкосновенны. Чистая функция от текста ->
# детерминирована между повторами одного и того же сообщения -> не бьёт по prefix-кэшу.
TOOL_OUTPUT_CHAR_LIMIT = 65536  # agy считает байтами; символы — грубый, но безопасный аналог
def _truncate_middle(text):
    n = len(text)
    if n <= TOOL_OUTPUT_CHAR_LIMIT: return text, False
    half = TOOL_OUTPUT_CHAR_LIMIT // 2
    marker = f"\n<truncated {n - TOOL_OUTPUT_CHAR_LIMIT} of {n} chars from the middle of the output>\n"
    return text[:half] + marker + text[-half:], True
def _shrink_old_tool_results(body):
    try:
        j = json.loads(body)
        msgs = j.get("messages")
        if not isinstance(msgs, list) or len(msgs) < 2: return body, 0
        shrunk = 0
        for msg in msgs[:-1]:  # последний ход не трогаем — модель может его использовать прямо сейчас
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list): continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result": continue
                bc = block.get("content")
                if isinstance(bc, str):
                    new, did = _truncate_middle(bc)
                    if did: block["content"] = new; shrunk += 1
                elif isinstance(bc, list):
                    for part in bc:
                        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                            new, did = _truncate_middle(part["text"])
                            if did: part["text"] = new; shrunk += 1
        return (json.dumps(j).encode("utf-8"), shrunk) if shrunk else (body, 0)
    except Exception:
        return body, 0

_NUDGE = ("IMPORTANT: Produce a visible response now — either a textual answer or a tool "
          "call. Do NOT end your turn with only internal thinking and no output.")

# Детектор read-loop: Claude Code режет Read на ~50-100 строк (в отличие от agy, где
# view_file отдаёт до 800 строк за раз), и после десятка однотипных ходов tool_use:Read ->
# tool_result модель впадает в авторегрессионный транс — thinking=False, text=False, просто
# читает следующий кусок. У agy это лечит клиентский loop-guard оркестратора; здесь его нет,
# поэтому эмулируем то же самое на уровне прокси: считаем streak подряд-идущих "молчаливых"
# Read-ходов ТОЛЬКО в основном потоке (request-class=main, суб-агентов не трогаем) и один раз
# впрыскиваем подсказку остановиться.
LOOP_STREAK_THRESHOLD = 10
_loop_lock = threading.Lock()
_loop_streak = 0
def _loop_record(is_main, tool_names, saw_text, saw_thinking):
    global _loop_streak
    if not is_main: return
    with _loop_lock:
        if tool_names == ["Read"] and not saw_text and not saw_thinking:
            _loop_streak += 1
        else:
            _loop_streak = 0
def _loop_check_and_reset(is_main):
    global _loop_streak
    if not is_main: return False
    with _loop_lock:
        if _loop_streak >= LOOP_STREAK_THRESHOLD:
            _loop_streak = 0
            return True
        return False

_LOOP_NUDGE = ("Notice from the proxy: you have just made many consecutive Read calls on "
               "small line ranges with no thinking or text in between — this looks like an "
               "unproductive read-loop over a large file. Stop reading slices. Use Grep/Bash "
               "to jump straight to the relevant symbol/section, or read the file in much "
               "larger ranges (hundreds of lines) at once, or proceed to answer/edit with what "
               "you already have.")
# Нудж вставляется синтетическим user-ходом в ХВОСТ messages, а не правкой system.
# system — вершина prompt-кэша (byte-stable префикс, см. clirelay-read-loop-guard память:
# 98-99% cache hit всю сессию); дёргая его от случая к случаю, мы сами бьём по кэшу ровно на
# том ходе, где пытаемся быстро поправить модель. Хвост messages и так меняется каждый ход
# (туда приходят новые tool_result) — добавление ещё одного сообщения туда кэш не трогает,
# как у agy (newFallbackUserInputStep — синтетический user-ход, не правка системного промпта).
def _inject_user_nudge(body, text):
    try:
        j = json.loads(body)
        msgs = j.get("messages")
        if isinstance(msgs, list):
            j["messages"] = msgs + [{"role": "user", "content": text}]
            return json.dumps(j).encode("utf-8")
    except Exception:
        pass
    return body
def _loop_nudge(body):
    return _inject_user_nudge(body, _LOOP_NUDGE)

# Детектор повтора/осцилляции вызовов тулов (см. logagy.txt, docs/17_exact_defaults_...md
# в agyCli — agy.EXE's detectLoop[T]): отдельно от read-streak выше, этот ловит ТОЧНЫЙ повтор
# одного и того же вызова (tool_name + канонические аргументы) — например, повторный Bash
# с идентичной падающей командой, или Read/Edit на одном и том же диапазоне. Пороги по
# мотивам agy: k=1 (один и тот же вызов) x3 подряд, k=2 (осцилляция A->B->A->B) x2 цикла.
# Эскейп-тег как у agy: модель может явно пометить намеренный повтор, тогда не нуджим.
TOOLCALL_HIST_MAX = 12
_OSC_IGNORE_TAG = "[ignoring loop detection]"
_OSC_NUDGE = ("Your output is flagged for looping content (the exact same tool call, or an "
              "A/B oscillation, repeated). If this really was intentional, add the tag "
              + _OSC_IGNORE_TAG + " to your output; otherwise stop and try a fundamentally "
              "different approach.")
_toolcall_hist = []  # делит _loop_lock с read-streak выше
def _canon_tool_key(name, args_obj):
    try:
        return (name or "") + ":" + json.dumps(args_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return (name or "") + ":" + repr(args_obj)
def _detect_exact_loop(history):
    # k=1: тот же вызов x3 подряд; k=2/k=3: осцилляция из 2/3 звеньев (A->B->A->B / A->B->C->A->B->C) x2 цикла
    n = len(history)
    for period, reps in ((1, 3), (2, 2), (3, 2)):
        need = period * reps
        if n < need: continue
        window = history[-need:]
        pattern = window[:period]
        if period > 1 and len(set(pattern)) < period: continue  # вырожденный паттерн -> это уже меньший period
        if all(window[i] == pattern[i % period] for i in range(need)):
            return True
    return False
def _toolcall_record(is_main, calls, override):
    # Один элемент истории = один ХОД, не один вызов тула — иначе параллельные тулы за ход
    # (Read(a.py)+Read(b.py) одним ответом) размазываются по отдельным "звеньям" и забивают
    # окно истории быстрее, чем кажется (12 звеньев при 2 тулах/ход = 6 ходов, а не 12).
    global _toolcall_hist
    if not is_main: return
    with _loop_lock:
        if override:
            _toolcall_hist = []  # эскейп-тег встречен в тексте -> модель явно подтвердила намеренный повтор
            return
        if calls:
            _toolcall_hist.append(tuple(sorted(calls)))
        if len(_toolcall_hist) > TOOLCALL_HIST_MAX: del _toolcall_hist[:-TOOLCALL_HIST_MAX]
def _toolcall_check_and_reset(is_main):
    if not is_main: return False
    with _loop_lock:
        if _detect_exact_loop(_toolcall_hist):
            _toolcall_hist.clear()
            return True
        return False

def _nudge(body):
    return _inject_user_nudge(body, _NUDGE)

# agy ModelOutputRetryConfig.ForbidToolUse: на САМОЙ последней попытке пустого хода тулы
# убираются из запроса целиком — у модели физически нет способа ответить ещё одним тихим
# tool_use, она обязана выдать текст. Страхует EMPTY_FALLBACK_TEXT ниже, а не подменяет его.
def _strip_tools(body):
    try:
        j = json.loads(body)
        j.pop("tools", None)
        j.pop("tool_choice", None)
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
        eff = (j.get("output_config") or {}).get("effort")
        if eff: th_s += f" effort={eff}"
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
_TTFT_TIMEOUT    = 60   # таймаут на ПЕРВЫЙ байт тела стрима: мёртвый воркер шлюза виснет молча,
                        # незачем ждать по нему все 600с — падаем быстро и честно (без звона тишины)
_READ_TIMEOUT    = 600  # после первого байта — длинный таймаут на чтение остального SSE-стрима
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

# Экспоненциальный backoff с джиттером вместо фиксированных sleep(0.5)/sleep(3) — по мотивам
# agy.EXE ModelAPIRetryConfig (agyCli/docs/17_exact_defaults...md): InitialSleepDurationMs=1000,
# ExponentialMultiplier=2.0, JitterRange=0.4 (±40%), MaxSleepDurationMs=300000 (5 мин потолок).
def _backoff_sleep(attempt):
    base = min(1.0 * (2.0 ** max(attempt - 1, 0)), 300.0)
    jitter = base * 0.4
    return max(0.05, base + random.uniform(-jitter, jitter))

def _open_upstream(command, path, body, fh, rid, t0):
    """Открывает апстрим с ретраями (блип связи + 429/503). (conn, resp) или (None, None)."""
    attempt = 0
    while True:
        c, reused = _acquire()
        try:
            if not reused:
                c.connect()                        # форсим connect с коротким таймаутом (_CONNECT_TIMEOUT)
            c.sock.settimeout(_TTFT_TIMEOUT)  # короткий таймаут до первого байта тела; _stream_one
                                               # сам продлит его до _READ_TIMEOUT после первого чанка.
                                               # Ставим и на реюзнутых соединениях — иначе унаследуют
                                               # растянутый _READ_TIMEOUT от предыдущего SSE-стрима.
            c.request(command, path, body=body, headers=fh)
            resp = c.getresponse()
        except Exception as e:
            try: c.close()
            except Exception: pass
            if reused: continue
            attempt += 1
            dur = time.time() - t0
            log(f"[{time.strftime('%H:%M:%S')}] #{rid} !! conn err after {dur:.1f}s (att {attempt}): {e!r}")
            if attempt < 5: time.sleep(_backoff_sleep(attempt)); continue
            return None, None
        if resp.status in (429, 503) and attempt < 4:  # agy ModelAPIRetryConfig.MaxRetries=4
            ok_read = True
            try:
                rb = resp.read()
                log(f"[{time.strftime('%H:%M:%S')}] #{rid} -- attempt {attempt+1} -> {resp.status} {resp.reason} ({rb[:120].decode(errors='replace')}), retry in backoff")
            except Exception:
                ok_read = False  # тело не дочитано -> нельзя реюзать соединение как чистое
            _release(c, resp, ok=ok_read); attempt += 1; time.sleep(_backoff_sleep(attempt)); continue
        return c, resp

def _stream_one(conn, resp, wfile, offset, suppress_start):
    """Стримит один ответ ЖИВЬЁМ, придерживая терминальные кадры; при offset>0
    переиндексирует content_block_*; при suppress_start глушит message_start."""
    st = {"saw_text": False, "saw_tool": False, "saw_thinking": False,
          "stop_reason": None, "max_index": -1, "held": [], "usage": None, "err": None,
          "tool_names": [], "tool_calls": [], "text_buf": ""}
    buf = ""
    got_first_byte = False
    _tool_blocks = {}  # idx -> {"name": str, "json": str}  — накопление input_json_delta для детектора повторов
    # Инкрементальный декодер: resp.read(8192) режет байты без учёта границ UTF-8 символа
    # (кириллица = 2 байта и т.п.). decode() на каждом куске ПО ОТДЕЛЬНОСТИ портит символ,
    # разрезанный между кусками, в "�" — инкрементальный декодер сам буферизует неполный хвост.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            chunk = resp.read(8192)
            if not got_first_byte and chunk:
                got_first_byte = True
                try: conn.sock.settimeout(_READ_TIMEOUT)  # первый байт пришёл -> снимаем короткий TTFT-таймаут
                except Exception: pass
            if not chunk:
                buf += decoder.decode(b"", final=True)
                break
            buf += decoder.decode(chunk)
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
                        cb = obj.get("content_block") or {}
                        bt = cb.get("type")
                        if bt == "tool_use":
                            st["saw_tool"] = True
                            st["tool_names"].append(cb.get("name"))
                            _tool_blocks[idx] = {"name": cb.get("name"), "json": ""}
                        elif bt == "thinking": st["saw_thinking"] = True
                    elif typ == "content_block_delta":
                        d = obj.get("delta") or {}
                        dtype = d.get("type")
                        if dtype == "text_delta":
                            t = d.get("text") or ""
                            if t.strip(): st["saw_text"] = True
                            if len(st["text_buf"]) < 4096: st["text_buf"] += t
                        elif dtype == "thinking_delta": st["saw_thinking"] = True
                        elif dtype == "input_json_delta" and idx in _tool_blocks:
                            _tool_blocks[idx]["json"] += d.get("partial_json") or ""
                    elif typ == "content_block_stop" and idx in _tool_blocks:
                        tb = _tool_blocks.pop(idx)
                        try: args_obj = json.loads(tb["json"]) if tb["json"] else {}
                        except Exception: args_obj = tb["json"]  # невалидный JSON -> хешируем как сырую строку
                        st["tool_calls"].append(_canon_tool_key(tb["name"], args_obj))
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

_req_id_lock = threading.Lock()
_req_id = 0

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _h(self):
        global _req_id
        with _req_id_lock:
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
        body, shrunk = _shrink_old_tool_results(body)
        body, stools = strip_search_tools(body)
        meta = summarize_req(body) if "/v1/messages" in self.path else f"bytes={n}"
        rm = f" remap={remapped[0]}->{remapped[1]}" if remapped else ""
        tc = f" think={think[1]}[{think[2]}]" if think else ""
        # Заголовки-подсказки шлюзу (CLAUDE_CODE_GATEWAY_HINT_HEADERS=1): различают суб-агента
        # и родительскую сессию в логе — запросы иначе выглядят одинаково.
        hint = " ".join(f"{k[14:]}={v}" for k, v in self.headers.items()
                         if k.lower() in ("x-claude-code-agent-type", "x-claude-code-request-class"))
        hs = f" [{hint}]" if hint else ""
        req_class = self.headers.get("x-claude-code-request-class")
        is_main = req_class in (None, "main")  # без заголовка (флаг выключен/другой клиент) считаем основным потоком
        if is_main and _loop_check_and_reset(is_main):
            body = _loop_nudge(body)
            log(f"[{ts}] #{rid} .. read-loop streak >= {LOOP_STREAK_THRESHOLD} -> injecting stop-nudge")
        if is_main and _toolcall_check_and_reset(is_main):
            body = _inject_user_nudge(body, _OSC_NUDGE)
            log(f"[{ts}] #{rid} .. exact tool-call repeat/oscillation -> injecting loop-nudge")
        sk = f" shrunk={shrunk}" if shrunk else ""
        stk = " stools=True" if stools else ""
        log(f"[{ts}] #{rid} >> {self.command} {self.path} (scrub={scrubbed}){rm}{tc} {meta}{hs}{sk}{stk}")

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
            offset, suppress, eretry, next_idx = 0, False, 0, 0
            final_st = None
            while True:
                st = _stream_one(cur_conn, cur_resp, self.wfile, offset, suppress)
                next_idx = max(next_idx, st["max_index"] + 1)  # сквозной счётчик индексов блоков через все сплайсы
                _release(cur_conn, cur_resp, ok=(st["err"] is None))
                empty = (not st["saw_text"] and not st["saw_tool"] and st["stop_reason"] == "end_turn")
                if st["err"] is None and empty and eretry < EMPTY_RETRY_MAX:
                    eretry += 1
                    log(f"[{time.strftime('%H:%M:%S')}] #{rid} .. EMPTY turn -> splice-retry {eretry}/{EMPTY_RETRY_MAX}")
                    nb = _continue(body) if eretry < EMPTY_RETRY_MAX else _strip_tools(_continue(_nudge(body)))
                    cur_conn, cur_resp = _open_upstream(self.command, self.path, nb, fh, rid, t0)
                    if cur_resp is None or "event-stream" not in (cur_resp.getheader("content-type") or ""):
                        if cur_resp is not None: _release(cur_conn, cur_resp, ok=False)
                        final_st = st; break
                    offset = next_idx; suppress = True; continue
                final_st = st; break

            injected = False
            # Если стрим оборвался без ошибки, но и без message_stop (final_st["held"] пуст),
            # ход не завершён формально -> не выдаём fallback-текст как замену ответу, честно
            # отдаём что есть (клиент увидит оборванное сообщение, но не ложный "ответ").
            if (final_st and final_st["err"] is None and final_st["held"]
                    and not final_st["saw_text"] and not final_st["saw_tool"]):
                try:
                    idx = next_idx
                    _write_frame_obj(self.wfile, {"type":"content_block_start","index":idx,"content_block":{"type":"text","text":""}})
                    _write_frame_obj(self.wfile, {"type":"content_block_delta","index":idx,"delta":{"type":"text_delta","text":EMPTY_FALLBACK_TEXT}})
                    _write_frame_obj(self.wfile, {"type":"content_block_stop","index":idx})
                    injected = True
                except Exception: pass
            try:
                for frame in (final_st["held"] if final_st else []): _write_raw(self.wfile, frame)
            except Exception: pass
            if final_st:
                _loop_record(is_main, final_st.get("tool_names") or [], final_st["saw_text"], final_st["saw_thinking"])
                override = _OSC_IGNORE_TAG in (final_st.get("text_buf") or "")
                _toolcall_record(is_main, final_st.get("tool_calls") or [], override)
                if override: log(f"[{time.strftime('%H:%M:%S')}] #{rid} .. saw {_OSC_IGNORE_TAG!r} -> loop history reset, not nudging")
            dur = time.time() - t0
            log(f"[{time.strftime('%H:%M:%S')}] #{rid} << SSE done ({dur:.1f}s, empty_retries={eretry}, fallback={injected}, "
                f"text={final_st['saw_text'] if final_st else '?'}, tool={final_st['saw_tool'] if final_st else '?'}, "
                f"thinking={final_st['saw_thinking'] if final_st else '?'}, "
                f"stop={final_st['stop_reason'] if final_st else '?'}"
                + (f", tool_names={final_st['tool_names']}" if final_st and final_st.get('tool_names') else "")
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
REM CLAUDE_CODE_SIMPLE отключён: он режет инструмент Agent (суб-агенты) и ещё ~10 других
set "CLAUDE_CONFIG_DIR=%AGENT_HOME%\claude-home"
set "ANTHROPIC_BASE_URL=http://127.0.0.1:{PORT}"
REM ANTHROPIC_AUTH_TOKEN, не API_KEY: последний требует интерактивного подтверждения
REM ("Do you want to use this API key? No (recommended)"), в headless-режиме отвечать некому -> "Not logged in".
set "ANTHROPIC_AUTH_TOKEN={KEY}"
REM Модель задаётся через settings.json (+ modelOverrides) — переключается в сессии: /model
set "ANTHROPIC_SMALL_FAST_MODEL={SMALL}"
set "CLAUDE_CODE_MAX_CONTEXT_TOKENS={CTX}"
set "CLAUDE_CODE_AUTO_COMPACT_WINDOW={COMPACT}"
REM потолок вывода как у нативного agy (~65536) — длинные ответы/мышление не режутся
set "CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000"
set "CLAUDE_CODE_ATTRIBUTION_HEADER=0"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
REM с 2.1.268 TaskCreate/TodoWrite выдаются только распознанным моделям Claude; у gemini-* их нет без этого флага
set "CLAUDE_CODE_ENABLE_TODO_TOOLS=1"
REM x-claude-code-agent-type / x-claude-code-request-class в заголовках -> видно суб-агентов в логе прокси
set "CLAUDE_CODE_GATEWAY_HINT_HEADERS=1"
if not "%~1"=="" cd /d "%~1"
claude --dangerously-skip-permissions
'''

LAUNCH_SH = r'''#!/bin/bash
AGENT_HOME="$HOME/.clirelay-agent"
"{PYTHON}" "$AGENT_HOME/ensure-proxy.py"
# CLAUDE_CODE_SIMPLE отключён: он режет инструмент Agent (суб-агенты) и ещё ~10 других
export CLAUDE_CONFIG_DIR="$AGENT_HOME/claude-home"
export ANTHROPIC_BASE_URL="http://127.0.0.1:{PORT}"
# ANTHROPIC_AUTH_TOKEN, не API_KEY: последний требует интерактивного подтверждения
# ("Do you want to use this API key? No (recommended)"), в headless-режиме отвечать некому -> "Not logged in".
export ANTHROPIC_AUTH_TOKEN="{KEY}"
# Модель задаётся через settings.json (+ modelOverrides) — переключается в сессии: /model
export ANTHROPIC_SMALL_FAST_MODEL="{SMALL}"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS={CTX}
export CLAUDE_CODE_AUTO_COMPACT_WINDOW={COMPACT}
# потолок вывода как у нативного agy (~65536) — длинные ответы/мышление не режутся
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# с 2.1.268 TaskCreate/TodoWrite выдаются только распознанным моделям Claude; у gemini-* их нет без этого флага
export CLAUDE_CODE_ENABLE_TODO_TOOLS=1
# x-claude-code-agent-type / x-claude-code-request-class в заголовках -> видно суб-агентов в логе прокси
export CLAUDE_CODE_GATEWAY_HINT_HEADERS=1
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
    # Авто-компакт на 88% контекста (как раньше) разрастается до сотен тысяч токенов на
    # 1M-моделях — а внимание трансформера дорожает с объёмом истории независимо от кэша:
    # даже при 99% прогретом кэше ход на ~570k токенов занимал 15-30с против 3-7с на ~70k
    # (см. H:\agy\dist, clirelay-read-loop-guard память). Жёсткий потолок 300k — компакт
    # либо на 88% маленького окна, либо на 300k для больших — что раньше.
    compact_val = min(int(ctx_val * 0.88), 300000)
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
