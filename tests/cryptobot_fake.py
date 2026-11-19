"""In-memory Crypto Pay API served through httpx.MockTransport (no network)."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from collections import defaultdict
from typing import Any

import httpx

TOKEN = "123:test-cryptopay-token"


class _ApiError(Exception):
    """Raised by a fake API method → returned as ``{"ok": false, "error": {...}}``."""


class FakeCryptoPay:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.invoices: dict[int, dict[str, Any]] = {}
        self.transfers: dict[str, dict[str, Any]] = {}  # by spend_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, list[str | Exception]] = defaultdict(list)  # method → queued failures
        self.timeout_after_transfer = False
        self._ids = itertools.count(9001)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    # ── server side ─────────────────────────────────────────────────────
    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Crypto-Pay-API-Token") != self.token:
            return httpx.Response(401, json={"ok": False, "error": {"code": 401, "name": "UNAUTHORIZED"}})
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        if self.errors[method]:
            failure = self.errors[method].pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(400, json={"ok": False, "error": {"code": 400, "name": failure}})
        handler = getattr(self, f"_{method}")
        try:
            result = handler(params)
        except _ApiError as exc:
            return httpx.Response(400, json={"ok": False, "error": {"code": 400, "name": str(exc)}})
        return httpx.Response(200, json={"ok": True, "result": result})

    def _createInvoice(self, p: dict[str, Any]) -> dict[str, Any]:
        invoice_id = next(self._ids)
        invoice = {
            "invoice_id": invoice_id,
            "hash": f"IV{invoice_id}",
            "currency_type": p["currency_type"],
            "asset": p["asset"],
            "amount": p["amount"],
            "status": "active",
            "payload": p.get("payload"),
            "description": p.get("description"),
            "bot_invoice_url": f"https://t.me/CryptoTestnetBot?start=IV{invoice_id}",
            "allow_comments": p.get("allow_comments"),
            "allow_anonymous": p.get("allow_anonymous"),
            "created_at": "2026-03-01T10:00:00.000Z",
        }
        self.invoices[invoice_id] = invoice
        return invoice

    def _getInvoices(self, p: dict[str, Any]) -> dict[str, Any]:
        ids = {int(x) for x in str(p.get("invoice_ids", "")).split(",") if x}
        items = [i for i in self.invoices.values() if not ids or i["invoice_id"] in ids]
        if p.get("status"):
            items = [i for i in items if i["status"] == p["status"]]
        return {"items": items}

    def _deleteInvoice(self, p: dict[str, Any]) -> bool:
        invoice = self.invoices[int(p["invoice_id"])]
        if invoice["status"] == "paid":
            raise AssertionError("tests never delete paid invoices")
        invoice["status"] = "deleted"
        return True

    def _transfer(self, p: dict[str, Any]) -> dict[str, Any]:
        spend_id = p["spend_id"]
        if spend_id in self.transfers:
            raise _ApiError("SPEND_ID_ALREADY_USED")
        transfer = {
            "transfer_id": next(self._ids),
            "spend_id": spend_id,
            "user_id": p["user_id"],
            "asset": p["asset"],
            "amount": p["amount"],
            "status": "completed",
            "completed_at": "2026-03-01T10:00:00.000Z",
            "comment": p.get("comment"),
        }
        self.transfers[spend_id] = transfer
        if self.timeout_after_transfer:
            self.timeout_after_transfer = False
            raise httpx.ReadTimeout("response lost after the transfer was executed")
        return transfer

    def _getTransfers(self, p: dict[str, Any]) -> dict[str, Any]:
        found = self.transfers.get(p.get("spend_id", ""))
        return {"items": [found] if found else []}

    def _getBalance(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"currency_code": "USDT", "available": "1000", "onhold": "0"}]

    # ── test controls ───────────────────────────────────────────────────
    def pay(
        self, invoice_id: int, *, amount: str | None = None, fee: str | None = None, asset: str | None = None
    ) -> dict[str, Any]:
        invoice = self.invoices[invoice_id]
        invoice.update(
            status="paid",
            paid_asset=asset or invoice["asset"],
            paid_amount=amount or invoice["amount"],
            paid_at="2026-03-01T10:05:00.000Z",
        )
        if fee is not None:
            invoice.update(fee_asset="USDT", fee_amount=fee)
        return invoice

    def webhook(self, invoice: dict[str, Any], *, update_id: int = 1) -> tuple[bytes, str]:
        body = json.dumps(
            {
                "update_id": update_id,
                "update_type": "invoice_paid",
                "request_date": "2026-03-01T10:05:01.000Z",
                "payload": invoice,
            }
        ).encode()
        secret = hashlib.sha256(self.token.encode()).digest()
        return body, hmac.new(secret, body, hashlib.sha256).hexdigest()
