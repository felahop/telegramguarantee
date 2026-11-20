"""EVM adapter (BEP-20 / ERC-20) against fake JSON-RPC nodes, and end to end through the watcher."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.chains.adapters.evm import EvmAdapter
from app.chains.base import ChainUnavailable
from app.chains.pool import EndpointPool
from app.config import ChainsConfig, FinalityConfig, RpcEndpoint
from app.domain.enums import DealStatus, Network, TransferStatus
from app.services.ledger import AccountRef
from tests import addresses
from tests.chains_fake import FakeEvmNode, routing_transport
from tests.integration.chain_world import OC, usdt

BSC = Network.BSC
E18 = 10**18


@dataclass
class EvmWorld:
    adapter: EvmAdapter
    primary: FakeEvmNode
    backup: FakeEvmNode
    verifier: FakeEvmNode
    contract: str
    address: str

    def pay(self, amount: int, *, block: int, tx: str, **kw: object) -> None:
        """A transfer every node knows about."""
        for node in (self.primary, self.backup, self.verifier):
            node.transfer(contract=self.contract, to=self.address, amount=amount, block=block, tx=tx, **kw)  # type: ignore[arg-type]


def make_world(chains: ChainsConfig, network: Network = BSC) -> EvmWorld:
    cfg = chains.networks[network].model_copy(
        update={
            "finality": FinalityConfig(kind="finalized_tag", min_confirmations=15),
            "rpc": (
                RpcEndpoint(name="primary", url="https://primary.test/rpc", max_block_range=500),
                RpcEndpoint(name="backup", url="https://backup.test"),
            ),
            "verify_rpc": (RpcEndpoint(name="verifier", url="https://verifier.test"),),
        }
    )
    nodes = {"primary.test": FakeEvmNode(), "backup.test": FakeEvmNode(), "verifier.test": FakeEvmNode()}
    for node in nodes.values():
        node.decimals = cfg.decimals
    transport = routing_transport(nodes)
    adapter = EvmAdapter(
        network,
        cfg,
        addresses=(cfg.deposit_address,),
        primary=EndpointPool(cfg.rpc, transport=transport),
        verifier=EndpointPool(cfg.verify_rpc, transport=transport),
    )
    return EvmWorld(
        adapter,
        nodes["primary.test"],
        nodes["backup.test"],
        nodes["verifier.test"],
        cfg.contract,
        cfg.deposit_address,
    )


@pytest.fixture
async def evm(chains: ChainsConfig) -> EvmWorld:
    return make_world(chains)


async def test_final_head_is_the_stricter_of_tag_and_confirmations(evm: EvmWorld) -> None:
    evm.primary.latest, evm.primary.finalized = 1000, 995
    assert (await evm.adapter.head()).final == 985  # 1000 − 15 confirmations
    evm.primary.finalized = 980
    head = await evm.adapter.head()
    assert (head.latest, head.final) == (1000, 980)


async def test_scan_reads_only_the_real_usdt_to_our_address(evm: EvmWorld) -> None:
    evm.pay(150 * E18, block=950, tx="0xaaa", index=3)
    fake_token = addresses.evm(66)
    evm.primary.transfer(contract=fake_token, to=evm.address, amount=10**30, block=951, tx="0xfake")
    evm.primary.logs.append(
        {**evm.primary.logs[0], "removed": True, "transactionHash": "0xremoved", "logIndex": "0x9"}
    )
    [t] = await evm.adapter.scan(900, 990, final=True)
    assert (t.tx_hash, t.event_index, t.position, t.amount) == (
        "0xaaa",
        3,
        950,
        150 * E18,
    )  # 18 decimals on BSC
    assert t.to_address == evm.address
    assert t.token_contract.lower() == evm.contract.lower()


async def test_getlogs_range_shrinks_when_refused(evm: EvmWorld) -> None:
    evm.primary.max_range = 100
    evm.backup.max_range = 100
    evm.pay(5 * E18, block=400, tx="0xbbb")
    [t] = await evm.adapter.scan(0, 990, final=True)
    assert t.tx_hash == "0xbbb"
    ranges = [
        int(p[0]["toBlock"], 16) - int(p[0]["fromBlock"], 16) + 1
        for m, p in evm.primary.calls
        if m == "eth_getLogs"
    ]
    assert max(r for r in ranges[1:]) <= 500  # configured cap, then halved down to what the node accepts


async def test_failover_to_the_next_endpoint(evm: EvmWorld) -> None:
    evm.primary.failures["eth_blockNumber"] += [503]
    await evm.adapter.head()
    assert evm.adapter.active_endpoint == "backup"
    evm.backup.failures["eth_blockNumber"] += [429]
    evm.primary.failures.clear()
    await evm.adapter.head()  # primary is in cooldown, but it is the only one left that answers
    for node in (evm.primary, evm.backup):
        node.failures["eth_blockNumber"] += [500, 500]
    with pytest.raises(ChainUnavailable):
        await evm.adapter.head()


async def test_verification_by_an_independent_node(evm: EvmWorld) -> None:
    evm.pay(150 * E18, block=950, tx="0xaaa")
    [t] = await evm.adapter.scan(900, 990, final=True)
    assert await evm.adapter.verify(t)

    evm.verifier.receipts["0xaaa"]["logs"][0]["data"] = hex(1 * E18)  # the second source sees another amount
    assert not await evm.adapter.verify(t)
    evm.verifier.receipts["0xaaa"]["logs"][0]["data"] = hex(150 * E18)
    evm.verifier.receipts["0xaaa"]["status"] = "0x0"  # reverted
    assert not await evm.adapter.verify(t)
    evm.verifier.receipts["0xaaa"]["status"] = "0x1"
    evm.verifier.receipts["0xaaa"]["blockHash"] = "0xother"  # another block (fork)
    assert not await evm.adapter.verify(t)

    del evm.verifier.receipts["0xaaa"]
    with pytest.raises(ChainUnavailable):  # not known there yet: retry, never credit
        await evm.adapter.verify(t)
    evm.verifier.finalized = 940
    with pytest.raises(ChainUnavailable):  # not final at the second source yet
        await evm.adapter.verify(t)


async def test_decimals_are_checked_at_start(evm: EvmWorld) -> None:
    await evm.adapter.check_contract()
    evm.primary.decimals = evm.backup.decimals = 6
    with pytest.raises(RuntimeError, match="decimals"):
        await evm.adapter.check_contract()


async def test_token_balance_at_the_final_block(evm: EvmWorld) -> None:
    evm.primary.balances[evm.address.lower()] = 42 * E18
    assert await evm.adapter.token_balance(evm.address) == 42 * E18
    [call] = [p for m, p in evm.primary.calls if m == "eth_call"]
    assert call[1] == hex(985)


async def test_bep20_payment_end_to_end(oc: OC, chains: ChainsConfig) -> None:
    world = make_world(chains)
    oc.adapters[BSC] = world.adapter  # type: ignore[assignment]
    world.primary.latest = world.verifier.latest = world.backup.latest = 1000
    world.primary.finalized = world.verifier.finalized = world.backup.finalized = 985
    await oc.tick(BSC)
    deal_id, request = await oc.awaiting(BSC)
    assert request.expected_amount % 10**14 == 0  # the tag sits on the 4th decimal even with 18 decimals
    world.pay(request.expected_amount, block=1001, tx="0xpay")
    for node in (world.primary, world.backup, world.verifier):
        node.latest = 1003
    await oc.tick(BSC)
    [t] = await oc.transfers()
    assert t.status is TransferStatus.SEEN
    for node in (world.primary, world.backup, world.verifier):
        node.latest, node.finalized = 1030, 1010
    await oc.tick(BSC)
    assert (await oc.deal(deal_id)).status is DealStatus.FUNDED
    assert await oc.balance(AccountRef.custody(usdt(BSC), world.address)) == request.expected_amount


def test_registry_builds_evm_adapters(chains: ChainsConfig, settings: object) -> None:
    from app.chains.registry import build_adapters  # noqa: PLC0415

    adapters = build_adapters(chains, settings)  # type: ignore[arg-type]
    assert isinstance(adapters[Network.BSC], EvmAdapter)
    assert isinstance(adapters[Network.ETH], EvmAdapter)
    assert type(adapters[Network.TRON]).__name__ == "TronAdapter"
    assert type(adapters[Network.SOL]).__name__ == "SolanaAdapter"
    assert Network.CRYPTOBOT not in adapters
    assert Network.TON not in adapters


def test_startup_refuses_a_network_without_a_watcher(chains: ChainsConfig) -> None:
    from app.chains.registry import check_supported  # noqa: PLC0415

    with pytest.raises(RuntimeError, match="ton"):
        check_supported(chains)  # the test config enables TON
    networks = {n: c.model_copy(update={"enabled": n is not Network.TON}) for n, c in chains.networks.items()}
    check_supported(ChainsConfig(networks=networks))


async def test_lookup_tx_for_claims(evm: EvmWorld) -> None:
    evm.pay(150 * E18, block=950, tx="0xaaa", index=2)
    [t] = await evm.adapter.lookup_tx("0xaaa")
    assert (t.tx_hash, t.event_index, t.amount, t.position) == ("0xaaa", 2, 150 * E18, 950)
    assert await evm.adapter.lookup_tx("0xunknown") == []
    evm.primary.receipts["0xaaa"]["status"] = "0x0"
    assert await evm.adapter.lookup_tx("0xaaa") == []
