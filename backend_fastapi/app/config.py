from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "chat-ai-fastapi"
    env: str = "dev"

    # Local defaults use localhost; docker-compose overrides these via env vars.
    postgres_dsn: str = "postgresql+asyncpg://chat:chat@localhost:5432/chatdb"
    redis_url: str = "redis://localhost:6379/0"

    internal_api_token: str = "change-me-in-prod"
    memory_max_items: int = 20
    
    # OpenAI Integration
    # OpenAI / Azure OpenAI Integration
    openai_api_key: str = ""
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2025-01-01-preview"
    azure_openai_deployment: str = "gpt-41_milky"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()


def get_settings() -> Settings:
    """获取全局 settings 单例"""
    return settings
