import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - unit tests may run without python-dotenv installed
    def load_dotenv(*args, **kwargs):
        return False


_DEFAULT_WEAK_PASSWORDS = {
    "",
    "admin",
    "admin123",
    "password",
    "123456",
}

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    admin_password: str
    factory_root: Path = Path(__file__).resolve().parents[2]
    jwt_hours: int = 24
    auth_db_file: Path | None = None
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    )
    max_upload_size: int = 100 * 1024 * 1024
    gcp_project_id: str = ""
    gcp_region: str = "europe-west4"
    gcp_solver_service: str = "solver-api"

    @property
    def ongoing_dir(self) -> Path:
        return self.factory_root / "ongoing"

    @property
    def complete_dir(self) -> Path:
        return self.factory_root / "complete"

    @property
    def uploads_dir(self) -> Path:
        return self.factory_root / "uploads"

    @property
    def papers_dir(self) -> Path:
        return self.factory_root / "papers"

    @property
    def logs_dir(self) -> Path:
        return self.factory_root / "logs"

    @property
    def launch_script(self) -> Path:
        return self.factory_root / "launch_agents.sh"

    @property
    def model_registry_file(self) -> Path:
        return self.factory_root / "web" / "model_registry.json"

    @property
    def model_config_file(self) -> Path:
        return self.factory_root / "web" / "model_config.json"

    @property
    def resolved_auth_db_file(self) -> Path:
        return self.auth_db_file or self.factory_root / "web" / "auth.db"


def load_settings() -> Settings:
    cors_origins = tuple(
        part.strip()
        for part in (os.getenv("CORS_ORIGINS") or "").split(",")
        if part.strip()
    ) or Settings.cors_origins

    auth_db_env = (os.getenv("AUTH_DB_FILE") or "").strip()

    return Settings(
        jwt_secret=(os.getenv("JWT_SECRET") or os.getenv("JWT_SECRET_KEY") or "").strip(),
        admin_password=(os.getenv("ADMIN_PASSWORD") or "").strip(),
        factory_root=Path(os.getenv("FACTORY_ROOT") or Path(__file__).resolve().parents[2]),
        jwt_hours=int(os.getenv("JWT_EXPIRATION_HOURS") or 24),
        auth_db_file=Path(auth_db_env) if auth_db_env else None,
        cors_origins=cors_origins,
        max_upload_size=int(os.getenv("MAX_UPLOAD_SIZE") or 100 * 1024 * 1024),
        gcp_project_id=(os.getenv("GCP_PROJECT_ID") or "").strip(),
        gcp_region=(os.getenv("GCP_REGION") or "europe-west4").strip(),
        gcp_solver_service=(os.getenv("GCP_SOLVER_SERVICE") or "solver-api").strip(),
    )


def validate_settings(settings: Settings) -> None:
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 characters long.")

    if settings.admin_password in _DEFAULT_WEAK_PASSWORDS:
        raise RuntimeError("ADMIN_PASSWORD is missing or uses a default weak value.")
