from __future__ import annotations

import json
from decimal import Decimal

import pytest
import structlog
from pydantic import SecretStr, ValidationError

from app.config import ChainsConfig, NetworkConfig, Settings
from app.domain.enums import Network
from app.logging import REDACTED, configure_logging, get_logger, redact_secrets
from tests.conftest import ROOT


def make_settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg, arg-type]


class TestSettings:
    def test_admin_ids_comma_separated(self) -> None:
        assert make_settings(admin_ids="1, 2,3").admin_ids == (1, 2, 3)

    def test_admin_ids_json(self) -> None:
        assert make_settings(admin_ids="[10, 20]").admin_ids == (10, 20)

    def test_admin_ids_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADMIN_IDS", "7,8")
        assert make_settings().admin_ids == (7, 8)

    def test_min_fee_must_cover_tag_discount(self) -> None:
        with pytest.raises(ValidationError, match="maximum payment tag"):
            make_settings(fee_min_usdt=Decimal("0.05"))

    def test_limits_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(new_user_deal_max_usdt=Decimal("20000"))

    def test_secrets_are_not_printable(self) -> None:
        s = make_settings(bot_token="123:SECRET", cryptobot_api_token="cb-secret")
        dumped = repr(s) + str(s.model_dump())
        assert "SECRET" not in dumped
        assert "cb-secret" not in dumped


class TestChainsConfig:
    def test_enabled_network_requires_contract_and_finality(self) -> None:
        with pytest.raises(ValidationError, match="contract"):
            ChainsConfig(networks={Network.TRON: NetworkConfig(enabled=True, title="t", decimals=6)})

    def test_disabled_network_is_not_available(self) -> None:
        cfg = ChainsConfig(networks={Network.TRON: NetworkConfig(enabled=False, title="t", decimals=6)})
        with pytest.raises(KeyError):
            cfg.spec(Network.TRON)


class TestRedaction:
    def test_redacts_secret_keys_recursively(self) -> None:
        event = {
            "event": "payout",
            "api_key": "k",
            "nested": {"private_key": "pk", "amount": 5, "items": [{"token": "t"}]},
            "secret_value": SecretStr("s"),
            "tx_hash": "0x" + "ab" * 32,
        }
        out = redact_secrets(None, "info", dict(event))
        assert out["api_key"] == REDACTED
        assert out["nested"]["private_key"] == REDACTED
        assert out["nested"]["items"][0]["token"] == REDACTED
        assert out["nested"]["amount"] == 5
        assert out["secret_value"] == REDACTED
        # tx hashes look like private keys but are public and essential for incident analysis
        assert out["tx_hash"] == event["tx_hash"]

    def test_rendered_log_has_no_secrets(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", json=True)
        try:
            get_logger("t").info("signing", mnemonic="abandon abandon", authorization="Bearer x", deal_id=7)
            line = capsys.readouterr().out.strip().splitlines()[-1]
            record = json.loads(line)
            assert record["mnemonic"] == REDACTED
            assert record["authorization"] == REDACTED
            assert record["deal_id"] == 7
            assert "abandon" not in line
        finally:
            structlog.reset_defaults()


def test_env_example_is_valid() -> None:
    s = Settings(_env_file=ROOT / ".env.example")  # type: ignore[call-arg]
    assert s.payment_ttl.total_seconds() == 1800
    assert s.buyer_silence_dispute_after.total_seconds() == 72 * 3600
    assert s.dispute_payout_requires_approval is False
    assert s.large_payout_approvals == 1  # one admin confirmation in every case
    assert Settings(_env_file=None).large_payout_approvals == 1  # type: ignore[call-arg]
