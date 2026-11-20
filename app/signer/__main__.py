"""``python -m app.signer`` — the signer process: keys in memory, an internal HTTP API, no internet."""

from __future__ import annotations

from aiohttp import web

from app.config import get_chains, get_settings
from app.db.session import create_engine, create_sessionmaker
from app.logging import configure_logging, get_logger
from app.services.deals import utcnow
from app.signer.remote import build_app
from app.signer.signer import Signer

log = get_logger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    secret = settings.signer_api_secret.get_secret_value().encode()
    signer = Signer.from_keystore(settings, get_chains(), clock=utcnow)
    engine = create_engine(settings.database_url, pool_size=2)
    app = build_app(signer, create_sessionmaker(engine), secret)

    async def dispose(_: web.Application) -> None:
        await engine.dispose()

    app.on_cleanup.append(dispose)
    log.info("signer_started", wallets=len(signer.wallets))
    web.run_app(app, host="0.0.0.0", port=8090, print=None)  # noqa: S104 - reachable only on the internal network


if __name__ == "__main__":
    main()
