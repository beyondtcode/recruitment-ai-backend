from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"
    # Light model for matcher batch scoring; empty falls back to anthropic_model.
    anthropic_matcher_model: str = "claude-3-5-haiku-20241022"
    monday_board_id: str


settings = Settings()
