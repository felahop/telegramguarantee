"""Operator CLI (``python -m app.cli``): things that must not be possible from Telegram alone.

grant-role <user_id> <user|arbitrator|admin>   change a user's role
reset-totp <user_id>                           drop an admin's TOTP (they re-enroll with /totp)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import sqlalchemy as sa

from app.config import get_settings
from app.db.models import AdminAudit, User
from app.db.session import create_engine, create_sessionmaker
from app.domain.enums import UserRole


async def _run(args: argparse.Namespace) -> int:
    engine = create_engine(get_settings().database_url, pool_size=1)
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            user = await session.get(User, args.user_id, with_for_update=True)
            if user is None:
                print(f"user {args.user_id} not found (they must /start the bot first)", file=sys.stderr)  # noqa: T201
                return 1
            if args.command == "grant-role":
                user.role = UserRole(args.role)
                values = (
                    {"totp_secret_enc": None, "totp_confirmed_at": None} if user.role is UserRole.USER else {}
                )
                if values:
                    await session.execute(sa.update(User).where(User.id == user.id).values(**values))
            elif args.command == "reset-totp":
                user.totp_secret_enc = None
                user.totp_confirmed_at = None
            session.add(
                AdminAudit(
                    admin_id=0,
                    action=f"cli:{args.command}",
                    target_type="user",
                    target_id=user.id,
                    payload={"role": getattr(args, "role", None)},
                )
            )
        print(f"ok: {args.command} {args.user_id}")  # noqa: T201
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant-role")
    grant.add_argument("user_id", type=int)
    grant.add_argument("role", choices=[r.value for r in UserRole])
    reset = sub.add_parser("reset-totp")
    reset.add_argument("user_id", type=int)
    sys.exit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
