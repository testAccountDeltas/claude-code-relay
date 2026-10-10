# claude-code-relay

One script to run **Claude Code** against your own [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) /
CliRelay gateway (Gemini, Claude, GPT, … on your subscription). Right-click any folder →
**"Run agent here"** → Claude Code opens in that folder with `--dangerously-skip-permissions`,
talking to your gateway. Windows & macOS.

> Один скрипт ставит Claude Code в паре с CliRelay (или любым CLIProxyAPI-шлюзом):
> правый клик по папке → **«Запустить агента здесь»** → Claude Code открывается в этой
> папке с `--dangerously-skip-permissions`, работая через шлюз на твоей подписке.

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
- Переводит `/effort` (low/medium/high) в реальный бюджет мышления для шлюза — он
  это поле иначе игнорирует. Без ограничения в safety-режиме «high» не уходит в
  бесконечное раздумье — верхняя граница 24576 токенов.
- Короткий таймаут на обрыв соединения (4с вместо дефолтных ~20с) + больше попыток —
  обрыв не превращается в «агент завис на минуту».
- Полный набор инструментов, включая суб-агентов (`Agent`/`Task`, `/agents`). Ранее
  режим минимального интерфейса (`CLAUDE_CODE_SIMPLE`) молча их отключал — агент делал
  работу сам и лишь писал в чат, что «запускает суб-агента».
- Создаёт **изолированный профиль** Claude Code (`~/.clirelay-agent/claude-home`) с
  авторизацией по `ANTHROPIC_AUTH_TOKEN` (не `ANTHROPIC_API_KEY` — тот в headless-режиме
  требует интерактивного подтверждения и ломает вход) — твою обычную установку и
  OAuth-подписку Anthropic не трогает.
- Добавляет пункт в **контекстное меню папки**:
  - **Windows** — через реестр (правый клик по папке и по пустому месту в папке);
  - **macOS** — Quick Action (правый клик → Быстрые действия). Если не появился:
    `System Settings → Keyboard → Keyboard Shortcuts → Services` — включить пункт.

## Где смотреть ошибки

Тихий прокси пишет **только ошибки шлюза** (и строку старта) в:
```
~/.clirelay-agent/relay-proxy.log
```

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
