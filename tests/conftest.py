from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import ChainsConfig, Settings, load_chains
from tests import addresses

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def chains() -> ChainsConfig:
    """chains.example.yaml with every network enabled (deposit addresses are test values)."""
    base = load_chains(ROOT / "chains.example.yaml")
    deposit = {
        "tron": addresses.tron(1000),
        "bsc": addresses.evm(1000),
        "eth": addresses.evm(1000),
        "ton": addresses.ton(1000),
        "sol": addresses.sol(1000),
    }
    networks = {
        network: cfg.model_copy(update={"enabled": True, "deposit_address": deposit.get(network.value, "")})
        for network, cfg in base.networks.items()
    }
    return ChainsConfig(networks=networks)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        fee_rate_bps=200,
        fee_min_usdt=Decimal("1.00"),
        deal_min_usdt=Decimal("5"),
        deal_max_usdt=Decimal("10000"),
        new_user_deal_max_usdt=Decimal("300"),
        manual_approval_threshold_usdt=Decimal("2000"),
        offer_ttl=timedelta(hours=24),
        payment_ttl=timedelta(minutes=30),
    )
