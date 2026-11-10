from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_FILE = 25 * 1024 * 1024
MAX_REQUEST = MAX_FILE + 64 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir=None, extra="ignore")

    database_url: SecretStr
    public_origin: str = "https://localhost"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_token_auth: str = "client_secret_basic"
    openai_api_key: SecretStr = SecretStr("")
    openai_answer_model: str = "gpt-4.1-mini"
    web_hosts: str = ""
    question_limit_per_minute: int = 12

    @field_validator("public_origin", "oidc_issuer")
    @classmethod
    def https_url(cls, value: str, info):
        if not value and info.field_name == "oidc_issuer":
            return value
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
            or (info.field_name == "public_origin" and url.path not in ("", "/"))
        ):
            raise ValueError("Expected an HTTPS URL")
        return value.rstrip("/") if info.field_name == "public_origin" else value

    @classmethod
    def load(cls):
        secrets_dir = Path("/run/secrets")
        return cls(_secrets_dir=secrets_dir if secrets_dir.is_dir() else None)

    @property
    def allowed_hosts(self) -> set[str]:
        return {host.strip().lower().rstrip(".") for host in self.web_hosts.split(",") if host.strip()}
