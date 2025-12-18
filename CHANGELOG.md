# Changelog

Всі важливі зміни в проекті документуються тут.

## [2025-12-18] - Виправлення Race Condition та Покращення

### Виправлено
- **Race condition в Pyrogram** - замінено самописний багатопотоковий завантажувач на вбудований `download_media()`
  - Усунуто помилку: `RuntimeError: read() called while another coroutine is already waiting for incoming data`
  - Спрощено `core/downloader.py` з 231 до 102 рядків (-56%)

### Додано
- **Логування у файл** з автоматичною ротацією
  - Файл: `app.log`
  - Максимальний розмір: 10 МБ
  - Резервні копії: 5 файлів
  - Подвійне логування: консоль + файл
- **`.gitignore`** для виключення логів, sessions, .env з git

### Видалено
- Невикористовуваний імпорт `download_video` з `main.py`
- Дублікат коментаря в `main.py`
- Дублікат поля `GEMINI_API_KEY` в `config.py`
- Функція `download_chunk()` та всі константи багатопотокового завантажувача

### Технічні деталі

**До змін:**
```python
# Складна логіка з 4 воркерами, locks, manual chunking
async def download_chunk(...): # ~60 рядків
for i in range(WORKERS):
    tasks.append(download_chunk(...))
await asyncio.gather(*tasks)
```

**Після змін:**
```python
# Простий виклик
downloaded_path = await client.download_media(
    message,
    file_name=target_path,
    progress=progress
)
```

## Попередні Версії

### [2025-12-17] - Rate Limiting для AI API
- Додано обмеження запитів до Google Gemini (10 запитів/хв)
- Використано алгоритм Token Bucket

### [2025-12-17] - Черга Завантажень
- Реалізовано послідовну обробку завантажень
- Додано `QueueManager` для уникнення одночасних завантажень

### [2025-12-17] - Міграція на новий AI SDK
- Оновлено з `google-generativeai` на `google-genai`
- Адаптовано код під новий API
