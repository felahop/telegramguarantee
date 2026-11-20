"""SPL USDT on Solana over JSON-RPC.

* our token accounts for the whitelisted mint are discovered with ``getTokenAccountsByOwner``
  (the token program sets the mint, so a fake token cannot pose as USDT); every scan refreshes
  the list, because anyone may open another token account owned by our address;
* positions are slots; final = ``finalized`` commitment, latest = ``confirmed``;
* a transfer is the increase of one of our token accounts in a successful transaction
  (``meta.err = null``, post − pre token balance for the USDT mint);
* Solana Pay references are the transaction's account keys; the memo comes from the memo program;
* the second source (another RPC) must report the same increase in the same slot, finalized.
"""

from __future__ import annotations

from typing import Any

from app.chains.base import ChainHead, ChainUnavailable, ObservedTransfer
from app.chains.pool import EndpointPool
from app.config import NetworkConfig
from app.domain.enums import Network
from app.logging import get_logger

log = get_logger(__name__)

SIGNATURES_LIMIT = 1000
MAX_PAGES = 20
SPAN_SLOTS = 1500  # ≈ 10 minutes of slots per tick
MEMO_PROGRAMS = {
    "spl-memo",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}


class SolanaAdapter:
    network = Network.SOL
    max_span = SPAN_SLOTS

    def __init__(
        self, cfg: NetworkConfig, *, addresses: tuple[str, ...], primary: EndpointPool, verifier: EndpointPool
    ) -> None:
        self._mint = cfg.contract
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
        latest = await self._primary.json_rpc("getSlot", [{"commitment": "confirmed"}])
        final = await self._primary.json_rpc("getSlot", [{"commitment": "finalized"}])
        return ChainHead(latest=int(latest), final=int(final))

    async def token_accounts(self, owner: str, pool: EndpointPool | None = None) -> list[str]:
        result = await (pool or self._primary).json_rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": self._mint}, {"encoding": "jsonParsed", "commitment": "finalized"}],
        )
        accounts = []
        for item in result.get("value") or []:
            info = (((item.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            if info.get("mint") == self._mint and info.get("owner") == owner:
                accounts.append(str(item["pubkey"]))
        return accounts

    async def scan(self, after: int, upto: int, *, final: bool) -> list[ObservedTransfer]:
        commitment = "finalized" if final else "confirmed"
        found: dict[tuple[str, int], ObservedTransfer] = {}
        for owner in self._addresses:
            accounts = await self.token_accounts(owner)
            for account in accounts:
                for signature, _slot in await self._signatures(account, after, upto, commitment):
                    tx = await self._primary.json_rpc(
                        "getTransaction",
                        [
                            signature,
                            {
                                "encoding": "jsonParsed",
                                "commitment": commitment,
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    )
                    if not tx:
                        continue  # not visible at this commitment yet: the next tick sees it
                    transfer = self._parse(tx, signature, owner, set(accounts))
                    if transfer is not None and after < transfer.position <= upto:
                        found[transfer.key] = transfer
        return sorted(found.values(), key=lambda t: (t.position, t.key))

    async def _signatures(
        self, account: str, after: int, upto: int, commitment: str
    ) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        before: str | None = None
        for _ in range(MAX_PAGES):
            options: dict[str, Any] = {"limit": SIGNATURES_LIMIT, "commitment": commitment}
            if before:
                options["before"] = before
            page = await self._primary.json_rpc("getSignaturesForAddress", [account, options])
            if not page:
                return out
            for entry in page:  # newest first
                slot = int(entry["slot"])
                if slot <= after:
                    return out
                if slot <= upto and entry.get("err") is None:
                    out.append((str(entry["signature"]), slot))
            before = str(page[-1]["signature"])
            if len(page) < SIGNATURES_LIMIT:
                return out
        raise ChainUnavailable("too many signatures in one range; the next tick continues")

    def _parse(
        self, tx: dict[str, Any], signature: str, owner: str, accounts: set[str]
    ) -> ObservedTransfer | None:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None
        message = (tx.get("transaction") or {}).get("message") or {}
        keys = [k["pubkey"] if isinstance(k, dict) else str(k) for k in message.get("accountKeys") or []]

        def balances(name: str) -> dict[int, int]:
            out: dict[int, int] = {}
            for b in meta.get(name) or []:
                index = int(b["accountIndex"])
                if b.get("mint") == self._mint and index < len(keys) and keys[index] in accounts:
                    out[index] = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            return out

        pre, post = balances("preTokenBalances"), balances("postTokenBalances")
        increase = sum(post.get(i, 0) - pre.get(i, 0) for i in set(pre) | set(post))
        if increase <= 0:
            return None
        memo = None
        for ix in message.get("instructions") or []:
            if ix.get("program") in MEMO_PROGRAMS or ix.get("programId") in MEMO_PROGRAMS:
                parsed = ix.get("parsed")
                memo = parsed if isinstance(parsed, str) else memo
        return ObservedTransfer(
            network=self.network,
            tx_hash=signature,
            event_index=0,
            position=int(tx["slot"]),
            token_contract=self._mint,
            to_address=owner,
            amount=increase,
            from_address=keys[0] if keys else None,
            block_number=int(tx["slot"]),
            memo=memo,
            references=tuple(keys),
            raw={"signature": signature, "slot": tx["slot"]},
        )

    async def lookup_tx(self, signature: str) -> list[ObservedTransfer]:
        """The increase of our USDT accounts in one finalized transaction (tx-hash claim)."""
        tx = await self._primary.json_rpc(
            "getTransaction",
            [
                signature,
                {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0},
            ],
        )
        if not tx:
            return []
        found = []
        for owner in self._addresses:
            transfer = self._parse(tx, signature, owner, set(await self.token_accounts(owner)))
            if transfer is not None:
                found.append(transfer)
        return found

    async def verify(self, transfer: ObservedTransfer) -> bool:
        tx = await self._verifier.json_rpc(
            "getTransaction",
            [
                transfer.tx_hash,
                {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0},
            ],
        )
        if not tx:
            raise ChainUnavailable("the second source has not finalized the transaction yet")
        accounts = set(await self.token_accounts(transfer.to_address, self._verifier))
        again = self._parse(tx, transfer.tx_hash, transfer.to_address, accounts)
        return again is not None and again.amount == transfer.amount and again.position == transfer.position

    async def token_balance(self, address: str) -> int:
        total = 0
        for account in await self.token_accounts(address):
            result = await self._primary.json_rpc(
                "getTokenAccountBalance", [account, {"commitment": "finalized"}]
            )
            total += int(result["value"]["amount"])
        return total

    async def close(self) -> None:
        await self._primary.close()
        await self._verifier.close()
