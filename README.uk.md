# Telegram Video Downloader Bot

> 🇬🇧 [English version](README.md)

Telegram userbot/бот для автоматичного завантаження відео з каналів та чатів. Використовує Google Gemini AI для розпізнавання метаданих аніме та організовує файли в зручну структуру папок.

## Можливості

- **AI-аналіз метаданих** — Google Gemini аналізує захаращені назви файлів і підписи для визначення назви аніме, сезону та номера серії
- **Два режими роботи** — Звичайний (аналіз кожного відео окремо) та Пакетний (назва+сезон задаються один раз, серії визначаються масово)
- **Черга завантажень** — послідовна обробка для уникнення перевантаження
- **Автоматична організація файлів** — створює папки для кожного тайтлу та перейменовує файли за шаблоном `Назва - S01E05.mp4`
- **Mapper назв** — запам'ятовує підтверджені користувачем відповідності у `mappings.json` _(лише в Звичайному режимі)_
- **Rate limiting** — алгоритм Token Bucket обмежує запити до Gemini API (10/хв)
- **Ротація логів** — `app.log`, макс. 10 МБ, 5 резервних копій, дублюється в stdout для `docker logs`
- **Docker-ready** — запуск одною командою `docker-compose up -d`

## Режими роботи

### 📥 Звичайний режим _(за замовчуванням)_
Кожне відео аналізується незалежно. AI витягує назву тайтлу, сезон і серію з підпису або назви файлу.
- Якщо назва вже є в локальній базі (`mappings.json`) → завантаження одразу
- Якщо назва нова → бот запитує офіційну назву, зберігає і завантажує
- Якщо AI повністю впав → бот просить ввести назву, серію і сезон вручну

### 📦 Пакетний режим (Batch)
Ідеальний для серіалів, де AI постійно визначає кожну серію як S01E01.
- Активувати через `/mode` → кнопка **Batch**
- **Перше відео:** AI пропонує назву (ви підтверджуєте або виправляєте) → ви задаєте сезон один раз
- **Кожне наступне відео:** AI визначає лише номер серії, знаючи назву та сезон
- Якщо номер серії не вдалося визначити → бот запитує вручну
- Повністю ізольований від `mappings.json` — без читання і запису
- Сесія завершується після **30 хвилин бездіяльності** або кнопкою **⏹ Завершити сесію**

## Архітектура

```
tg_video_downloader/
├── main.py                 # Точка входу: Pyrogram клієнт, обробники, логіка режимів
├── config/
│   └── config.py          # Конфігурація через pydantic-settings
├── core/
│   ├── downloader.py      # Обгортка Pyrogram download_media + прогрес-бар
│   ├── queue_manager.py   # Асинхронна черга завантажень
│   └── renamer.py         # Генерація імен файлів і шляхів
├── analyzer/
│   ├── ai_cleaner.py      # Gemini API: повна екстракція + екстракція лише серії
│   └── mapper.py          # Збереження відповідностей назв (JSON)
├── sessions/              # Pyrogram session файли (git-ігноруються)
├── .env                   # Секрети (git-ігнорується)
└── .env.template          # Приклад конфігурації
```

## Вимоги

- Python 3.11+
- Telegram API credentials з [my.telegram.org](https://my.telegram.org)
- Bot token від [@BotFather](https://t.me/BotFather)
- Google Gemini API key з [Google AI Studio](https://aistudio.google.com/app/apikey)

## Налаштування

### 1. Змінні середовища

Скопіюйте `.env.template` в `.env` та заповніть:

```env
API_ID=ваш_telegram_api_id
API_HASH=ваш_telegram_api_hash
BOT_TOKEN=ваш_bot_token

GEMINI_API_KEY=ваш_gemini_api_key

DOWNLOAD_PATH=/data/downloads
ALLOWED_USERS=123456789,987654321

# Опційно: session string для Docker (уникає інтерактивного входу)
SESSION_STRING=
```

`ALLOWED_USERS` — список Telegram user ID через кому. Відправте боту `/id`, щоб дізнатися свій.

### 2. Запуск через Docker (рекомендовано)

Відредагуйте шляхи до томів у `docker-compose.yml` під свою систему:

```bash
docker-compose build
docker-compose up -d

# Перегляд логів
docker-compose logs -f

# Зупинити
docker-compose down
```

### 3. Локальний запуск

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

## Команди бота

| Команда | Опис |
|---------|------|
| `/start` | Привітання та ваш User ID |
| `/id` | Отримати свій Telegram User ID |
| `/help` | Довідка та опис режимів |
| `/mode` | Перемикання між Звичайним і Пакетним режимом |

## Формат імен файлів

| Тип контенту | Формат |
|--------------|--------|
| Серія | `Назва аніме - S01E05.mp4` |
| Фільм / кліп (без серії) | `Назва аніме.mp4` |

## Технічний стек

| Бібліотека | Призначення |
|------------|-------------|
| [Pyrofork](https://github.com/pyrofork/pyrofork) | Telegram MTProto клієнт (форк Pyrogram) |
| [google-genai](https://pypi.org/project/google-genai/) | Google Gemini AI SDK |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Конфігурація через змінні середовища |
| [tgcrypto](https://github.com/pyrogram/tgcrypto) | Швидке шифрування Telegram |

## Ліцензія

MIT
