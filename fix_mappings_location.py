"""
Одноразовий скрипт-фікс: mappings.db потрапила не в той volume.

Причина: docker-compose.yml монтує /home/magestiua/Tg_video_downloader -> /app,
АЛЕ окремо монтує /DATA/AppData/tg-video-downloader/sessions -> /app/sessions
(більш вузький шлях перекриває ширший). Коли міграційний скрипт запускався
напряму на хості (CWD = /home/magestiua/Tg_video_downloader), він записав
sessions/mappings.db у ХОСТОВУ теку репозиторію, а не в реальний volume, який
бачить контейнер. Тому контейнер працює з ПОРОЖНЬОЮ mappings.db.

Цей скрипт БЕЗПЕЧНО ЗЛИВАЄ (не перезаписує!) дані:
  - джерело (стара, помилково записана БД з 26 історичними мапінгами):
      /home/magestiua/Tg_video_downloader/sessions/mappings.db
  - ціль (справжній volume, який реально бачить контейнер; могла вже
    накопичити НОВІ мапінги з моменту бага):
      /DATA/AppData/tg-video-downloader/sessions/mappings.db

Записи в цілі, які там вже є, НЕ перезаписуються — додаються лише відсутні.

Запуск (з будь-якої директорії, шляхи абсолютні):
    python fix_mappings_location.py

Після успішного виконання — перезапустити контейнер:
    docker compose restart tg_video_downloader

Цей файл можна видалити після використання.
"""
import sqlite3
import os

SOURCE_DB = "/home/magestiua/Tg_video_downloader/sessions/mappings.db"
TARGET_DB = "/DATA/AppData/tg-video-downloader/sessions/mappings.db"


def main():
    if not os.path.exists(SOURCE_DB):
        print(f"Джерело {SOURCE_DB} не знайдено — виправляти нічого.")
        return
    if not os.path.exists(TARGET_DB):
        print(f"Ціль {TARGET_DB} не знайдено. Перевір, що контейнер хоч раз запускався.")
        return

    src = sqlite3.connect(SOURCE_DB)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(TARGET_DB)
    dst.row_factory = sqlite3.Row

    dst.execute("""
        CREATE TABLE IF NOT EXISTS mappings (
            raw_title      TEXT PRIMARY KEY,
            official_title TEXT NOT NULL
        )
    """)

    src_rows = src.execute("SELECT raw_title, official_title FROM mappings").fetchall()
    existing = {r["raw_title"] for r in dst.execute("SELECT raw_title FROM mappings").fetchall()}

    added = 0
    skipped = 0
    for row in src_rows:
        if row["raw_title"] in existing:
            skipped += 1
            continue
        dst.execute(
            "INSERT INTO mappings (raw_title, official_title) VALUES (?, ?)",
            (row["raw_title"], row["official_title"])
        )
        added += 1
    dst.commit()

    total_after = dst.execute("SELECT COUNT(*) AS c FROM mappings").fetchone()["c"]

    src.close()
    dst.close()

    print(f"Джерело: {len(src_rows)} записів.")
    print(f"Додано в ціль: {added}, вже було в цілі (пропущено): {skipped}.")
    print(f"Всього записів у цілі тепер: {total_after}.")
    print(f"\nЦіль ({TARGET_DB}) — саме той файл, який бачить контейнер.")
    print("Перезапусти контейнер: docker compose restart tg_video_downloader")


if __name__ == "__main__":
    main()
