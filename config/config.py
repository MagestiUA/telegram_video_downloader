from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    API_ID: int
    API_HASH: str
    BOT_TOKEN: str | None = None # Optional usually, but good to have if we act as bot
    
    # Target Channel/Chat specific (optional filter)
    # TARGET_CHAT_ID: int | None = None
    
    # Download Path
    DOWNLOAD_PATH: str = "/data/downloads"
    
    
    # API Keys
    GEMINI_API_KEY: str
    # TMDB_API_KEY removed

    # Session String (for Docker)
    SESSION_STRING: str | None = None

    # Access Control
    ALLOWED_USERS: str | None = None # Comma-separated IDs

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def allowed_users_set(self) -> set[int]:
        if not self.ALLOWED_USERS:
            return set()
        try:
            return {int(x.strip()) for x in self.ALLOWED_USERS.split(",") if x.strip()}
        except ValueError:
            return set()

settings = Settings()
