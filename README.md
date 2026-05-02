# Bookmark Processor

Стежить за закладками **Safari**, **Firefox** або **Chrome** і автоматично обробляє нові посилання: генерує заголовок, саммарі та теги через OpenAI, зберігає нотатку в Obsidian і надсилає повідомлення в месенджери.

## Можливості

- Підтримує **Safari** (binary plist), **Firefox** (SQLite `places.sqlite`), **Chrome** (JSON)
- Відстежує зміни у файлі закладок у реальному часі
- Групує URL за регулярними виразами — кожна група має власні правила обробки
- Генерує через OpenAI: **заголовок**, **саммарі**, **теги** (мова задається в конфігу)
- Для GitHub-репозиторіїв автоматично додає автора і назву як теги
- Зберігає Markdown-нотатку з YAML frontmatter в Obsidian
- Надсилає сповіщення в **Telegram**, **Mastodon**, **Signal**, **Matrix**
- Обробляє одне довільне посилання з командного рядка
- Підтримує початкову ініціалізацію (`--init`) — фіксує поточний стан без обробки

## Вимоги

- macOS (Safari, Firefox або Chrome)
- Python 3.11+
- OpenAI API key — для генерації заголовку, саммарі та тегів
- **Firefox**: `places.sqlite` читається напряму, Firefox може бути відкритим
- **Signal**: потребує запущеного [signal-cli REST API](https://github.com/bbernhard/signal-rest-api) (Docker)

## Встановлення

```bash
git clone <repo>
cd BMProc
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Відредагуй `config.yaml` — вкажи шлях до Obsidian vault та OpenAI API key.

## Конфігурація

### Структура `config.yaml`

```yaml
openai:
  api_key: "sk-..."        # або env OPENAI_API_KEY
  model: gpt-4o-mini       # gpt-4o для вищої якості
  language: Ukrainian      # мова заголовку і саммарі за замовчуванням

obsidian:
  vault_path: ~/Documents/Obsidian

# Платформи для сповіщень (заповни лише ті, що використовуєш)
telegram:
  bot_token: ""            # або env TELEGRAM_BOT_TOKEN
  chat_id: ""              # або env TELEGRAM_CHAT_ID

mastodon:
  instance_url: "https://mastodon.social"
  access_token: ""         # або env MASTODON_ACCESS_TOKEN
  visibility: public       # public | unlisted | private | direct

signal:
  api_url: "http://localhost:8080"   # signal-cli REST API
  number: "+380501234567"
  recipients:
    - "+380671234567"

matrix:
  homeserver: "https://matrix.org"
  access_token: ""         # або env MATRIX_ACCESS_TOKEN
  room_id: ""              # або env MATRIX_ROOM_ID

groups:
  - name: GitHub
    pattern:
      - "github\\.com/[^/]+/[^/]+"
    action: tags+summary
    target: Tech/GitHub
    language: English      # перевизначення мови для групи
    notify:
      - telegram
      - matrix
```

### Поля групи

| Поле | Опис |
| --- | --- |
| `name` | Назва групи (для логів і `--group`) |
| `pattern` | Список регулярних виразів для URL (перший збіг виграє) |
| `action` | Що генерувати: `summary`, `tags`, `tags+summary`, або `""` |
| `target` | Папка всередині Obsidian vault (створюється автоматично) |
| `language` | Мова заголовку і саммарі для цієї групи (перевизначає глобальний) |
| `notify` | Список платформ для сповіщень: `telegram`, `mastodon`, `signal`, `matrix` |

### AI-обробка (`action`)

При будь-якому `action` OpenAI завжди генерує **заголовок** (`title`).

| Значення | Що генерується |
| --- | --- |
| `summary` | заголовок + саммарі |
| `tags` | заголовок + теги |
| `tags+summary` | заголовок + саммарі + теги |
| `""` | нічого (посилання зберігається без AI) |

Теги завжди англійською (зручніше для пошуку). Для GitHub-репозиторіїв автор і назва додаються як перші два теги незалежно від AI.

### Формат нотатки в Obsidian

```markdown
---
tags: [astral-sh, uv, package-manager, python, rust]
url: "https://github.com/astral-sh/uv"
added: 2026-05-01
---

# uv — надшвидкий менеджер пакетів Python

> https://github.com/astral-sh/uv

## Summary

uv — це менеджер пакетів і інструмент для Python-проєктів, написаний на Rust...
```

## Використання

### Перший запуск — ініціалізація

Щоб уже наявні закладки не оброблялись, спочатку зафіксуй поточний стан:

```bash
python bookmark_processor.py --init
# → Init complete — 847 bookmark(s) recorded as baseline (none processed)
```

### Watch-режим (основний)

```bash
# Safari (за замовчуванням)
python bookmark_processor.py

# Firefox (авто-визначення профілю)
python bookmark_processor.py --browser firefox

# Chrome
python bookmark_processor.py --browser chrome
```

Слідкує за файлом закладок. Щойно браузер зберігає нову закладку — скрипт її обробляє. Зупинка: `Ctrl-C`.

### Один прохід

```bash
python bookmark_processor.py --once
```

### Обробка одного посилання

```bash
# Автоматичний матчинг по групах
python bookmark_processor.py https://github.com/astral-sh/uv

# Примусово вказати групу
python bookmark_processor.py https://example.com/article --group "Tech Articles"
```

### Переобробити всі закладки

```bash
python bookmark_processor.py --reset --once
```

## Аргументи командного рядка

| Аргумент | Опис |
| --- | --- |
| `URL` | Обробити одне посилання і вийти |
| `--group NAME` | Примусово вказати групу для URL (ігнорує pattern) |
| `--browser NAME` | Браузер: `safari` (default), `firefox`, `chrome` |
| `--init` | Зафіксувати поточні закладки як baseline, нічого не обробляти |
| `--once` | Один прохід по дельті і вийти |
| `--reset` | Скинути стан і обробити всі закладки заново |
| `--config FILE` | Шлях до YAML-конфігу (за замовч.: `config.yaml`) |
| `--state FILE` | Шлях до файлу стану (за замовч.: `.state.json`) |
| `--bookmarks FILE` | Перевизначити шлях до файлу закладок (авто-визначається по `--browser`) |
| `--verbose`, `-v` | Debug-логування |

## Змінні оточення

Всі чутливі значення можна задавати через env замість конфігу:

| Змінна | Призначення |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID |
| `MASTODON_ACCESS_TOKEN` | Mastodon access token |
| `SIGNAL_NUMBER` | Signal номер відправника |
| `MATRIX_ACCESS_TOKEN` | Matrix access token |
| `MATRIX_ROOM_ID` | Matrix room ID |

## Налаштування Signal

Signal не має офіційного API. Скрипт використовує [signal-cli REST API](https://github.com/bbernhard/signal-rest-api) — Docker-контейнер, що обгортає `signal-cli`:

```bash
docker run -p 8080:8080 \
  -v /path/to/signal-cli-config:/home/.local/share/signal-cli \
  bbernhard/signal-rest-api
```

## Структура проєкту

```text
BMProc/
├── bookmark_processor.py   — головний скрипт
├── config.yaml             — твоя конфігурація (не комітити з секретами)
├── config.example.yaml     — шаблон конфігурації
├── requirements.txt        — залежності
└── .state.json             — стан обробки (генерується автоматично)
```

## Ліцензія

Copyright (C) 2026 Андрій Петренко

Ця програма є вільним програмним забезпеченням і розповсюджується на умовах
[Ukrainian Restricted Jurisdictions Public License (URJPL) v1.0](LicenseUA.md).

**УВАГА:** Використання цієї програми на території Російської Федерації,
Китайської Народної Республіки та Ісламської Республіки Іран **СУВОРО ЗАБОРОНЕНО**.
