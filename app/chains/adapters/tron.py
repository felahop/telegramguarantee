"""TRC-20 USDT through the TronGrid HTTP API (free key) and an independent full node.

* positions are block timestamps in ms: TronGrid filters account transfers by time;
* incoming transfers: ``/v1/accounts/{our address}/transactions/trc20`` for the whitelisted
  contract only, ``only_confirmed=true`` (solidified) for the final scan;
* final head = the solidified block (``/walletsolidity/getnowblock``);
* the second source (another node) must return the solidified transaction info with
  ``receipt.result = SUCCESS`` and a ``Transfer`` log of the USDT contract to our address with the
  same amount, in the same block.
"""

from __future__ import annotations

from typing import Any

import base58

from app.chains.base import ChainHead, ChainUnavailable, ObservedTransfer
from app.chains.pool import EndpointPool
from app.chains.tron_tx import RefBlock
from app.config import NetworkConfig
from app.domain.enums import Network
from app.logging import get_logger

log = get_logger(__name__)

TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PAGE_LIMIT = 200
MAX_PAGES = 50
SPAN_MS = 10 * 60 * 1000  # one tick covers at most ten minutes of blocks


def tron_hex(address: str) -> str:
    """Base58check T-address → the 20-byte body as hex (how logs and ABI parameters carry it)."""
    payload = base58.b58decode_check(address)
    if len(payload) != 21 or payload[0] != 0x41:
        raise ValueError("not a TRON address")
    return payload[1:].hex()


def tron_base58(hex20: str) -> str:
    return str(base58.b58encode_check(b"\x41" + bytes.fromhex(hex20[-40:])).decode())


