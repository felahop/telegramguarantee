from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from pydantic import SecretStr

from app.bot.storage import MemoryKV
from app.config import Settings
from app.db.models import User
from app.services.totp import TotpError, TotpService
from tests.integration.deal_world import Clock


def code_at(secret: str, when: datetime) -> str:
    return pyotp.TOTP(secret).at(int(when.timestamp()))


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 3, 1, 10, 0, 5, tzinfo=UTC))


@pytest.fixture
def totp(settings: Settings, clock: Clock) -> TotpService:
    return TotpService(settings, MemoryKV(), clock)


async def enrolled(totp: TotpService, clock: Clock) -> tuple[User, str]:
    user = User(id=1, first_name="admin")
    secret, uri = totp.start_enrollment(user)
    assert uri.startswith("otpauth://totp/")
    assert secret.encode() not in (user.totp_secret_enc or b"")  # encrypted at rest
    assert not totp.enrolled(user)
    await totp.confirm_enrollment(user, code_at(secret, clock.now))
    assert totp.enrolled(user)
    return user, secret


async def test_valid_code(totp: TotpService, clock: Clock) -> None:
    user, secret = await enrolled(totp, clock)
    clock.advance(timedelta(seconds=30))
    await totp.verify(user, code_at(secret, clock.now))


async def test_code_cannot_be_reused(totp: TotpService, clock: Clock) -> None:
    user, secret = await enrolled(totp, clock)
    clock.advance(timedelta(seconds=30))
    code = code_at(secret, clock.now)
    await totp.verify(user, code)
    with pytest.raises(TotpError) as err:
        await totp.verify(user, code)
    assert err.value.context["reason"] == "replay"


async def test_one_step_of_drift_is_accepted(totp: TotpService, clock: Clock) -> None:
    user, secret = await enrolled(totp, clock)
    clock.advance(timedelta(minutes=5))
    await totp.verify(user, code_at(secret, clock.now - timedelta(seconds=30)))
    with pytest.raises(TotpError):
        await totp.verify(user, code_at(secret, clock.now - timedelta(seconds=90)))


@pytest.mark.parametrize("bad", ["000000", "12345", "abcdef", ""])
async def test_wrong_codes(totp: TotpService, clock: Clock, bad: str) -> None:
    user, _ = await enrolled(totp, clock)
    with pytest.raises(TotpError) as err:
        await totp.verify(user, bad)
    assert err.value.context["reason"] == "invalid"


async def test_not_enrolled(totp: TotpService) -> None:
    with pytest.raises(TotpError) as err:
        await totp.verify(User(id=2, first_name="x"), "123456")
    assert err.value.context["reason"] == "not_enrolled"


async def test_server_without_key(settings: Settings, clock: Clock) -> None:
    service = TotpService(
        settings.model_copy(update={"secrets_encryption_key": SecretStr("")}), MemoryKV(), clock
    )
    with pytest.raises(TotpError) as err:
        service.start_enrollment(User(id=3, first_name="x"))
    assert err.value.context["reason"] == "not_configured"
