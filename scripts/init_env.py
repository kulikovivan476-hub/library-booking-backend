"""Создать локальный .env с новым случайным паролем базы. Существующий файл не изменяется."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit(".env уже существует; его настройки сохранены")
password = secrets.token_hex(24)
template = (root / ".env.example").read_text(encoding="utf-8")
template = template.replace("DB_PASSWORD=\n", f"DB_PASSWORD={password}\n").replace("your-password", password)
target.write_text(template, encoding="utf-8")
print("Создан локальный .env с отдельным паролем базы библиотеки")
