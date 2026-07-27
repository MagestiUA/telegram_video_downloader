"""
Одноразовий скрипт міграції: переносить mappings.json у sessions/mappings.db.

Після успішного виконання на сервері цей файл можна видалити — він більше не
потрібен, analyzer/mapper.py відтоді працює виключно через SQLite.

Запуск (всередині контейнера або з тим самим CWD, що й бот):
    python migrate_mappings_to_db.py
"""
import json
import os

from analyzer.mapper import TitleMapper, DB_PATH

LEGACY_JSON_PATH = "mappings.json"


def main():
    if not os.path.exists(LEGACY_JSON_PATH):
        print(f"{LEGACY_JSON_PATH} не знайдено — міграція не потрібна.")
        return

    with open(LEGACY_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    mapper = TitleMapper(DB_PATH)
    imported = 0
    skipped = 0
    for raw, official in data.items():
        raw, official = raw.strip(), (official or "").strip()
        if not raw or not official:
            continue
        if mapper.get_mapping(raw):
            skipped += 1
            continue
        mapper.add_mapping(raw, official)
        imported += 1

    print(f"Імпортовано: {imported}, вже існувало: {skipped}, всього в файлі: {len(data)}")

    backup_path = LEGACY_JSON_PATH + ".migrated"
    os.rename(LEGACY_JSON_PATH, backup_path)
    print(f"{LEGACY_JSON_PATH} перейменовано в {backup_path} (залишено як бекап).")
    print(f"Дані тепер у {DB_PATH}. Цей скрипт можна видалити.")


if __name__ == "__main__":
    main()
