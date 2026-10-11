# claude-code-relay

One script to run **Claude Code** against your own [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) /
CliRelay gateway (Gemini, Claude, GPT, … on your subscription). Right-click any folder →
**"Run agent here"** → Claude Code opens in that folder with `--dangerously-skip-permissions`,
talking to your gateway. Windows & macOS.

> Один скрипт ставит Claude Code в паре с CliRelay (или любым CLIProxyAPI-шлюзом):
> правый клик по папке → **«Запустить агента здесь»** → Claude Code открывается в этой
> папке с `--dangerously-skip-permissions`, работая через шлюз на твоей подписке.

<img src="docs/img/subagents-parallel-effort.png" alt="Два суб-агента параллельно, разные уровни /effort" width="700">

*Два суб-агента запущены параллельно, каждый со своим `/effort` (low / high).*

## Why this exists

A CLIProxyAPI/CliRelay gateway lets you talk to other models (Gemini, GPT, …) through an
Anthropic-compatible API, using your own subscription instead of per-token Anthropic billing.
Point Claude Code at one with `ANTHROPIC_BASE_URL` and in principle it should just work — in
practice it breaks in three separate, invisible-from-the-outside ways:

- **It fails outright.** The very first request 503s — looks like a quota problem, isn't (see
  below).
- **`/effort` silently does nothing.** The thinking-depth control in Claude Code/Desktop
  looks like it works, but the gateway ignores it, so every turn thinks at an unbounded,
  unpredictable depth — including on trivial questions.
- **It stalls.** A dropped connection costs Claude Code's full default timeout before it
  retries, which just reads as the agent "doing nothing" for tens of seconds.

This repo is one proxy + installer that fixes all three transparently. You keep using Claude
Code exactly as normal — same `/model`, same `/effort`, same workflow — it just works.

> ### Зачем это нужно (RU)
> Шлюз CLIProxyAPI/CliRelay даёт доступ к другим моделям (Gemini, GPT, …) через
> Anthropic-совместимый API, по твоей подписке, а не по токенам Anthropic. В теории
> достаточно указать Claude Code на него через `ANTHROPIC_BASE_URL` — на деле он ломается
> тремя разными, незаметными снаружи способами:
> - **падает сразу** — первый же запрос 503, выглядит как проблема с квотой, но это не она;
> - **`/effort` молча ничего не делает** — регулятор глубины мышления в Claude Code/Desktop
>   выглядит рабочим, но шлюз его игнорирует, и каждый ход думает на неограниченную,
>   непредсказуемую глубину — даже на тривиальных вопросах;
> - **зависает** — оборванное соединение стоит Claude Code полного дефолтного таймаута
>   перед повтором, со стороны это выглядит как «агент ничего не делает» десятки секунд.
>
> Этот репозиторий — один прокси + установщик, который чинит все три проблемы прозрачно.
> Работаешь в Claude Code как обычно — те же `/model`, `/effort`, тот же воркфлоу — просто
> теперь оно работает.

## The `x-anthropic-billing-header` fix

Point Claude Code at a CLIProxyAPI/Antigravity gateway via `ANTHROPIC_BASE_URL` and the very
first request fails with:

```
503 All credentials for model ... are temporarily unavailable
(upstream: 429 RESOURCE_EXHAUSTED — "Resource has been exhausted / check quota")
```

It looks like a quota/capacity problem — **it is not**. Claude Code injects a system block
whose text is `x-anthropic-billing-header: cc_version=...; cc_entrypoint=cli;`. The gateway's
Antigravity→Gemini path chokes on that text and returns `429`, which cascades to `503`.
(Proven by byte-exact request replay + bisection: removing just that one system block → `200`.)

This tool ships a tiny **local proxy that strips that block** before forwarding, so Claude Code
works. Everything else — auth, streaming, tools — already works fine over CLIProxyAPI.

## Установка

```bash
python setup-agent.py
```
Спросит **URL шлюза** и **API-ключ**, подтянет список моделей, всё настроит.

Требуется: Python 3.8+ и установленный Claude Code (`claude` в PATH).
- установить Claude Code: `npm i -g @anthropic-ai/claude-code` или `brew install --cask claude-code`.

