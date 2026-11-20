"""What the ChainWatcher needs from a network adapter, and what adapters report.

Positions are integers in the network's scan order: EVM block number, TRON block timestamp (ms),
TON logical time, Solana slot. A scan covers ``(after, upto]``. ``final=True`` means every
transfer returned is irreversible by the network's rules (finalized block, solidified block,
masterchain, ``finalized`` commitment); only those are ever credited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.domain.enums import Network


class ChainUnavailable(Exception):
    """No endpoint could answer (all failing, rate limited or lagging): try again later."""


@dataclass(frozen=True)
class ChainHead:
    latest: int
    final: int


@dataclass(frozen=True)
class ObservedTransfer:
    """One incoming USDT transfer to one of our addresses, as one source reports it."""

    network: Network
    tx_hash: str
    event_index: int  # log index / position of the transfer inside the transaction
    position: int
    token_contract: str  # always the whitelisted contract / master / mint (adapters filter)
    to_address: str  # our address, normalized
    amount: int  # minor units
    from_address: str | None = None
    block_number: int | None = None
    block_hash: str | None = None
    block_time: datetime | None = None
    memo: str | None = None  # TON comment / Solana memo
    references: tuple[str, ...] = ()  # Solana Pay references among the transaction's accounts
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def key(self) -> tuple[str, int]:
        return (self.tx_hash, self.event_index)


class ChainAdapter(Protocol):
    network: Network
    max_span: int  # the most positions one tick scans (keeps transactions and RPC calls small)

    @property
    def active_endpoint(self) -> str | None: ...

    @property
    def addresses(self) -> tuple[str, ...]:
        """Our deposit addresses on this network (normalized)."""
        ...

    async def head(self) -> ChainHead: ...

    async def scan(self, after: int, upto: int, *, final: bool) -> list[ObservedTransfer]: ...

    async def verify(self, transfer: ObservedTransfer) -> bool:
        """Confirm a final transfer with an independent source. False = the source disagrees
        (never credit); raises ChainUnavailable when it cannot tell right now."""
        ...

    async def lookup_tx(self, tx_hash: str) -> list[ObservedTransfer]:
        """Final USDT transfers to our addresses in one transaction (empty if none / not final)."""
        ...

    async def token_balance(self, address: str) -> int:
        """USDT balance of an address at the final head (for reconciliation)."""
        ...

    async def close(self) -> None: ...
