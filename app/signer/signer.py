"""Signer: builds and signs payout transactions from the payout row itself.

The caller (the payout worker) passes only the payout id and chain parameters (nonce, fees, gas):
the recipient and the amount are read here from the database, so a compromised worker cannot
make the signer pay someone else. Before signing, the signer re-checks on its own:

* the payout is executable (queued / retryable) and has every approval it needs;
* the recipient is a valid address of that network and not one of our own wallets;
* the amount fits the per-transaction limit and the rolling 24-hour limit;
* it holds the key of the hot wallet.

Keys are Ethereum keystore v3 files decrypted at start; they live only in this object's memory,
are never logged and never leave it. Running the signer as a separate, network-isolated process
(docs §9.2) only changes how this class is reached, not what it checks.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.addresses import validate_address
from app.config import ChainsConfig, Settings
from app.db.models import Payout, PayoutApproval, PayoutAttempt
from app.domain.enums import ApprovalDecision, Network, PayoutStatus
from app.domain.errors import DomainError
from app.domain.money import to_minor
from app.logging import get_logger

log = get_logger(__name__)

TRANSFER_SELECTOR = "a9059cbb"
SIGNABLE = frozenset({PayoutStatus.QUEUED, PayoutStatus.FAILED_RETRYABLE})


class SignerRefused(DomainError):
    code = "signer_refused"


@dataclass(frozen=True)
class EvmFees:
    gas: int
    gas_price: int | None = None  # legacy (BSC)
    max_fee: int | None = None  # EIP-1559 (Ethereum)
    max_priority_fee: int | None = None


@dataclass(frozen=True)
class SignedTx:
    raw: bytes
    tx_hash: str
    nonce: int

    def __repr__(self) -> str:  # never print raw transactions in logs
        return f"SignedTx(tx_hash={self.tx_hash!r}, nonce={self.nonce})"


class Signer:
    def __init__(
        self,
        keys: Mapping[str, LocalAccount],
        settings: Settings,
        chains: ChainsConfig,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._keys = {address.lower(): account for address, account in keys.items()}
        self._settings = settings
        self._chains = chains
        self._clock = clock

    def __repr__(self) -> str:
        return f"Signer(wallets={len(self._keys)})"

    @classmethod
    def from_keystore(
        cls, settings: Settings, chains: ChainsConfig, *, clock: Callable[[], datetime]
    ) -> Signer:
        directory = Path(settings.signer_keystore_dir) if settings.signer_keystore_dir else None
        keys: dict[str, LocalAccount] = {}
        if directory is not None and directory.is_dir():
            password = settings.signer_password.get_secret_value()
            for path in sorted(directory.glob("*.json")):
                account: LocalAccount = Account.from_key(
                    Account.decrypt(json.loads(path.read_text()), password)
                )
                keys[account.address] = account
        log.info("signer_loaded", wallets=len(keys))  # addresses only via has_key; never key material
        return cls(keys, settings, chains, clock=clock)

    def has_key(self, address: str) -> bool:
        return address.lower() in self._keys

    async def sign_evm_transfer(
        self, session: AsyncSession, payout_id: int, *, from_address: str, nonce: int, fees: EvmFees
    ) -> SignedTx:
        payout = await session.get(Payout, payout_id)
        if payout is None:
            raise SignerRefused("unknown payout", payout_id=payout_id)
        network = payout.network
        if network not in (Network.BSC, Network.ETH):
            raise SignerRefused("not an EVM payout", payout_id=payout_id)
        cfg = self._chains.networks[network]
        if not cfg.chain_id:
            raise SignerRefused("chain_id is not configured", network=network)
        recipient = await self._check(session, payout, from_address)
        account = self._keys[from_address.lower()]
        tx: dict[str, Any] = {
            "to": to_checksum_address(cfg.contract),
            "value": 0,
            "data": "0x"
            + TRANSFER_SELECTOR
            + recipient.lower().removeprefix("0x").rjust(64, "0")
            + f"{payout.amount:064x}",
            "nonce": nonce,
            "gas": fees.gas,
            "chainId": cfg.chain_id,
        }
        if fees.max_fee is not None:
            tx |= {
                "maxFeePerGas": fees.max_fee,
                "maxPriorityFeePerGas": fees.max_priority_fee or 0,
                "type": 2,
            }
        else:
            tx["gasPrice"] = fees.gas_price or 0
        signed = account.sign_transaction(tx)
        tx_hash = "0x" + bytes(signed.hash).hex()
        log.info("signer_signed", payout_id=payout.id, network=network.value, nonce=nonce, tx_hash=tx_hash)
        return SignedTx(raw=bytes(signed.raw_transaction), tx_hash=tx_hash, nonce=nonce)

    async def _check(self, session: AsyncSession, payout: Payout, from_address: str) -> str:
        if payout.status not in SIGNABLE:
            raise SignerRefused("payout is not executable", status=payout.status.value)
        if payout.approvals_required:
            approvals = await session.scalar(
                sa.select(sa.func.count())
                .select_from(PayoutApproval)
                .where(
                    PayoutApproval.payout_id == payout.id, PayoutApproval.decision == ApprovalDecision.APPROVE
                )
            )
            if int(approvals or 0) < payout.approvals_required:
                raise SignerRefused("payout lacks approvals", payout_id=payout.id)
        if not payout.recipient_address:
            raise SignerRefused("payout has no recipient address", payout_id=payout.id)
        recipient = validate_address(
            payout.network, payout.recipient_address, allow_testnet=self._settings.testnet_mode
        )
        ours = {self._chains.networks[n].deposit_address.lower() for n in self._chains.networks} | set(
            self._keys
        )
        if recipient.lower() in ours:
            raise SignerRefused("refusing to pay one of our own wallets", payout_id=payout.id)
        if not self.has_key(from_address):
            raise SignerRefused("no key for this hot wallet", wallet=from_address)
        decimals = self._chains.spec(payout.network, include_disabled=True).decimals
        if payout.amount > to_minor(self._settings.signer_max_payout_usdt, decimals):
            raise SignerRefused("amount exceeds the signer's per-transaction limit", payout_id=payout.id)
        await self._check_daily_limit(session, payout, decimals)
        return recipient

    async def _check_daily_limit(self, session: AsyncSession, payout: Payout, decimals: int) -> None:
        """Rolling 24 h over everything signed, in USDT cents (networks have different decimals)."""
        since = sa.func.now() - timedelta(hours=24)  # attempts carry database time
        rows = await session.execute(
            sa.select(Payout.network, Payout.amount).where(
                Payout.id.in_(sa.select(PayoutAttempt.payout_id).where(PayoutAttempt.created_at >= since)),
                Payout.id != payout.id,
            )
        )
        cents = 0
        for network, amount in rows.all():
            cents += amount // 10 ** (self._chains.spec(network, include_disabled=True).decimals - 2)
        cents += payout.amount // 10 ** (decimals - 2)
        if cents > int(self._settings.signer_daily_limit_usdt * 100):
            raise SignerRefused("the signer's 24-hour limit is reached", payout_id=payout.id)
