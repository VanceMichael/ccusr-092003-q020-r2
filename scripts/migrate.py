
import os
import sqlite3
from pathlib import Path


database_path = Path(os.getenv("DATABASE_PATH", "data/app.sqlite3"))
database_path.parent.mkdir(parents=True, exist_ok=True)
sql = Path("migrations/001_bootstrap.sql").read_text(encoding="utf-8")
with sqlite3.connect(database_path) as connection:
    connection.executescript(sql)
print(f"数据库迁移完成：{database_path}")
