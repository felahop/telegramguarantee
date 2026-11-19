"""Application configuration.

* ``Settings`` — process settings from environment / ``.env`` (secrets are ``SecretStr``).
* ``ChainsConfig`` — per-network settings from ``chains.yaml``: token whitelist, decimals,
  finality, deposit mode, fixed payout network fee, RPC endpoints.

Monetary settings are expressed in USDT (human units, ``Decimal``) and converted to minor units
per network with ``AssetSpec`` — USDT has 18 decimals on BSC and 6 elsewhere.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.enums import Asset, Network, NetworkFeePayer
from app.domain.money import to_minor

MAX_TAG_USDT = Decimal("0.0999")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True)

    environment: Literal["dev", "test", "prod"] = "dev"
    # Accept testnet-only address formats (TON testnet flag) — never enable in production.
    testnet_mode: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    # Telegram
    bot_token: SecretStr = SecretStr("")
    bot_username: str = "escrow_guarantee_bot"
    webhook_base_url: str = ""
    telegram_webhook_secret: SecretStr = SecretStr("")
    # Bootstrap admins (also granted the admin role in the DB on startup). "1,2,3" or JSON list.
    admin_ids: Annotated[tuple[int, ...], NoDecode] = ()

    # Infrastructure
    database_url: str = "postgresql+asyncpg://escrow:escrow@localhost:5432/escrow"
    redis_url: str = "redis://localhost:6379/0"
    chains_config_path: Path = Path("chains.yaml")

    # Crypto Pay (CryptoBot)
    cryptobot_api_token: SecretStr = SecretStr("")
    cryptobot_testnet: bool = True
    cryptobot_webhook_secret_path: SecretStr = SecretStr("")

    # Fees and limits (USDT)
    fee_rate_bps: int = Field(200, ge=0, le=5000)
    fee_min_usdt: Decimal = Decimal("1.00")
    deal_min_usdt: Decimal = Decimal("5")
    deal_max_usdt: Decimal = Decimal("10000")
    new_user_deal_max_usdt: Decimal = Decimal("300")
    new_user_completed_deals: int = Field(3, ge=0)
    network_fee_payer: NetworkFeePayer = NetworkFeePayer.SELLER
    tag_mode: Literal["discount", "surcharge"] = "discount"
    overpay_dust_usdt: Decimal = Decimal("1.00")
    refund_charges_service_fee: bool = False
    dispute_keeps_fee_default: bool = True
    terms_max_length: int = 2000

    # Timeouts
    offer_ttl: timedelta = timedelta(hours=24)
    pay_start_ttl: timedelta = timedelta(hours=24)
    payment_ttl: timedelta = timedelta(minutes=30)
    payment_extension: timedelta = timedelta(minutes=30)
    buyer_reminder_interval: timedelta = timedelta(hours=24)
    buyer_silence_dispute_after: timedelta = timedelta(hours=72)
    refund_objection_window: timedelta = timedelta(hours=12)
    tag_cooldown: timedelta = timedelta(hours=72)
    dispute_sla: timedelta = timedelta(hours=24)
    execution_hours_max: int = Field(24 * 30, ge=1)

    # Payout controls
    manual_approval_threshold_usdt: Decimal = Decimal("2000")
    # Product decision: one admin confirmation in every case — a large payout needs one approval,
    # and a dispute decision by one admin authorizes its payouts by itself.
    large_payout_approvals: int = Field(1, ge=1)
    dispute_payout_requires_approval: bool = False
    hot_wallet_limit_usdt: Decimal = Decimal("10000")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ()
            if value.startswith("["):
                return tuple(yaml.safe_load(value))
            return tuple(int(part) for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.tag_mode == "discount" and self.fee_min_usdt <= MAX_TAG_USDT:
            # The tag discount is paid out of the service fee; it must never exceed it.
            raise ValueError("fee_min_usdt must exceed the maximum payment tag (0.0999) in discount mode")
        if not (Decimal(0) < self.deal_min_usdt <= self.new_user_deal_max_usdt <= self.deal_max_usdt):
            raise ValueError("expected 0 < deal_min <= new_user_deal_max <= deal_max")
        if self.testnet_mode and self.environment == "prod":
            raise ValueError("testnet_mode must not be enabled in production")
        if self.buyer_reminder_interval >= self.buyer_silence_dispute_after:
            raise ValueError("buyer reminders must come before the automatic dispute")
        return self


class FinalityConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # evm: "finalized" block tag (plus min_confirmations); tron: solidified; ton: masterchain; sol: finalized
    kind: Literal["finalized_tag", "confirmations", "solidified", "masterchain", "commitment_finalized"]
    min_confirmations: int = Field(0, ge=0)


class RpcEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    name: str
    weight: int = Field(1, ge=1)
    # Methods the endpoint is known to support (many public RPCs disable or cap eth_getLogs).
    methods: tuple[str, ...] = ()
    max_block_range: int | None = None
    api_key_env: str | None = None  # name of the env var with the key, never the key itself


class NetworkConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    title: str
    asset: Asset = Asset.USDT
    decimals: int = Field(ge=2, le=36)
    contract: str = ""  # token contract / jetton master / SPL mint; empty for CryptoBot
    deposit_mode: Literal["shared", "per_deal", "invoice"] = "shared"
    deposit_address: str = ""
    finality: FinalityConfig | None = None
    poll_interval_seconds: float = Field(5, gt=0)
    network_fee_usdt: Decimal = Decimal("0")
    min_payout_usdt: Decimal = Decimal("1")
    rpc: tuple[RpcEndpoint, ...] = ()
    verify_rpc: tuple[RpcEndpoint, ...] = ()  # independent second source for crediting


class AssetSpec(BaseModel):
    """Everything the money logic needs to know about USDT on one network, in minor units."""

    model_config = ConfigDict(frozen=True)

    network: Network
    asset: Asset
    decimals: int
    title: str
    contract: str
    network_fee: int
    min_payout: int

    def minor(self, usdt: Decimal | int | str) -> int:
        return to_minor(usdt, self.decimals)


class ChainsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    networks: dict[Network, NetworkConfig]

    @model_validator(mode="after")
    def _check(self) -> Self:
        for network, cfg in self.networks.items():
            if network is not Network.CRYPTOBOT and cfg.enabled:
                if not cfg.contract:
                    raise ValueError(f"{network}: token contract is required (whitelist)")
                if cfg.finality is None:
                    raise ValueError(f"{network}: finality rule is required")
        return self

    def enabled_networks(self) -> list[Network]:
        return [n for n, cfg in self.networks.items() if cfg.enabled]

    def spec(self, network: Network) -> AssetSpec:
        cfg = self.networks.get(network)
        if cfg is None or not cfg.enabled:
            raise KeyError(f"network {network} is not enabled")
        return AssetSpec(
            network=network,
            asset=cfg.asset,
            decimals=cfg.decimals,
            title=cfg.title,
            contract=cfg.contract,
            network_fee=to_minor(cfg.network_fee_usdt, cfg.decimals),
            min_payout=to_minor(cfg.min_payout_usdt, cfg.decimals),
        )


def load_chains(path: Path) -> ChainsConfig:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return ChainsConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_chains() -> ChainsConfig:
    return load_chains(get_settings().chains_config_path)
