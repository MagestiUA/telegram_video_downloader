"""
Одноразовий скрипт міграції: копіює sessions/dorama.db -> sessions/anime.db
(перейменування шляху після повного рефакторингу пакету dorama/ -> anime_tracker/).

ВАЖЛИВО: запускати ЛИШЕ через `docker exec` (усередині контейнера), А НЕ
напряму на хості! Через накладання volume-мапінгів у docker-compose.yml
(/app та окремо /app/sessions) шлях "sessions/" резолвиться по-різному
залежно від того, звідки запущено скрипт — ми вже раз наступили на ці
граблі з мапінгами (mappings.json). docker exec гарантує ту саму файлову
систему, що бачить сам бот.

Запуск:
    docker exec -it tg_video_downloader python migrate_dorama_db.py

Після успішного виконання — перезапустити контейнер, щоб new-коду підхопив
нову назву БД (перейменований пакет уже читає sessions/anime.db):
    docker compose restart tg_video_downloader

Стару sessions/dorama.db скрипт НЕ видаляє (лишає як бекап) — можна
прибрати вручну пізніше, після перевірки що все працює.
Цей файл можна видалити після використання.
"""
import os
import shutil

OLD_PATH = "sessions/dorama.db"
NEW_PATH = "sessions/anime.db"


def main():
    if not os.path.exists(OLD_PATH):
        print(f"{OLD_PATH} не знайдено — міграція не потрібна (можливо, вже виконана).")
        return
    if os.path.exists(NEW_PATH):
        print(f"{NEW_PATH} вже існує — пропускаю копіювання, щоб нічого не перезаписати.")
        return
    shutil.copy2(OLD_PATH, NEW_PATH)
    print(f"Скопійовано {OLD_PATH} -> {NEW_PATH}.")
    print(f"Старий файл {OLD_PATH} залишено як є (бекап, можна видалити вручну пізніше).")
    print("Тепер перезапусти контейнер: docker compose restart tg_video_downloader")


if __name__ == "__main__":
    main()
