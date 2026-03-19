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
- **Сумісність з CasaOS** — встановлення та налаштування прямо з інтерфейсу CasaOS без редагування файлів

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
├── .env                   # Секрети для локальної розробки (git-ігнорується)
└── .env.template          # Приклад конфігурації
```

## Вимоги

- Python 3.11+
- Telegram API credentials з [my.telegram.org](https://my.telegram.org)
- Bot token від [@BotFather](https://t.me/BotFather)
- Google Gemini API key з [Google AI Studio](https://aistudio.google.com/app/apikey)

## Конфігурація

Всі налаштування зчитуються зі **змінних середовища**. Пріоритет:
1. Змінні ОС (блок `environment:` в Docker, інтерфейс CasaOS) — вищий пріоритет
2. Файл `.env` — fallback для локальної розробки

| Змінна | Обов'язкова | Опис |
|--------|-------------|------|
| `API_ID` | ✅ | Telegram API ID з my.telegram.org |
| `API_HASH` | ✅ | Telegram API Hash з my.telegram.org |
| `BOT_TOKEN` | ✅ | Токен бота від @BotFather |
| `GEMINI_API_KEY` | ✅ | Google Gemini API key |
| `DOWNLOAD_PATH` | — | Папка завантажень всередині контейнера (за замовч.: `/data/downloads`) |
| `ALLOWED_USERS` | — | Telegram user ID через кому — кому дозволено користуватись ботом |
| `SESSION_STRING` | — | Pyrogram session string — потрібен для Docker (уникає інтерактивного входу) |

`ALLOWED_USERS` — відправте боту `/id`, щоб дізнатися свій Telegram ID.

## Налаштування

### Варіант А: CasaOS (рекомендовано для домашніх серверів)

1. CasaOS → **App Store** → **Custom Install** → вставити вміст `docker-compose.yml`
2. Заповнити облікові дані прямо в інтерфейсі CasaOS — секції Volumes та Environment Variables
3. Натиснути **Submit**

Усі поля мають підписи. Редагувати файли не потрібно.

### Варіант Б: Docker (стандартно)

Відредагуйте `docker-compose.yml` — заповніть порожні значення в блоці `environment:`, потім:

```bash
docker-compose build
docker-compose up -d

# Перегляд логів
docker-compose logs -f

# Зупинити
docker-compose down
```

### Варіант В: Локальний запуск

Скопіюйте `.env.template` в `.env` та заповніть:

```bash
cp .env.template .env
```

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
