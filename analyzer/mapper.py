import json
import os
import logging

logger = logging.getLogger(__name__)

MAPPING_FILE = "mappings.json"

class TitleMapper:
    def __init__(self, mapping_file=MAPPING_FILE):
        self.mapping_file = mapping_file
        self.mappings = self._load_mappings()

    def _load_mappings(self) -> dict:
        if not os.path.exists(self.mapping_file):
            return {}
        try:
            with open(self.mapping_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Failed to decode {self.mapping_file}, returning empty mappings.")
            return {}
        except Exception as e:
            logger.error(f"Error loading mappings: {e}")
            return {}

    def _save_mappings(self):
        try:
            with open(self.mapping_file, "w", encoding="utf-8") as f:
                json.dump(self.mappings, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Error saving mappings: {e}")

    def get_mapping(self, bad_title: str) -> str | None:
        """
        Returns the corrected title if a mapping exists.
        Case-insensitive lookup could be implemented here if needed,
        but for now we stick to exact or normalized match.
        """
        return self.mappings.get(bad_title.strip())

    def add_mapping(self, bad_title: str, correct_title: str):
        """
        Adds a new mapping and saves to file.
        """
        if not bad_title or not correct_title:
            return
        
        self.mappings[bad_title.strip()] = correct_title.strip()
        self._save_mappings()
        logger.info(f"Added mapping: '{bad_title}' -> '{correct_title}'")

# Global instance
mapper = TitleMapper()
