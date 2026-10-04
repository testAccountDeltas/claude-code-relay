# claude-code-relay

One script to run **Claude Code** against your own [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) /
CliRelay gateway (Gemini, Claude, GPT, … on your subscription). Right-click any folder →
**"Run agent here"** → Claude Code opens in that folder with `--dangerously-skip-permissions`,
talking to your gateway. Windows & macOS.

> Один скрипт ставит Claude Code в паре с CliRelay (или любым CLIProxyAPI-шлюзом):
> правый клик по папке → **«Запустить агента здесь»** → Claude Code открывается в этой
> папке с `--dangerously-skip-permissions`, работая через шлюз на твоей подписке.

## The `x-anthropic-billing-header` fix (why this exists)

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
- Создаёт **изолированный профиль** Claude Code (`~/.clirelay-agent/claude-home`) с
  авторизацией строго по API-ключу (`CLAUDE_CODE_SIMPLE=1`) — твою обычную установку
  и OAuth-подписку Anthropic не трогает.
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

The installer fetches your gateway's model list (`/v1/models`) and writes it to Claude Code's
`availableModels` setting. The `/model` picker then lists **your gateway's models by name**
(the built-in Anthropic entries drop out), and you switch between them **without restarting** —
just `/model` → pick any (gemini, claude, gpt, …).

Edit the list anytime in `~/.clirelay-agent/claude-home/settings.json` (the `availableModels`
array); change the default with the `model` field.

> ## Смена модели (RU)
> В пикере **`/model`** — модели твоего шлюза по именам (встроенные Anthropic убраны).
> Переключение прямо в сессии: `/model` → выбрать любую. Список/дефолт правятся в
> `~/.clirelay-agent/claude-home/settings.json` (`availableModels` / `model`).

## Удаление

```bash
python setup-agent.py --uninstall
```
Убирает пункт меню и всю папку `~/.clirelay-agent` (включая изолированный профиль).
