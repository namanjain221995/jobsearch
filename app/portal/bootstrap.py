"""Create the first administrator.   python -m app.portal.bootstrap

Credentials come from BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD / BOOTSTRAP_ADMIN_NAME, or are asked
for interactively. Nothing is hard-coded. Running it again is safe: an existing administrator with the
same e-mail is left untouched unless --reset-password is given."""
import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from ..config import get_settings
from . import security
from .db import make_engine, make_sessionmaker
from .models import User
from .services.audit import audit


async def run(email: str, password: str, name: str, reset: bool) -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url, pool_size=2)
    try:
        async with make_sessionmaker(engine)() as db:
            user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user is not None and user.role != "ADMIN":
                print(f"A {user.role} account already uses {email}. Choose another e-mail.", file=sys.stderr)
                return 2
            if user is not None and not reset:
                print(f"Administrator {email} already exists. Nothing changed (use --reset-password to set a new password).")
                return 0
            problem = security.password_problem(password, settings, email, name)
            if problem:
                print(f"Password rejected: {problem}", file=sys.stderr)
                return 2
            if user is None:
                user = User(name=name, email=email, role="ADMIN", is_active=True, password_hash=security.hash_password(password))
                db.add(user)
                await db.flush()
                audit(db, None, "bootstrap.admin_created", "user", user.id, None, {})
                print(f"Administrator {email} created.")
            else:
                user.password_hash, user.is_active = security.hash_password(password), True
                await security.revoke_user_sessions(db, user.id)
                audit(db, None, "bootstrap.admin_password_reset", "user", user.id, None, {})
                print(f"Password of administrator {email} was reset.")
            await db.commit()
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first administrator account.")
    parser.add_argument("--reset-password", action="store_true", help="set a new password when the administrator already exists")
    args = parser.parse_args()
    settings = get_settings()
    email = (settings.bootstrap_admin_email or (input("Administrator e-mail: ") if sys.stdin.isatty() else "")).strip().lower()
    if not email or "@" not in email:
        print("Set BOOTSTRAP_ADMIN_EMAIL (and BOOTSTRAP_ADMIN_PASSWORD) or run this command in a terminal.", file=sys.stderr)
        sys.exit(2)
    password = settings.bootstrap_admin_password
    if not password:
        if not sys.stdin.isatty():
            print("Set BOOTSTRAP_ADMIN_PASSWORD or run this command in a terminal.", file=sys.stderr)
            sys.exit(2)
        password = getpass.getpass("Administrator password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(2)
    sys.exit(asyncio.run(run(email, password, settings.bootstrap_admin_name.strip() or "Administrator", args.reset_password)))


if __name__ == "__main__":
    main()
