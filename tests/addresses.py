"""Deterministic valid test addresses for every network."""

from __future__ import annotations

import base64
import hashlib

import base58
from eth_utils.address import to_checksum_address

from app.chains.addresses import _crc16_xmodem


def _seed_bytes(seed: int, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return out[:length]


def evm(seed: int) -> str:
    return str(to_checksum_address("0x" + _seed_bytes(seed, 20).hex()))


def tron(seed: int) -> str:
    return base58.b58encode_check(b"\x41" + _seed_bytes(seed, 20)).decode()


def ton(seed: int, *, testnet: bool = False, bounceable: bool = True) -> str:
    flags = (0x11 if bounceable else 0x51) | (0x80 if testnet else 0)
    body = bytes([flags, 0x00]) + _seed_bytes(seed, 32)
    return base64.urlsafe_b64encode(body + _crc16_xmodem(body).to_bytes(2, "big")).decode()


def sol(seed: int) -> str:
    return base58.b58encode(_seed_bytes(seed, 32)).decode()
