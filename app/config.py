from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/todo_sync"
    ms_client_id: str = ""
    ms_tenant_id: str = "consumers"
    sync_interval_seconds: int = 300
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    api_key: str = ""
    user_timezone: str = "Europe/Samara"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
