"""The signer as a separate process: an HTTP service on an internal network, and its client.

Requests are authenticated with HMAC-SHA256 over ``timestamp.body`` (shared secret), must be at
most ``MAX_SKEW`` old and are accepted once (replay cache), so a captured request cannot be
re-sent. The server reads the payout from the database itself — the request carries only the
payout id and chain parameters — and applies every check of :class:`Signer`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chains.tron_tx import RefBlock
from app.logging import get_logger
from app.signer.signer import EvmFees, SignedTx, Signer, SignerRefused

log = get_logger(__name__)

SIGNATURE_HEADER = "X-Signer-Signature"
TIMESTAMP_HEADER = "X-Signer-Timestamp"
MAX_SKEW = 30.0


class SignerUnavailable(RuntimeError):
    """The signer did not answer (or answered nonsense): retry later, never guess."""


def request_signature(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# ── server ───────────────────────────────────────────────────────────────────


def build_app(
    signer: Signer,
    sessionmaker: async_sessionmaker[AsyncSession],
    secret: bytes,
    *,
    clock: Callable[[], float] = time.time,
) -> web.Application:
    if len(secret) < 32:
        raise ValueError("SIGNER_API_SECRET must be at least 32 bytes")
    seen: dict[str, float] = {}

    @web.middleware
    async def authenticate(request: web.Request, handler: Any) -> web.StreamResponse:
        body = await request.read()
        timestamp = request.headers.get(TIMESTAMP_HEADER, "")
        given = request.headers.get(SIGNATURE_HEADER, "")
        now = clock()
        try:
            fresh = abs(now - float(timestamp)) <= MAX_SKEW
        except ValueError:
            fresh = False
        expected = request_signature(secret, timestamp, body)
        if not fresh or not hmac.compare_digest(given, expected):
            log.warning("signer_auth_failed", path=request.path)
            return web.json_response({"error": "unauthorized"}, status=401)
        for key, at in list(seen.items()):
            if now - at > 2 * MAX_SKEW:
                del seen[key]
        if given in seen:
            return web.json_response({"error": "replayed request"}, status=409)
        seen[given] = now
        return await handler(request)  # type: ignore[no-any-return]

    async def wallets(request: web.Request) -> web.Response:
        return web.json_response({"wallets": signer.wallets})

    async def sign(request: web.Request) -> web.Response:
        body = json.loads(await request.read())
        kind = request.match_info["kind"]
        try:
            async with sessionmaker() as session, session.begin():
                if kind == "evm":
                    signed = await signer.sign_evm_transfer(
                        session,
                        int(body["payout_id"]),
                        from_address=str(body["from_address"]),
                        nonce=int(body["nonce"]),
                        fees=EvmFees(**body["fees"]),
                    )
                elif kind == "tron":
                    signed = await signer.sign_tron_transfer(
                        session,
                        int(body["payout_id"]),
                        from_address=str(body["from_address"]),
                        ref=RefBlock(**body["ref"]),
                        now_ms=int(body["now_ms"]),
                        fee_limit_sun=int(body["fee_limit_sun"]),
                    )
                else:
                    return web.json_response({"error": "unknown network kind"}, status=404)
        except SignerRefused as exc:
            return web.json_response({"error": exc.message}, status=422)
        return web.json_response(
            {
                "raw": signed.raw.hex(),
                "tx_hash": signed.tx_hash,
                "nonce": signed.nonce,
                "valid_until": signed.valid_until.isoformat() if signed.valid_until else None,
            }
        )

    app = web.Application(middlewares=[authenticate], client_max_size=64 * 1024)
    app.router.add_get("/v1/wallets", wallets)
    app.router.add_post("/v1/sign/{kind}", sign)
    return app


# ── client ───────────────────────────────────────────────────────────────────


class RemoteSigner:
    """Same interface as :class:`Signer`; ``session`` is unused (the signer reads the DB itself)."""

    def __init__(
        self,
        url: str,
        secret: bytes,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=url, transport=transport, timeout=15.0, trust_env=False)
        self._secret = secret
        self._clock = clock
        self._wallets: set[str] = set()

    async def load(self) -> None:
        body = await self._call("GET", "/v1/wallets", b"")
        self._wallets = {w.lower() for w in body["wallets"]}

    def has_key(self, address: str) -> bool:
        return address.lower() in self._wallets

    async def sign_evm_transfer(
        self, session: AsyncSession, payout_id: int, *, from_address: str, nonce: int, fees: EvmFees
    ) -> SignedTx:
        payload = {
            "payout_id": payout_id,
            "from_address": from_address,
            "nonce": nonce,
            "fees": fees.__dict__,
        }
        return _signed(await self._call("POST", "/v1/sign/evm", json.dumps(payload).encode()))

    async def sign_tron_transfer(
        self,
        session: AsyncSession,
        payout_id: int,
        *,
        from_address: str,
        ref: RefBlock,
        now_ms: int,
        fee_limit_sun: int,
    ) -> SignedTx:
        payload = {
            "payout_id": payout_id,
            "from_address": from_address,
            "ref": {"number": ref.number, "block_id": ref.block_id},
            "now_ms": now_ms,
            "fee_limit_sun": fee_limit_sun,
        }
        return _signed(await self._call("POST", "/v1/sign/tron", json.dumps(payload).encode()))

    async def _call(self, method: str, path: str, body: bytes) -> dict[str, Any]:
        timestamp = f"{self._clock():.3f}"
        headers = {
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: request_signature(self._secret, timestamp, body),
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.request(method, path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SignerUnavailable(f"signer is unreachable: {type(exc).__name__}") from exc
        if response.status_code == 422:
            raise SignerRefused(str(response.json().get("error", "refused")))
        if response.status_code != 200:
            raise SignerUnavailable(f"signer answered HTTP {response.status_code}")
        result: dict[str, Any] = response.json()
        return result

    async def close(self) -> None:
        await self._client.aclose()


def _signed(body: dict[str, Any]) -> SignedTx:
    valid_until = body.get("valid_until")
    return SignedTx(
        raw=bytes.fromhex(body["raw"]),
        tx_hash=str(body["tx_hash"]),
        nonce=body.get("nonce"),
        valid_until=datetime.fromisoformat(valid_until) if valid_until else None,
    )
