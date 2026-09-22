
import os
import sqlite3
from pathlib import Path


_OVERRIDE: Path | None = None


def set_path_override(path: Path | None) -> None:
    """仅供测试切换数据文件。"""
    global _OVERRIDE
    _OVERRIDE = path


def default_database_path() -> Path:
    return _OVERRIDE or Path(os.getenv("DATABASE_PATH", "data/app.sqlite3"))


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = database_path or default_database_path()
    # isolation_level=None：由代码显式开启事务，保证容量校验与写入在同一事务内
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_migrated(database_path: Path | None = None) -> None:
    """服务启动时确保数据文件已应用全部迁移。"""
    from scripts.migrate import run_migrations

    run_migrations(database_path or default_database_path())
