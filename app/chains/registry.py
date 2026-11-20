"""Build the chain adapters of the enabled on-chain networks from chains.yaml."""

from __future__ import annotations

import httpx

from app.chains.adapters.evm import EvmAdapter
from app.chains.adapters.tron import TronAdapter
from app.chains.addresses import validate_address
from app.chains.base import ChainAdapter
from app.chains.pool import EndpointPool
from app.config import ChainsConfig, Settings
from app.domain.enums import Network, Provider
from app.logging import get_logger

log = get_logger(__name__)


def build_adapters(
    chains: ChainsConfig, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[Network, ChainAdapter]:
    adapters: dict[Network, ChainAdapter] = {}
    for network in chains.enabled_networks():
        if network.provider is not Provider.ONCHAIN:
            continue
        cfg = chains.networks[network]
        address = validate_address(network, cfg.deposit_address, allow_testnet=settings.testnet_mode)
        if network in (Network.BSC, Network.ETH):
            adapters[network] = EvmAdapter(
                network,
                cfg,
                addresses=(address,),
                primary=EndpointPool(cfg.rpc, transport=transport),
                verifier=EndpointPool(cfg.verify_rpc, transport=transport),
            )
        elif network is Network.TRON:
            adapters[network] = TronAdapter(
                cfg,
                addresses=(address,),
                primary=EndpointPool(cfg.rpc, key_header="TRON-PRO-API-KEY", transport=transport),
                verifier=EndpointPool(cfg.verify_rpc, key_header="TRON-PRO-API-KEY", transport=transport),
            )
        else:
            log.warning("chain_adapter_missing", network=network.value)
    return adapters