class TronAdapter:
    network = Network.TRON
    max_span = SPAN_MS

    def __init__(
        self, cfg: NetworkConfig, *, addresses: tuple[str, ...], primary: EndpointPool, verifier: EndpointPool
    ) -> None:
        self._cfg = cfg
        self._contract = cfg.contract
        self._contract_hex = tron_hex(cfg.contract)
        self._addresses = addresses
        self._primary = primary
        self._verifier = verifier

    @property
    def active_endpoint(self) -> str | None:
        return self._primary.active

    @property
    def addresses(self) -> tuple[str, ...]:
        return self._addresses

    async def head(self) -> ChainHead:
        latest = await self._primary.post("/wallet/getnowblock", {})
        solid = await self._primary.post("/walletsolidity/getnowblock", {})
        return ChainHead(latest=_block_ts(latest), final=_block_ts(solid))

    async def scan(self, after: int, upto: int, *, final: bool) -> list[ObservedTransfer]:
        found: list[ObservedTransfer] = []
        for address in self._addresses:
            found += await self._scan_address(address, after, upto, final=final)
        return sorted(found, key=lambda t: (t.position, t.key))

    async def _scan_address(
        self, address: str, after: int, upto: int, *, final: bool
    ) -> list[ObservedTransfer]:
        params: dict[str, Any] = {
            "only_to": "true",
            "contract_address": self._contract,
            "min_timestamp": after + 1,
            "max_timestamp": upto,
            "order_by": "block_timestamp,asc",
            "limit": PAGE_LIMIT,
        }
        if final:
            params["only_confirmed"] = "true"
        items: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            page = await self._primary.get(f"/v1/accounts/{address}/transactions/trc20", params)
            if not page.get("success", True):
                raise ChainUnavailable(f"TronGrid refused the request: {str(page.get('error'))[:120]}")
            items += page.get("data") or []
            fingerprint = (page.get("meta") or {}).get("fingerprint")
            if not fingerprint:
                break
            params["fingerprint"] = fingerprint
        else:
            raise ChainUnavailable("too many transfers in one range; the next tick continues")
        ordinal: dict[str, int] = {}
        found: list[ObservedTransfer] = []
        for item in items:
            transfer = self._parse(item, address, ordinal)
            if transfer is not None:
                found.append(transfer)
        return found

    def _parse(self, item: dict[str, Any], address: str, ordinal: dict[str, int]) -> ObservedTransfer | None:
        token = item.get("token_info") or {}
        if (
            token.get("address") != self._contract
            or item.get("to") != address
            or item.get("type") != "Transfer"
        ):
            return None  # another token (a fake "USDT") or not to us
        amount = int(item.get("value") or 0)
        if amount <= 0:
            return None
        tx = str(item["transaction_id"]).lower()
        index = ordinal.get(tx, 0)
        ordinal[tx] = index + 1
        return ObservedTransfer(
            network=self.network,
            tx_hash=tx,
            event_index=index,
            position=int(item["block_timestamp"]),
            token_contract=self._contract,
            to_address=address,
            amount=amount,
            from_address=item.get("from"),
            raw={"trongrid": item},
        )

    async def verify(self, transfer: ObservedTransfer) -> bool:
        info = await self._verifier.post(
            "/walletsolidity/gettransactioninfobyid", {"value": transfer.tx_hash}
        )
        if not info:
            raise ChainUnavailable("the second source has not solidified the transaction yet")
        if (info.get("receipt") or {}).get("result") != "SUCCESS":
            return False
        if info.get("blockTimeStamp") is not None and int(info["blockTimeStamp"]) != transfer.position:
            return False
        ours = tron_hex(transfer.to_address)
        matches = [
            entry
            for entry in info.get("log") or []
            if str(entry.get("address", "")).lower().removeprefix("41")[-40:] == self._contract_hex
            and len(entry.get("topics") or []) == 3
            and str(entry["topics"][0]).lower() == TRANSFER_TOPIC
            and str(entry["topics"][2]).lower()[-40:] == ours
            and int(entry.get("data") or "0", 16) == transfer.amount
        ]
        return len(matches) > transfer.event_index

    async def token_balance(self, address: str) -> int:
        result = await self._primary.post(
            "/wallet/triggerconstantcontract",
            {
                "owner_address": address,
                "contract_address": self._contract,
                "function_selector": "balanceOf(address)",
                "parameter": "0" * 24 + tron_hex(address),
                "visible": True,
            },
        )
        values = result.get("constant_result") or []
        if not values:
            raise ChainUnavailable("balanceOf returned nothing")
        return int(values[0], 16)

    # ── outgoing (used by the payout worker) ──────────────────────────────
    async def ref_block(self) -> RefBlock:
        """The solidified block as the reference block: it cannot be reorganized away."""
        block = await self._primary.post("/walletsolidity/getnowblock", {})
        return RefBlock(
            number=int(block["block_header"]["raw_data"]["number"]), block_id=str(block["blockID"])
        )

    async def chain_time_ms(self) -> int:
        return _block_ts(await self._primary.post("/wallet/getnowblock", {}))

    async def solid_time_ms(self) -> int:
        """The older of both sources' solidified heads (expiry is judged conservatively)."""
        a = _block_ts(await self._primary.post("/walletsolidity/getnowblock", {}))
        b = _block_ts(await self._verifier.post("/walletsolidity/getnowblock", {}))
        return min(a, b)

    async def native_balance(self, address: str) -> int:
        account = await self._primary.post("/wallet/getaccount", {"address": address, "visible": True})
        return int((account or {}).get("balance") or 0)

    async def broadcast(self, raw: bytes) -> bool:
        accepted = False
        for pool in (self._primary, self._verifier):
            for endpoint in pool.endpoints:
                try:
                    result = await pool.post_at(endpoint, "/wallet/broadcasthex", {"transaction": raw.hex()})
                except Exception as exc:
                    log.warning("tron_broadcast_failed", endpoint=endpoint.name, error=type(exc).__name__)
                    continue
                if result.get("result") or result.get("code") == "DUP_TRANSACTION_ERROR":
                    accepted = True
                else:
                    log.warning("tron_broadcast_rejected", endpoint=endpoint.name, code=result.get("code"))
        return accepted

    async def tx_info(self, tx_hash: str, *, second_source: bool = False) -> dict[str, Any] | None:
        pool = self._verifier if second_source else self._primary
        info = await pool.post("/walletsolidity/gettransactioninfobyid", {"value": tx_hash})
        return info or None

    def transfer_in_info(self, info: dict[str, Any], *, to: str, amount: int) -> bool:
        recipient = tron_hex(to)
        return any(
            str(entry.get("address", "")).lower().removeprefix("41")[-40:] == self._contract_hex
            and len(entry.get("topics") or []) == 3
            and str(entry["topics"][0]).lower() == TRANSFER_TOPIC
            and str(entry["topics"][2]).lower()[-40:] == recipient
            and int(entry.get("data") or "0", 16) == amount
            for entry in info.get("log") or []
        )

    async def close(self) -> None:
        await self._primary.close()
        await self._verifier.close()


def _block_ts(block: dict[str, Any]) -> int:
    try:
        return int(block["block_header"]["raw_data"]["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ChainUnavailable("unexpected block format") from exc
