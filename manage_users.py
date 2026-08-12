"""CLI для управления пользователями CRM (создание/смена пароля/список/удаление).

Самостоятельной регистрации в CRM нет — пользователей заводит администратор.
Примеры:
    python manage_users.py add admin
    python manage_users.py passwd admin
    python manage_users.py list
    python manage_users.py remove admin
"""
from __future__ import annotations

import argparse
import getpass
import sys

import auth
import db


def _read_password() -> str:
    password = getpass.getpass("Пароль: ")
    confirm = getpass.getpass("Повторите пароль: ")
    if password != confirm:
        print("Пароли не совпадают", file=sys.stderr)
        raise SystemExit(1)
    if len(password) < 8:
        print("Пароль должен быть не короче 8 символов", file=sys.stderr)
        raise SystemExit(1)
    return password


def cmd_add(username: str) -> None:
    with db.session() as conn:
        if db.get_user_by_username(conn, username) is not None:
            print(f"Пользователь {username} уже существует", file=sys.stderr)
            raise SystemExit(1)
        password = _read_password()
        db.create_user(conn, username, auth.hash_password(password))
    print(f"Пользователь {username} создан")


def cmd_passwd(username: str) -> None:
    with db.session() as conn:
        if db.get_user_by_username(conn, username) is None:
            print(f"Пользователь {username} не найден", file=sys.stderr)
            raise SystemExit(1)
        password = _read_password()
        db.set_user_password(conn, username, auth.hash_password(password))
    print(f"Пароль для {username} обновлён")


def cmd_list() -> None:
    with db.session() as conn:
        users = db.list_users(conn)
    if not users:
        print("Пользователей нет")
        return
    for u in users:
        print(f"{u['id']:>3}  {u['username']:<20} создан {u['created_at']}")


def cmd_remove(username: str) -> None:
    with db.session() as conn:
        if db.get_user_by_username(conn, username) is None:
            print(f"Пользователь {username} не найден", file=sys.stderr)
            raise SystemExit(1)
        db.delete_user(conn, username)
    print(f"Пользователь {username} удалён")


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление пользователями CRM og1")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="создать пользователя")
    p_add.add_argument("username")

    p_passwd = sub.add_parser("passwd", help="сменить пароль")
    p_passwd.add_argument("username")

    sub.add_parser("list", help="список пользователей")

    p_remove = sub.add_parser("remove", help="удалить пользователя")
    p_remove.add_argument("username")

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args.username)
    elif args.command == "passwd":
        cmd_passwd(args.username)
    elif args.command == "list":
        cmd_list()
    elif args.command == "remove":
        cmd_remove(args.username)


if __name__ == "__main__":
    main()
