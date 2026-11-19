"""Address format validation per network (offline, no RPC).

Returns a normalized address. A wrong address means irreversible loss, so validation is strict:
checksums are verified wherever the format has one.
"""

from __future__ import annotations

import base64
import binascii
import re

import base58
from eth_utils.address import is_checksum_address, to_checksum_address

from app.domain.enums import Network
from app.domain.errors import ValidationFailed

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TON_RAW_RE = re.compile(r"^(-1|0):[0-9a-fA-F]{64}$")
_EVM_ZERO = "0x" + "0" * 40


class InvalidAddress(ValidationFailed):
    code = "invalid_address"


def validate_address(network: Network, address: str, *, allow_testnet: bool = False) -> str:
    address = address.strip()
    if not address:
        raise InvalidAddress("empty address", network=network)
    if network in (Network.BSC, Network.ETH):
        return _evm(address)
    if network is Network.TRON:
        return _tron(address)
    if network is Network.TON:
        return _ton(address, allow_testnet=allow_testnet)
    if network is Network.SOL:
        return _solana(address)
    raise InvalidAddress("this network pays out to a Telegram account, not an address", network=network)


def _evm(address: str) -> str:
    if not _EVM_RE.fullmatch(address):
        raise InvalidAddress("not an EVM address", address=address)
    body = address[2:]
    mixed_case = body != body.lower() and body != body.upper()
    if mixed_case and not is_checksum_address(address):
        # A mixed-case address carries an EIP-55 checksum: a mismatch means a typo.
        raise InvalidAddress("EIP-55 checksum mismatch", address=address)
    if address.lower() == _EVM_ZERO:
        raise InvalidAddress("zero address", address=address)
    return str(to_checksum_address(address))


def _tron(address: str) -> str:
    try:
        payload = base58.b58decode_check(address)
    except ValueError as exc:
        raise InvalidAddress("bad TRON base58check", address=address) from exc
    if len(payload) != 21 or payload[0] != 0x41:
        raise InvalidAddress("not a TRON mainnet address", address=address)
    return address


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _ton(address: str, *, allow_testnet: bool) -> str:
    if _TON_RAW_RE.fullmatch(address):
        return address.lower()
    if len(address) != 48:
        raise InvalidAddress("not a TON address", address=address)
    try:
        raw = base64.urlsafe_b64decode(address.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError) as exc:
        raise InvalidAddress("bad TON base64", address=address) from exc
    if len(raw) != 36:
        raise InvalidAddress("bad TON address length", address=address)
    if _crc16_xmodem(raw[:34]) != int.from_bytes(raw[34:], "big"):
        raise InvalidAddress("TON address checksum mismatch", address=address)
    flags, workchain = raw[0], raw[1]
    if flags & 0x80 and not allow_testnet:
        raise InvalidAddress("testnet-only TON address", address=address)
    if flags & 0x7F not in (0x11, 0x51):
        raise InvalidAddress("unknown TON address flags", address=address)
    if workchain not in (0x00, 0xFF):
        raise InvalidAddress("unsupported TON workchain", address=address)
    return address


def _solana(address: str) -> str:
    try:
        raw = base58.b58decode(address)
    except ValueError as exc:
        raise InvalidAddress("bad Solana base58", address=address) from exc
    if len(raw) != 32:
        raise InvalidAddress("not a Solana public key", address=address)
    return address


def same_address(network: Network, a: str, b: str) -> bool:
    """EVM addresses are case-insensitive; everything else compares exactly."""
    if network in (Network.BSC, Network.ETH):
        return a.lower() == b.lower()
    return a == b
