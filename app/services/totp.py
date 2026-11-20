"""TOTP second factor for admin money actions.

A compromised Telegram account of an admin must not be enough to decide disputes or release
payouts. Secrets are encrypted at rest (Fernet); each code can be used once (replay protection by
time step in the KV store); ±1 step of clock drift is accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pyotp
from cryptography.fernet import Fernet, InvalidToken

from app.bot.storage import KeyValueStore
from app.config import Settings
from app.db.models import User
from app.domain.errors import DomainError

STEP_SECONDS = 30
ISSUER = "Escrow Guarantee"


class TotpError(DomainError):
    code = "totp_error"


class TotpService:
    def __init__(self, settings: Settings, kv: KeyValueStore, clock: Callable[[], datetime]) -> None:
        self._settings = settings
        self._kv = kv
        self._clock = clock

    @property
    def required(self) -> bool:
        return self._settings.admin_totp_required

    def _fernet(self) -> Fernet:
        key = self._settings.secrets_encryption_key.get_secret_value()
        if not key:
            raise TotpError("TOTP is not configured on the server", reason="not_configured")
        return Fernet(key.encode())

    @staticmethod
    def enrolled(user: User) -> bool:
        return user.totp_secret_enc is not None and user.totp_confirmed_at is not None

    def start_enrollment(self, user: User) -> tuple[str, str]:
        """New secret (stored encrypted, unconfirmed). Returns (secret, otpauth URI) to show once."""
        secret = pyotp.random_base32()
        user.totp_secret_enc = self._fernet().encrypt(secret.encode())
        user.totp_confirmed_at = None
        uri = pyotp.TOTP(secret).provisioning_uri(name=f"admin-{user.id}", issuer_name=ISSUER)
        return secret, uri

    async def confirm_enrollment(self, user: User, code: str) -> None:
        await self._check(user, code)
        user.totp_confirmed_at = self._clock()

    async def verify(self, user: User, code: str) -> None:
        if not self.enrolled(user):
            raise TotpError("TOTP is not set up for this admin", reason="not_enrolled")
        await self._check(user, code)

    async def _check(self, user: User, code: str) -> None:
        if user.totp_secret_enc is None:
            raise TotpError("TOTP is not set up for this admin", reason="not_enrolled")
        try:
            secret = self._fernet().decrypt(user.totp_secret_enc).decode()
        except InvalidToken as exc:
            raise TotpError("TOTP secret cannot be decrypted", reason="not_configured") from exc
        code = code.strip().replace(" ", "")
        if not (code.isdigit() and len(code) == 6):
            raise TotpError("bad code format", reason="invalid")
        totp = pyotp.TOTP(secret)
        now = int(self._clock().timestamp())
        for drift in (0, -1, 1):
            moment = now + drift * STEP_SECONDS
            if totp.at(moment) == code:
                step = moment // STEP_SECONDS
                if not await self._kv.set_nx(f"totp-used:{user.id}:{step}", "1", 3 * STEP_SECONDS):
                    raise TotpError("this code was already used", reason="replay")
                return
        raise TotpError("wrong code", reason="invalid")