## Что делает

- Ставит **тихий фикс-прокси** (`~/.clirelay-agent/relay-proxy.py`): вырезает служебный
  system-блок `x-anthropic-billing-header`, из-за которого шлюз иначе отдаёт 429/503.
  Работает **без окна**, один на все сессии (проверяет порт, не плодит процессы).
- Переводит `/effort` (low/medium/high/xhigh/max/ultracode) в реальный бюджет мышления
  для шлюза — он это поле иначе игнорирует. «High» и выше намеренно ограничены потолком
  24576 токенов вместо безлимита, чтобы не уходить в бесконечное раздумье.
- Короткий таймаут на обрыв соединения (4с вместо дефолтных ~20с) + больше попыток —
  обрыв не превращается в «агент завис на минуту».
- Не теряет и не портит кириллицу (и любой многобайтовый текст) в потоковом ответе —
  инкрементальный UTF-8-декодер вместо наивного `decode()` на каждом куске чтения.
- Держит авто-компакт на потолке **300K токенов** (а не 88% от окна модели, т.е. не
  880K на 1M-модели) — иначе переписка разрастается в сотни тысяч токенов и каждый
  ход тормозит вне зависимости от прогретости кэша (подробности и замеры —
  [ниже](#auto-compact-why-300k-not-1m)).
- Полный набор инструментов, включая суб-агентов (`Agent`/`Task`, `/agents`) и
  планирование задач (`TaskCreate`/`TaskUpdate`/`TodoWrite`). Ранее режим минимального
  интерфейса (`CLAUDE_CODE_SIMPLE`) молча отключал суб-агентов, а начиная с Claude Code
  2.1.268 планировщик задач выдаётся только распознанным именам Claude — обе проблемы
  тихие (агент просто делает всё сам, без ошибки), обе сняты.
- Создаёт **изолированный профиль** Claude Code (`~/.clirelay-agent/claude-home`) с
  авторизацией по `ANTHROPIC_AUTH_TOKEN` (не `ANTHROPIC_API_KEY` — тот в headless-режиме
  требует интерактивного подтверждения и ломает вход) — твою обычную установку и
  OAuth-подписку Anthropic не трогает.
- Добавляет пункт в **контекстное меню папки**:
  - **Windows** — через реестр (правый клик по папке и по пустому месту в папке);
  - **macOS** — Quick Action (правый клик → Быстрые действия). Если не появился:
    `System Settings → Keyboard → Keyboard Shortcuts → Services` — включить пункт.
- Генерирует `~/.clirelay-agent/claude-desktop-import.json` — у Claude Desktop нет
  лаунчера и своего пикера моделей, он принимает такой gateway-конфиг на импорт
  (модели там названы `claude-*` — наш же прокси ремапит их в модели шлюза). Прокси
  поднимается сам при входе в систему, в том числе для Desktop.

## Auto-compact: why 300k, not 1M

Claude Code's `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is the model's real, honest context
window — point it at one huge file/repo in a single turn and it genuinely handles
all of it. But a long back-and-forth agentic session (dozens of small `Read`/`Edit`
round-trips) is a different thing: if you let the *rolling conversation* grow toward
that window before compacting, every turn gets slower, because transformer attention
cost scales with **total history length**, not with how much of it is prompt-cached.

How compaction actually works — it doesn't touch new work, it folds up old work:

```
turn 1   turn 2   turn 3   ...   turn N          ←  history keeps growing
  └────────────┬────────────┘
       crosses AUTO_COMPACT_WINDOW
                │
                ▼
        model writes a summary of turns 1..N
        ("explored chats.php, handler at line 970,
         bug is in function X")
                │
                ▼
[ summary of 1..N ]   turn N+1   turn N+2   ...   ←  fresh turns keep flowing in untouched,
                                                      only the OLD tail got folded
```

We measured this live: a session that grew to **570k tokens (99.5% cache hit)** took
15-53s per turn; right after a manual `/compact` dropped it to **65k tokens (only
37.5% cache hit — colder, not warmer)**, turns dropped to 3-7s. Worse cache, same
account, same model — just 9x less raw history — and it got 5-10x faster. That's the
proof the slowdown was never about caching: it's attention cost over total context
size. Default Claude Code behavior compacts at ~88% of the context window, which on
a 1M-token model means waiting until ~880k tokens — by then you've already paid for
dozens of slow turns. This tool caps the auto-compact threshold at **300k** tokens
regardless of the model's actual window size, so cleanup happens 3x earlier.

> ### Авто-компакт: почему 300k, а не 1M (RU)
> `CLAUDE_CODE_MAX_CONTEXT_TOKENS` — честный потолок модели: разовая большая задача
> (залить файл/репозиторий целиком) реально использует его весь. Но длинный пошаговый
> диалог (десятки мелких `Read`/`Edit`) — другое дело: если дать **растущей переписке**
> ползти к этому потолку, каждый ход тормозит всё сильнее, потому что внимание
> трансформера дорожает с **общим объёмом истории**, а не с тем, сколько из неё взято
> из кэша.
>
> Как на самом деле работает компакт — он не трогает новое, он сворачивает старое:
> по достижении порога модель сворачивает **старые** ходы в сжатое резюме («исследован
> chats.php, обработчик на строке 970, проблема в функции X»), подставляет его в начало,
> а новые ходы продолжают копиться как обычно, НЕ сжимаясь.
>
> Замеряли живьём: сессия на **570K токенов (99.5% кэша)** — 15-53с на ход; сразу после
> ручного `/compact` до **65K токенов (всего 37.5% кэша — холоднее, не теплее!)** — 3-7с
> на ход. Кэш хуже, аккаунт/модель те же — просто истории в 9 раз меньше — и в 5-10 раз
> быстрее. Это и доказывает, что тормозило не кэширование, а объём контекста как
> таковой. Штатно Claude Code компактит на ~88% окна — на 1M-модели это ожидание до
> ~880K, а к этому моменту уже оплачены десятки медленных ходов. Этот установщик держит
> порог компакта на **300K** независимо от реального размера окна модели — уборка
> происходит втрое раньше.

## Claude Desktop

1. Установщик уже создал `~/.clirelay-agent/claude-desktop-import.json`.
2. В Desktop: настройки стороннего gateway → импортировать этот JSON.
3. Пикер моделей и `/effort` работают так же, как в Claude Code — через те же
   `claude-opus`/`claude-sonnet`/`claude-haiku`, которые прокси ремапит в модели шлюза.

## Где смотреть ошибки

Тихий прокси логирует каждый запрос/ответ (модель, `/effort`, было ли мышление,
какие инструменты вызвались, ошибки шлюза) в:
```
~/.clirelay-agent/relay-proxy.log
```
Ротируется сам при 512КБ, хранится до 3 бэкапов (`.log.1`/`.2`/`.3`) — не растёт бесконечно.

Суб-агентов видно отдельно от родительской сессии: строки запроса несут
`[agent-type=... request-class=subagent]`, если включён
`CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` (включён по умолчанию).

## Switching models in-session

The three built-in picker slots are remapped to your gateway's models via
`ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` (plus an optional extra named entry via
`ANTHROPIC_CUSTOM_MODEL_OPTION`), so the `/model` picker shows **your models by name**
and you switch between them **without restarting**:

| `/model` entry | your model |
|---|---|
| **Opus**   | `<model>-high`     |
| **Sonnet** | your default model (the picker default) |
| **Haiku**  | `<model>-low`      |
| extra      | a `pro` model if your gateway has one |

Any other model: `/model → Custom →` type the id (see `<gateway>/v1/models`).
Edit the mapping in `~/.clirelay-agent/claude-home/settings.json` → `env` →
`ANTHROPIC_DEFAULT_*_MODEL`.

> ## Смена модели (RU)
> Слоты пикера `/model` привязаны к моделям шлюза через `ANTHROPIC_DEFAULT_*_MODEL`
> (Opus→`-high`, Sonnet→дефолт, Haiku→`-low`, + доп. pro-модель), показываются ПО ИМЕНИ
> и переключаются в сессии. Привязки — в `settings.json`, блок `env`.

## Удаление

```bash
python setup-agent.py --uninstall
```
Убирает пункт меню и всю папку `~/.clirelay-agent` (включая изолированный профиль).
