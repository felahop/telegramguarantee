"""CryptoBot (Crypto Pay) webhook endpoint: ``POST /cryptopay/<secret path>``.

1. The signature is verified over the raw body *before* anything is parsed.
2. Processing is idempotent (duplicate deliveries and the polling fallback are no-ops).
3. 200 is returned for valid-but-irrelevant updates so the provider does not retry them forever;
   500 only when our own processing failed (the poller will pick the invoice up anyway).
"""

from __future__ import annotations

from datetime import timedelta

from aiohttp import web
from pydantic import ValidationError

from app.bot.context import BotContext
from app.logging import get_logger
from app.providers.cryptobot.client import SIGNATURE_HEADER, WebhookUpdate, verify_signature
from app.services.payments import PaymentService

log = get_logger(__name__)
PATH_PREFIX = "/cryptopay/"
STALE_AFTER = timedelta(hours=1)


class CryptoPayWebhook:
    def __init__(self, ctx: BotContext, api_token: str) -> None:
        self._ctx = ctx
        self._token = api_token

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not verify_signature(self._token, body, request.headers.get(SIGNATURE_HEADER, "")):
            log.warning("cryptopay_bad_signature", remote=request.remote)
            return web.json_response({"ok": False}, status=401)
        try:
            update = WebhookUpdate.model_validate_json(body)
        except ValidationError:
            log.warning("cryptopay_bad_payload")
            return web.json_response({"ok": False}, status=400)
        if update.update_type != "invoice_paid":
            return web.json_response({"ok": True})
        if update.request_date is not None and self._ctx.clock() - update.request_date > STALE_AFTER:
            # Genuine (signed) but old: Crypto Pay retries for up to 3 days. Processing is idempotent,
            # so a late delivery is applied; it is logged for monitoring.
            log.warning(
                "cryptopay_stale_update", update_id=update.update_id, request_date=str(update.request_date)
            )
        try:
            async with self._ctx.sessionmaker() as session, session.begin():
                result = await PaymentService(
                    session,
                    self._ctx.settings,
                    self._ctx.chains,
                    gateway=self._ctx.gateway,
                    clock=self._ctx.clock,
                ).apply_cryptobot_invoice(update.payload)
        except Exception as exc:
            log.error("cryptopay_webhook_failed", update_id=update.update_id, error=repr(exc), exc_info=exc)
            return web.json_response({"ok": False}, status=500)
        log.info(
            "cryptopay_webhook",
            update_id=update.update_id,
            invoice_id=update.payload.invoice_id,
            result=result.value,
        )
        return web.json_response({"ok": True})

    def register(self, app: web.Application, secret_path: str) -> None:
        if not secret_path:
            raise ValueError("CRYPTOBOT_WEBHOOK_SECRET_PATH is required")
        app.router.add_post(PATH_PREFIX + secret_path, self.handle)
