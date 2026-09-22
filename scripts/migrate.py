
import os
import sqlite3
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _applied_versions(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations"
    ).fetchall()
    return {row[0] for row in rows}


def run_migrations(database_path: Path) -> list[str]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    newly_applied: list[str] = []
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        already = _applied_versions(connection)
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = sql_file.stem
            if version in already:
                continue
            connection.executescript(sql_file.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
            newly_applied.append(version)
    return newly_applied


def main() -> None:
    database_path = Path(os.getenv("DATABASE_PATH", "data/app.sqlite3"))
    applied = run_migrations(database_path)
    if applied:
        print(f"数据库迁移完成：{database_path}（应用：{', '.join(applied)}）")
    else:
        print(f"数据库已是最新：{database_path}")


if __name__ == "__main__":
    main()
