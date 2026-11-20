"""BEP-20 (BSC) and ERC-20 (Ethereum) USDT over plain JSON-RPC.

* incoming transfers: ``eth_getLogs`` of the whitelisted USDT contract, ``Transfer`` topic,
  recipient topic = our addresses. Logs of any other contract (fake USDT) are never read;
* final head = min(``finalized`` block tag, latest − ``min_confirmations``);
* the second source (a different provider) must return a successful receipt in the same block
  with the very same log, and must itself consider the block final;
* ``getLogs`` ranges shrink by half when an endpoint refuses a range and grow back on success;
* decimals are checked against the contract at start: BSC USDT has 18, Ethereum USDT 6.
"""

from __future__ import annotations

from typing import Any

from eth_utils.address import to_checksum_address

from app.chains.base import ChainHead, ChainUnavailable, ObservedTransfer
from app.chains.pool import EndpointPool, RpcError
from app.config import NetworkConfig
from app.domain.enums import Network
from app.logging import get_logger

log = get_logger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BALANCE_OF = "0x70a08231"
DECIMALS = "0x313ce567"
DEFAULT_RANGE = 2000
MIN_RANGE = 1


def pad_address(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def topic_address(topic: str) -> str:
    return str(to_checksum_address("0x" + topic[-40:]))


class EvmAdapter:
    def __init__(
        self,
        network: Network,
        cfg: NetworkConfig,
        *,
        addresses: tuple[str, ...],
        primary: EndpointPool,
        verifier: EndpointPool,
    ) -> None:
        if cfg.finality is None:
            raise ValueError(f"{network}: finality is required")
        self.network = network
        self._cfg = cfg
        self._contract = str(to_checksum_address(cfg.contract))
        self._addresses = tuple(str(to_checksum_address(a)) for a in addresses)
        self._ours = {a.lower() for a in self._addresses}
        self._primary = primary
        self._verifier = verifier
        limits = [ep.max_block_range for ep in primary.endpoints if ep.max_block_range]
        self._range = min([DEFAULT_RANGE, *limits])
        self.max_span = max(self._range * 5, 1)

    @property
    def active_endpoint(self) -> str | None:
        return self._primary.active

    @property
    def addresses(self) -> tuple[str, ...]:
        return self._addresses

    async def check_contract(self) -> None:
        """Refuse to start when the configured decimals do not match the contract."""
        raw = await self._primary.json_rpc("eth_call", [{"to": self._contract, "data": DECIMALS}, "latest"])
        actual = int(raw, 16)
        if actual != self._cfg.decimals:
            raise RuntimeError(
                f"{self.network}: USDT decimals are {actual}, chains.yaml says {self._cfg.decimals}"
            )

    async def head(self) -> ChainHead:
        return await self._head(self._primary)

    async def _head(self, pool: EndpointPool) -> ChainHead:
        finality = self._cfg.finality
        assert finality is not None  # noqa: S101 - checked in __init__
        latest = int(await pool.json_rpc("eth_blockNumber", []), 16)
        final = latest - finality.min_confirmations
        if finality.kind == "finalized_tag":
            block = await pool.json_rpc("eth_getBlockByNumber", ["finalized", False])
            if not block:
                raise ChainUnavailable("the endpoint does not know a finalized block")
            final = min(final, int(block["number"], 16))
        return ChainHead(latest=latest, final=max(final, 0))

    async def scan(self, after: int, upto: int, *, final: bool) -> list[ObservedTransfer]:
        found: list[ObservedTransfer] = []
        start = after + 1
        while start <= upto:
            end = min(upto, start + self._range - 1)
            try:
                logs = await self._primary.json_rpc(
                    "eth_getLogs",
                    [
                        {
                            "fromBlock": hex(start),
                            "toBlock": hex(end),
                            "address": self._contract,
                            "topics": [TRANSFER_TOPIC, None, [pad_address(a) for a in self._addresses]],
                        }
                    ],
                )
            except RpcError as exc:
                if not exc.range_too_large or self._range <= MIN_RANGE:
                    raise ChainUnavailable(f"eth_getLogs failed: {exc.message[:120]}") from exc
                self._range = max(self._range // 2, MIN_RANGE)
                log.info("evm_getlogs_range_shrunk", network=self.network.value, range=self._range)
                continue
            found += [t for t in (self._parse(entry) for entry in logs) if t is not None]
            start = end + 1
            if self._range < DEFAULT_RANGE:
                self._range = min(self._range * 2, DEFAULT_RANGE)
        return found

    def _parse(self, entry: dict[str, Any]) -> ObservedTransfer | None:
        topics = entry.get("topics") or []
        if (
            entry.get("removed")
            or str(entry.get("address", "")).lower() != self._contract.lower()  # never trust the filter alone
            or len(topics) != 3
            or str(topics[0]).lower() != TRANSFER_TOPIC
        ):
            return None
        to = topic_address(topics[2])
        if to.lower() not in self._ours:
            return None
        amount = int(entry.get("data") or "0x0", 16)
        if amount <= 0:
            return None
        block = int(entry["blockNumber"], 16)
        return ObservedTransfer(
            network=self.network,
            tx_hash=str(entry["transactionHash"]).lower(),
            event_index=int(entry["logIndex"], 16),
            position=block,
            token_contract=self._contract,
            to_address=to,
            amount=amount,
            from_address=topic_address(topics[1]),
            block_number=block,
            block_hash=str(entry.get("blockHash", "")).lower() or None,
            raw={"log": entry},
        )

    async def verify(self, transfer: ObservedTransfer) -> bool:
        head = await self._head(self._verifier)
        if transfer.block_number is None or transfer.block_number > head.final:
            raise ChainUnavailable("the second source does not consider the block final yet")
        receipt = await self._verifier.json_rpc("eth_getTransactionReceipt", [transfer.tx_hash])
        if not receipt:
            raise ChainUnavailable("the second source does not know the transaction yet")
        if receipt.get("status") != "0x1":
            return False
        if transfer.block_hash and str(receipt.get("blockHash", "")).lower() != transfer.block_hash:
            return False
        for entry in receipt.get("logs") or []:
            if int(entry.get("logIndex", "0x0"), 16) != transfer.event_index:
                continue
            parsed = self._parse(
                {**entry, "blockNumber": entry.get("blockNumber", hex(transfer.block_number))}
            )
            return (
                parsed is not None
                and parsed.amount == transfer.amount
                and parsed.to_address.lower() == transfer.to_address.lower()
            )
        return False

    async def token_balance(self, address: str) -> int:
        head = await self.head()
        raw = await self._primary.json_rpc(
            "eth_call",
            [{"to": self._contract, "data": BALANCE_OF + pad_address(address)[2:]}, hex(head.final)],
        )
        return int(raw, 16)

    async def close(self) -> None:
        await self._primary.close()
        await self._verifier.close()
