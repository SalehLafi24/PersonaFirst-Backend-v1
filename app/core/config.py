from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "PersonaFirst"
    debug: bool = False
    database_url: str = "postgresql://postgres:postgres@localhost:5432/personafirst"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
