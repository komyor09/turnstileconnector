"""
Настройки через переменные окружения (pydantic-settings) — для запуска в Docker/Linux.

Работает вместе с config.toml: на Windows берётся TOML, в Docker — env. Если заданы и TOML,
и env — env перекрывает (connector.configure() так и делает). Пароли живут только в .env /
--env-file и в образ/гит не попадают.

Имена переменных см. в .env.example.
"""
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # База (MariaDB/MySQL)
    db_host: Optional[str] = None
    db_port: int = 3306
    db_user: Optional[str] = None
    db_password: Optional[str] = None
    db_name: Optional[str] = None            # имя базы коннектора (turnstile_db)
    db_charset: str = "utf8"

    # Синхронизация карт
    sync_view: str = "v_turnstile_xobgoh"
    dry_run: bool = False
    reconcile_at: str = "03:00"              # ночная полная сверка; "" — выкл.

    # Вебхук ISU → /sync
    http_token: Optional[str] = None         # токен вебхука ISU

    # Проходы (события)
    events_enabled: bool = False
    poll_seconds: int = 2
    history_on_first_start: bool = False
    mdb_path: str = ""                       # файл N3000 Download для суточной сверки; "" — выкл.

    # API/Swagger
    api_key: Optional[str] = None            # X-API-Key для эндпоинтов записи; пусто — запись запрещена
    root_path: str = ""                      # UVICORN_ROOT_PATH, напр. /turnstile (nginx срезает префикс)

    # Лог: пусто — только stdout (как надо в Docker)
    log_file: str = ""
    log_max_mb: int = 10
    log_keep: int = 10


def db_dict(s: Settings) -> dict:
    return {"host": s.db_host, "port": s.db_port, "user": s.db_user,
            "password": s.db_password, "database": s.db_name, "charset": s.db_charset}
