import os
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_WEAK_PASSWORDS = {
    "",
    "admin",
    "admin123",
    "password",
    "123456",
}


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    admin_password: str
    factory_root: Path = Path(__file__).resolve().parents[2]
    jwt_hours: int = 24


def load_settings() -> Settings:
    return Settings(
        jwt_secret=(os.getenv("JWT_SECRET") or os.getenv("JWT_SECRET_KEY") or "").strip(),
        admin_password=(os.getenv("ADMIN_PASSWORD") or "").strip(),
        factory_root=Path(os.getenv("FACTORY_ROOT") or Path(__file__).resolve().parents[2]),
        jwt_hours=int(os.getenv("JWT_EXPIRATION_HOURS") or 24),
    )


def validate_settings(settings: Settings) -> None:
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 characters long.")

    if settings.admin_password in _DEFAULT_WEAK_PASSWORDS:
        raise RuntimeError("ADMIN_PASSWORD is missing or uses a default weak value.")
