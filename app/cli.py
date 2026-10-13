"""Локальное первоначальное заполнение и назначение сотрудников, без открытого API повышения прав."""
import argparse
import asyncio
import getpass
from datetime import timedelta
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select
from app.database import SessionLocal, engine
from app.models import Branch, Event, User, utc_now
from app.security import hash_password


async def run(args):
    try:
        async with SessionLocal() as db:
            if args.command == "create-admin":
                email = str(TypeAdapter(EmailStr).validate_python(args.email)).lower()
                if await db.scalar(select(User.id).where(User.email == email)):
                    raise ValueError("Аккаунт уже существует; выберите другой email")
                password = getpass.getpass("Пароль администратора (не менее 10 символов): ")
                if len(password) < 10 or len(password) > 128 or password != getpass.getpass("Повторите пароль: "):
                    raise ValueError("Пароли должны совпадать и содержать 10–128 символов")
                db.add(User(email=email, full_name=args.name, password_hash=await hash_password(password), role="admin",
                            consent_version="system-account", consent_text="Служебная учётная запись создана локально администратором"))
                print("Администратор создан")
            elif args.command == "assign-staff":
                user = await db.scalar(select(User).where(User.email == args.email.lower()))
                if user is None or await db.get(Branch, args.branch_id) is None:
                    raise ValueError("Сначала зарегистрируйте сотрудника и создайте филиал")
                if user.role == "admin":
                    raise ValueError("Эта команда не изменяет роль администратора")
                user.role, user.branch_id = "staff", args.branch_id
                print("Права сотрудника назначены для указанного филиала")
            elif args.command == "seed-demo":
                if await db.scalar(select(Branch.id).limit(1)):
                    raise ValueError("Демонстрационные данные добавляются только в пустую базу")
                branch = Branch(name='ЦРБ «В Озерках»', address="пр-кт Энгельса, д. 111, корп. 1")
                db.add(branch)
                await db.flush()
                start = (utc_now() + timedelta(days=7)).replace(hour=15, minute=30, second=0, microsecond=0)
                db.add(Event(branch_id=branch.id, title="Читаем вместе: современная проза", category="Читательский клуб",
                             description="Демонстрационное событие по макету. Обсуждаем книги и выбираем тему следующей встречи.",
                             program="18:30 — знакомство; 19:00 — обсуждение; 19:45 — выбор следующей темы", age_rating=16,
                             starts_at=start, ends_at=start + timedelta(minutes=90), registration_opens_at=utc_now(),
                             registration_closes_at=start, capacity=30, status="published"))
                print("Добавлен демонстрационный филиал и мероприятие (не настоящая запись в афишу)")
            await db.commit()
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin")
    admin.add_argument("email")
    admin.add_argument("--name", default="Администратор библиотеки")
    staff = commands.add_parser("assign-staff")
    staff.add_argument("email")
    staff.add_argument("branch_id", type=int)
    commands.add_parser("seed-demo")
    try:
        asyncio.run(run(parser.parse_args()))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
