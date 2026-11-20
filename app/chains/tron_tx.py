"""Building TRON transactions locally (no node-built transactions to trust).

A node that builds the transaction for us (``triggersmartcontract``) could put any recipient into
the bytes we sign. Instead the signer encodes ``TriggerSmartContract`` itself: the few protobuf
fields of ``protocol.Transaction.raw`` are written by hand below (field numbers from java-tron's
``Tron.proto`` / ``smart_contract.proto``). The transaction id is sha256 of the raw bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

TRIGGER_SMART_CONTRACT = 31
TYPE_URL = "type.googleapis.com/protocol.TriggerSmartContract"


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative varint")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _int(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value) if value else b""


def _bytes(field: int, value: bytes) -> bytes:
    return _key(field, 2) + _varint(len(value)) + value if value else b""


@dataclass(frozen=True)
class RefBlock:
    number: int
    block_id: str  # hex, 32 bytes: the first 8 bytes carry the number, bytes 8..16 the hash part


def trc20_transfer_raw(
    *,
    owner: bytes,  # 21 bytes, 0x41-prefixed
    contract: bytes,
    recipient20: bytes,  # 20-byte address body
    amount: int,
    ref: RefBlock,
    timestamp_ms: int,
    expiration_ms: int,
    fee_limit_sun: int,
) -> bytes:
    if len(owner) != 21 or len(contract) != 21 or len(recipient20) != 20:
        raise ValueError("bad TRON address length")
    data = bytes.fromhex("a9059cbb") + recipient20.rjust(32, b"\0") + amount.to_bytes(32, "big")
    trigger = _bytes(1, owner) + _bytes(2, contract) + _bytes(4, data)
    any_param = _bytes(1, TYPE_URL.encode()) + _bytes(2, trigger)
    contract_msg = _int(1, TRIGGER_SMART_CONTRACT) + _bytes(2, any_param)
    block_id = bytes.fromhex(ref.block_id)
    return (
        _bytes(1, ref.number.to_bytes(8, "big")[6:8])  # ref_block_bytes
        + _bytes(4, block_id[8:16])  # ref_block_hash
        + _int(8, expiration_ms)
        + _bytes(11, contract_msg)
        + _int(14, timestamp_ms)
        + _int(18, fee_limit_sun)
    )


def tx_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def signed_transaction(raw: bytes, signature: bytes) -> bytes:
    """``protocol.Transaction`` {raw_data = 1, signature = 2} for /wallet/broadcasthex."""
    return _bytes(1, raw) + _bytes(2, signature)
