"""Crypto Pay API (CryptoBot) client.

Docs: https://help.send.tg/en/articles/10279948-crypto-pay-api — testnet bot @CryptoTestnetBot.
Models ignore unknown fields so API additions never break payment processing; amounts are parsed
as ``Decimal`` from the API's strings and converted to minor units by the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from app.logging import get_logger

log = get_logger(__name__)

MAINNET_URL = "https://pay.crypt.bot/api"
TESTNET_URL = "https://testnet-pay.crypt.bot/api"
SIGNATURE_HEADER = "crypto-pay-api-signature"
MAX_INVOICE_IDS_PER_CALL = 100
_READ_RETRIES = 3


class CryptoPayError(Exception):
    """Business error returned by the API (``{"ok": false, "error": {...}}``)."""

    def __init__(self, name: str, code: int | None = None, method: str = "") -> None:
        super().__init__(f"{method}: {name} ({code})")
        self.name = name
        self.code = code
        self.method = method

    @property
    def kind(self) -> str:  # noqa: PLR0911 - a classification table
        """Coarse classification by error name (robust to the exact naming used by the API)."""
        name = self.name.upper()
        if "SPEND_ID" in name:
            return "spend_id_used"
        if "INSUFFICIENT" in name or "NOT_ENOUGH" in name or "BALANCE" in name:
            return "insufficient_funds"
        if "USER" in name or "RECIPIENT" in name:
            return "recipient"
        if "AMOUNT" in name or "TOO_SMALL" in name or "TOO_BIG" in name or "LIMIT" in name:
            return "amount"
        if "DISABLED" in name or "NOT_ALLOWED" in name or "FORBIDDEN" in name:
            return "disabled"
        if "TOKEN" in name or "UNAUTHORIZED" in name or self.code == 401:
            return "auth"
        return "other"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Invoice(_Model):
    invoice_id: int
    status: str  # active | paid | expired
    currency_type: str | None = None
    asset: str | None = None
    amount: Decimal
    paid_asset: str | None = None
    paid_amount: Decimal | None = None
    fee_asset: str | None = None
    fee_amount: Decimal | None = None
    payload: str | None = None
    bot_invoice_url: str | None = None
    mini_app_invoice_url: str | None = None
    web_app_invoice_url: str | None = None
    pay_url: str | None = None  # deprecated alias of bot_invoice_url
    expiration_date: datetime | None = None
    paid_at: datetime | None = None
    paid_anonymously: bool | None = None

    @property
    def link(self) -> str | None:
        return self.bot_invoice_url or self.pay_url or self.mini_app_invoice_url or self.web_app_invoice_url

    @property
    def received_asset(self) -> str | None:
        return self.paid_asset or self.asset

    @property
    def received_amount(self) -> Decimal:
        return self.paid_amount if self.paid_amount is not None else self.amount


class Transfer(_Model):
    transfer_id: int
    spend_id: str | None = None
    user_id: int
    asset: str
    amount: Decimal
    status: str  # completed
    completed_at: datetime | None = None


class Balance(_Model):
    currency_code: str
    available: Decimal
    onhold: Decimal = Decimal(0)


class WebhookUpdate(_Model):
    update_id: int
    update_type: str  # invoice_paid
    request_date: datetime | None = None
    payload: Invoice


def verify_signature(token: str, body: bytes, signature: str) -> bool:
    """HMAC-SHA256 of the *raw* body with key SHA256(api token); constant-time comparison."""
    if not token or not signature:
        return False
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def api_amount(value: Decimal) -> str:
    """Decimal → API string without exponent or trailing zeros (``102.5``, ``100``)."""
    normalized = value.normalize()
    return f"{normalized:f}"


class CryptoPayClient:
    def __init__(
        self,
        token: SecretStr,
        *,
        testnet: bool,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Crypto-Pay-API-Token": token.get_secret_value()},
        )

    @property
    def token(self) -> str:
        return self._token.get_secret_value()

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, params: dict[str, Any], *, retry_reads: bool) -> Any:
        """``retry_reads``: the method is idempotent and may be retried after any transport error.
        Writes are retried only when the request provably never reached the server (connect failed)."""
        clean = {k: v for k, v in params.items() if v is not None}
        attempts = _READ_RETRIES if retry_reads else 2
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                response = await self._http.post(f"/{method}", json=clean)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if last:
                    raise
                await asyncio.sleep(0.2 * attempt)
                continue
            except httpx.TransportError:
                if not retry_reads or last:
                    raise
                await asyncio.sleep(0.2 * attempt)
                continue
            if response.status_code >= 500 and retry_reads and not last:
                await asyncio.sleep(0.2 * attempt)
                continue
            data = response.json() if response.content else {}
            if not data.get("ok"):
                error = data.get("error") or {}
                name = error.get("name") or f"HTTP_{response.status_code}"
                log.warning("cryptopay_error", method=method, error=name, status=response.status_code)
                raise CryptoPayError(str(name), error.get("code") or response.status_code, method)
            return data.get("result")
        raise RuntimeError("unreachable")

    async def get_me(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("getMe", {}, retry_reads=True)
        return result

    async def create_invoice(
        self,
        *,
        asset: str,
        amount: Decimal,
        description: str,
        payload: str,
        expires_in: int,
    ) -> Invoice:
        result = await self._call(
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": asset,
                "amount": api_amount(amount),
                "description": description[:1024],
                "payload": payload,
                "expires_in": expires_in,
                "allow_comments": False,
                "allow_anonymous": False,
            },
            retry_reads=False,
        )
        return Invoice.model_validate(result)

    async def delete_invoice(self, invoice_id: int) -> bool:
        return bool(await self._call("deleteInvoice", {"invoice_id": invoice_id}, retry_reads=False))

    async def get_invoices(self, invoice_ids: Sequence[int], *, status: str | None = None) -> list[Invoice]:
        invoices: list[Invoice] = []
        ids = list(invoice_ids)
        for start in range(0, len(ids), MAX_INVOICE_IDS_PER_CALL):
            chunk = ids[start : start + MAX_INVOICE_IDS_PER_CALL]
            result = await self._call(
                "getInvoices",
                {"invoice_ids": ",".join(map(str, chunk)), "status": status, "count": len(chunk)},
                retry_reads=True,
            )
            items = result.get("items", []) if isinstance(result, dict) else result
            invoices.extend(Invoice.model_validate(item) for item in items)
        return invoices

    async def transfer(
        self, *, user_id: int, asset: str, amount: Decimal, spend_id: str, comment: str
    ) -> Transfer:
        """Not retried here: the caller resolves an uncertain outcome via ``find_transfer``."""
        result = await self._call(
            "transfer",
            {
                "user_id": user_id,
                "asset": asset,
                "amount": api_amount(amount),
                "spend_id": spend_id,
                "comment": comment[:1024],
            },
            retry_reads=False,
        )
        return Transfer.model_validate(result)

    async def find_transfer(self, spend_id: str) -> Transfer | None:
        result = await self._call("getTransfers", {"spend_id": spend_id, "count": 1}, retry_reads=True)
        items = result.get("items", []) if isinstance(result, dict) else result
        return Transfer.model_validate(items[0]) if items else None

    async def get_balance(self) -> list[Balance]:
        result = await self._call("getBalance", {}, retry_reads=True)
        return [Balance.model_validate(item) for item in result]
