"""In-memory chain nodes served through httpx.MockTransport (no network)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.chains.adapters.evm import TRANSFER_TOPIC, pad_address


@dataclass
class FakeEvmNode:
    """One provider's view of an EVM chain. Several hosts can share a chain and still disagree."""

    latest: int = 1000
    finalized: int = 990
    logs: list[dict[str, Any]] = field(default_factory=list)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    decimals: int = 18
    balances: dict[str, int] = field(default_factory=dict)
    max_range: int | None = None
    failures: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))  # method → HTTP codes
    calls: list[tuple[str, list[Any]]] = field(default_factory=list)
    chain_id: int = 56
    native: dict[str, int] = field(default_factory=dict)
    gas_price_wei: int = 3 * 10**9
    sent_nonces: int = 0
    mempool: Any = None
    reject_broadcast: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append((method, params))
        if self.failures[method]:
            return httpx.Response(self.failures[method].pop(0), text="upstream error")
        try:
            result = getattr(self, "_" + method)(params)
        except _RpcFail as exc:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": str(exc)}}
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def _eth_blockNumber(self, p: list[Any]) -> str:
        return hex(self.latest)

    def _eth_getBlockByNumber(self, p: list[Any]) -> dict[str, Any] | None:
        tag = p[0]
        number = {"finalized": self.finalized, "latest": self.latest}.get(tag) or int(tag, 16)
        return {"number": hex(number), "hash": f"0xblock{number}", "baseFeePerGas": hex(10**9)}

    def _eth_chainId(self, p: list[Any]) -> str:
        return hex(self.chain_id)

    def _eth_getTransactionCount(self, p: list[Any]) -> str:
        return hex(self.sent_nonces)

    def _eth_getBalance(self, p: list[Any]) -> str:
        return hex(self.native.get(p[0].lower(), 0))

    def _eth_gasPrice(self, p: list[Any]) -> str:
        return hex(self.gas_price_wei)

    def _eth_maxPriorityFeePerGas(self, p: list[Any]) -> str:
        return hex(10**9)

    def _eth_sendRawTransaction(self, p: list[Any]) -> str:
        from eth_utils import keccak  # noqa: PLC0415

        if self.reject_broadcast:
            raise _RpcFail("insufficient funds for gas * price + value")
        raw = bytes.fromhex(p[0][2:])
        tx_hash = "0x" + keccak(raw).hex()
        if self.mempool is not None:
            if tx_hash in self.mempool.pending or tx_hash in self.receipts:
                raise _RpcFail("already known")
            self.mempool.pending[tx_hash] = raw
        return tx_hash

    def _eth_getLogs(self, p: list[Any]) -> list[dict[str, Any]]:
        f = p[0]
        start, end = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        if self.max_range is not None and end - start + 1 > self.max_range:
            raise _RpcFail(f"block range is too large, max {self.max_range}")
        wanted = {t.lower() for t in f["topics"][2]}
        return [
            log
            for log in self.logs
            if start <= int(log["blockNumber"], 16) <= end
            and log["address"].lower() == f["address"].lower()
            and log["topics"][2].lower() in wanted
        ]

    def _eth_getTransactionReceipt(self, p: list[Any]) -> dict[str, Any] | None:
        return self.receipts.get(p[0])

    def _eth_call(self, p: list[Any]) -> str:
        data = p[0]["data"]
        if data.startswith("0x313ce567"):
            return hex(self.decimals)
        holder = "0x" + data[-40:]
        return hex(self.balances.get(holder.lower(), 0))

    # ── test controls ───────────────────────────────────────────────────
    def transfer(
        self,
        *,
        contract: str,
        to: str,
        amount: int,
        block: int,
        tx: str,
        index: int = 0,
        sender: str = "0x" + "ab" * 20,
        status: str = "0x1",
    ) -> dict[str, Any]:
        log = {
            "address": contract,
            "topics": [TRANSFER_TOPIC, pad_address(sender), pad_address(to)],
            "data": hex(amount),
            "blockNumber": hex(block),
            "blockHash": f"0xblock{block}",
            "transactionHash": tx,
            "logIndex": hex(index),
            "removed": False,
        }
        self.logs.append(log)
        receipt = self.receipts.setdefault(
            tx, {"status": status, "blockHash": f"0xblock{block}", "blockNumber": hex(block), "logs": []}
        )
        receipt["logs"].append(dict(log))
        return log


class _RpcFail(Exception):
    pass


def routing_transport(nodes: dict[str, Any]) -> httpx.MockTransport:
    """Route requests by host to fake nodes (``handle(request)``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        node = nodes.get(request.url.host)
        if node is None:
            return httpx.Response(502, text="unknown host")
        return node.handle(request)  # type: ignore[no-any-return]

    return httpx.MockTransport(handler)


@dataclass
class FakeTronNode:
    """TronGrid-like HTTP API: account TRC-20 history, blocks, solidified transaction info."""

    latest_ts: int = 1_700_000_100_000
    solid_ts: int = 1_700_000_040_000
    transfers: list[dict[str, Any]] = field(default_factory=list)  # TronGrid v1 items
    infos: dict[str, dict[str, Any]] = field(default_factory=dict)  # tx → solidified info
    balances: dict[str, int] = field(default_factory=dict)
    page_size: int = 200
    requests: list[httpx.Request] = field(default_factory=list)
    trx: dict[str, int] = field(default_factory=dict)  # address → sun
    sent: dict[str, bytes] = field(default_factory=dict)  # txid → raw_data, broadcast but not solidified

    def _broadcast(self, tx_hex: str) -> dict[str, Any]:
        import hashlib  # noqa: PLC0415

        fields = _proto(bytes.fromhex(tx_hex))
        raw = fields[1][0]
        txid = hashlib.sha256(raw).hexdigest()
        if txid in self.sent or txid in self.infos:
            return {"result": False, "code": "DUP_TRANSACTION_ERROR"}
        assert len(fields[2][0]) == 65  # r, s, v
        self.sent[txid] = raw
        return {"result": True, "txid": txid}

    def solidify(self, txid: str, *, result: str = "SUCCESS", fee: int = 13_000_000) -> dict[str, Any]:
        """Include a broadcast transaction in a solidified block; returns what it transferred."""
        from app.chains.adapters.tron import TRANSFER_TOPIC  # noqa: PLC0415

        raw = self.sent.pop(txid)
        contract = _proto(_proto(_proto(_proto(raw)[11][0])[2][0])[2][0])
        data = contract[4][0]
        recipient, amount = data[16:36].hex(), int.from_bytes(data[36:68], "big")
        token = contract[2][0][1:].hex()
        logs = [
            {
                "address": token,
                "topics": [TRANSFER_TOPIC, "0" * 64, "0" * 24 + recipient],
                "data": f"{amount:064x}",
            }
        ]
        self.infos[txid] = {
            "id": txid,
            "blockTimeStamp": self.solid_ts,
            "fee": fee,
            "receipt": {"result": result},
            "log": logs if result == "SUCCESS" else [],
        }
        return {"recipient": recipient, "amount": amount}

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - a router
        self.requests.append(request)
        path = request.url.path
        if path == "/wallet/getnowblock":
            return httpx.Response(200, json=_tron_block(self.latest_ts))
        if path == "/walletsolidity/getnowblock":
            return httpx.Response(200, json=_tron_block(self.solid_ts))
        if path == "/walletsolidity/gettransactioninfobyid":
            return httpx.Response(200, json=self.infos.get(json.loads(request.content)["value"], {}))
        if path == "/wallet/getaccount":
            body = json.loads(request.content)
            return httpx.Response(200, json={"balance": self.trx.get(body["address"], 0)})
        if path == "/wallet/broadcasthex":
            return httpx.Response(200, json=self._broadcast(json.loads(request.content)["transaction"]))
        if path == "/wallet/triggerconstantcontract":
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"constant_result": [f"{self.balances.get(body['owner_address'], 0):064x}"]}
            )
        if path.startswith("/v1/accounts/") and path.endswith("/transactions/trc20"):
            return httpx.Response(200, json=self._history(path.split("/")[3], request.url.params))
        return httpx.Response(404, json={"error": "not found"})

    def _history(self, address: str, params: Any) -> dict[str, Any]:
        lo, hi = int(params["min_timestamp"]), int(params["max_timestamp"])
        confirmed = params.get("only_confirmed") == "true"
        items = [
            t
            for t in self.transfers
            if t["to"] == address
            and t["token_info"]["address"] == params["contract_address"]
            and lo <= t["block_timestamp"] <= hi
            and (not confirmed or t["block_timestamp"] <= self.solid_ts)
        ]
        start = int(params.get("fingerprint") or 0)
        page = items[start : start + self.page_size]
        meta: dict[str, Any] = {"page_size": len(page)}
        if start + self.page_size < len(items):
            meta["fingerprint"] = str(start + self.page_size)
        return {"data": page, "success": True, "meta": meta}

    def transfer(
        self, *, contract: str, to: str, amount: int, ts: int, tx: str, sender: str = "TSender"
    ) -> None:
        from app.chains.adapters.tron import TRANSFER_TOPIC, tron_hex  # noqa: PLC0415

        self.transfers.append(
            {
                "transaction_id": tx,
                "token_info": {"symbol": "USDT", "address": contract, "decimals": 6, "name": "Tether USD"},
                "block_timestamp": ts,
                "from": sender,
                "to": to,
                "type": "Transfer",
                "value": str(amount),
            }
        )
        self.infos[tx] = {
            "id": tx,
            "blockNumber": 60_000_000,
            "blockTimeStamp": ts,
            "receipt": {"result": "SUCCESS"},
            "log": [
                {
                    "address": tron_hex(contract),
                    "topics": [TRANSFER_TOPIC, "0" * 64, "0" * 24 + tron_hex(to)],
                    "data": f"{amount:064x}",
                }
            ],
        }


def _tron_block(ts: int) -> dict[str, Any]:
    number = ts // 3000
    block_id = number.to_bytes(8, "big").hex() + "ab" * 24
    return {"blockID": block_id, "block_header": {"raw_data": {"number": number, "timestamp": ts}}}


def _proto(data: bytes) -> dict[int, list[Any]]:
    """Minimal protobuf reader for the fake node: varint and length-delimited fields."""
    out: dict[int, list[Any]] = defaultdict(list)
    i = 0

    def varint() -> int:
        nonlocal i
        shift = value = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    while i < len(data):
        key = varint()
        field_no, wire = key >> 3, key & 7
        if wire == 0:
            out[field_no].append(varint())
        elif wire == 2:
            length = varint()
            out[field_no].append(data[i : i + length])
            i += length
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


@dataclass
class FakeSolanaNode:
    """Solana JSON-RPC: slots, token accounts, signatures per account, jsonParsed transactions."""

    confirmed: int = 300_000_000
    finalized: int = 299_999_960
    token_accounts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # owner → accounts
    signatures: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # account → newest first
    transactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    balances: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, list[Any]]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append((method, params))
        result = getattr(self, "_" + method)(params)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def _commit_slot(self, options: dict[str, Any]) -> int:
        return self.finalized if options.get("commitment") == "finalized" else self.confirmed

    def _getSlot(self, p: list[Any]) -> int:
        return self._commit_slot(p[0])

    def _getTokenAccountsByOwner(self, p: list[Any]) -> dict[str, Any]:
        owner, mint = p[0], p[1]["mint"]
        return {
            "value": [
                a
                for a in self.token_accounts.get(owner, [])
                if a["account"]["data"]["parsed"]["info"]["mint"] == mint
            ]
        }

    def _getSignaturesForAddress(self, p: list[Any]) -> list[dict[str, Any]]:
        account, options = p[0], p[1]
        limit_slot = self._commit_slot(options)
        items = [s for s in self.signatures.get(account, []) if s["slot"] <= limit_slot]
        if options.get("before"):
            index = next(i for i, s in enumerate(items) if s["signature"] == options["before"])
            items = items[index + 1 :]
        return items[: options["limit"]]

    def _getTransaction(self, p: list[Any]) -> dict[str, Any] | None:
        tx = self.transactions.get(p[0])
        if tx is None or tx["slot"] > self._commit_slot(p[1]):
            return None
        return tx

    def _getTokenAccountBalance(self, p: list[Any]) -> dict[str, Any]:
        return {"value": {"amount": str(self.balances.get(p[0], 0)), "decimals": 6}}

    # ── test controls ───────────────────────────────────────────────────
    def open_account(self, owner: str, account: str, mint: str) -> None:
        self.token_accounts.setdefault(owner, []).append(
            {
                "pubkey": account,
                "account": {
                    "data": {"parsed": {"info": {"mint": mint, "owner": owner}}},
                    "owner": "Tokenkeg",
                },
            }
        )

    def transfer(
        self,
        *,
        account: str,
        mint: str,
        amount: int,
        slot: int,
        signature: str,
        before: int = 0,
        refs: tuple[str, ...] = (),
        memo: str | None = None,
        err: object = None,
    ) -> None:
        keys = ["Payer111", "SourceAta111", account, *refs]
        instructions: list[dict[str, Any]] = [{"program": "spl-token", "parsed": {"type": "transferChecked"}}]
        if memo is not None:
            instructions.append({"program": "spl-memo", "parsed": memo})
        self.transactions[signature] = {
            "slot": slot,
            "meta": {
                "err": err,
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "uiTokenAmount": {"amount": str(10**9)}},
                    {"accountIndex": 2, "mint": mint, "uiTokenAmount": {"amount": str(before)}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "uiTokenAmount": {"amount": str(10**9 - amount)}},
                    {"accountIndex": 2, "mint": mint, "uiTokenAmount": {"amount": str(before + amount)}},
                ],
            },
            "transaction": {
                "message": {"accountKeys": [{"pubkey": k} for k in keys], "instructions": instructions}
            },
        }
        self.signatures.setdefault(account, []).insert(0, {"signature": signature, "slot": slot, "err": err})


@dataclass
class EvmMempool:
    """Transactions sent to fake EVM nodes; the test mines them into blocks on every node."""

    nodes: list[FakeEvmNode]
    contract: str
    pending: dict[str, bytes] = field(default_factory=dict)
    revert: set[str] = field(default_factory=set)  # recipients the token contract blacklists

    def attach(self) -> None:
        for node in self.nodes:
            node.mempool = self  # type: ignore[attr-defined]

    def mine(self, block: int) -> list[str]:
        import rlp  # noqa: PLC0415
        from eth_utils import keccak  # noqa: PLC0415

        mined = []
        for tx_hash, raw in list(self.pending.items()):
            if raw[0] == 2:
                fields = rlp.decode(raw[1:])
                to, data, price = fields[5], fields[7], int.from_bytes(fields[3], "big")
            else:
                fields = rlp.decode(raw)
                to, data, price = fields[3], fields[5], int.from_bytes(fields[1], "big")
            assert "0x" + keccak(raw).hex() == tx_hash
            recipient = "0x" + data[16:36].hex()
            amount = int.from_bytes(data[36:68], "big")
            status = "0x0" if recipient.lower() in self.revert else "0x1"
            logs = []
            if status == "0x1":
                logs = [
                    {
                        "address": "0x" + to.hex(),
                        "topics": [TRANSFER_TOPIC, pad_address("0x" + "11" * 20), pad_address(recipient)],
                        "data": hex(amount),
                        "logIndex": "0x0",
                        "blockNumber": hex(block),
                    }
                ]
            receipt = {
                "status": status,
                "blockNumber": hex(block),
                "blockHash": f"0xblock{block}",
                "gasUsed": hex(52_000),
                "effectiveGasPrice": hex(price),
                "logs": logs,
            }
            for node in self.nodes:
                node.receipts[tx_hash] = receipt
                node.sent_nonces += 1  # type: ignore[attr-defined]
            mined.append(tx_hash)
            del self.pending[tx_hash]
        return mined
