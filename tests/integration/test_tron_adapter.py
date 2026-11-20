"""TRC-20 adapter against a fake TronGrid and an independent fake node; end to end through the watcher."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.chains.adapters.tron import TronAdapter, tron_base58, tron_hex
from app.chains.base import ChainUnavailable
from app.chains.pool import EndpointPool
from app.config import ChainsConfig, RpcEndpoint
from app.domain.enums import DealStatus, Network, TransferStatus
from app.services.ledger import AccountRef
from tests import addresses
from tests.chains_fake import FakeTronNode, routing_transport
from tests.integration.chain_world import OC, usdt

TRON = Network.TRON
U = 10**6
T0 = 1_700_000_000_000


@dataclass
class TronWorld:
    adapter: TronAdapter
    grid: FakeTronNode
    node: FakeTronNode
    contract: str
    address: str

    def pay(self, amount: int, *, ts: int, tx: str, contract: str | None = None) -> None:
        for n in (self.grid, self.node):
            n.transfer(contract=contract or self.contract, to=self.address, amount=amount, ts=ts, tx=tx)


def make_world(chains: ChainsConfig, *, env: dict[str, str] | None = None) -> TronWorld:
    cfg = chains.networks[TRON].model_copy(
        update={
            "rpc": (RpcEndpoint(name="trongrid", url="https://grid.test", api_key_env="TRONGRID_API_KEY"),),
            "verify_rpc": (RpcEndpoint(name="node", url="https://node.test"),),
        }
    )
    nodes = {"grid.test": FakeTronNode(), "node.test": FakeTronNode()}
    transport = routing_transport(nodes)
    adapter = TronAdapter(
        cfg,
        addresses=(cfg.deposit_address,),
        primary=EndpointPool(cfg.rpc, key_header="TRON-PRO-API-KEY", transport=transport, env=env or {}),
        verifier=EndpointPool(cfg.verify_rpc, transport=transport, env={}),
    )
    return TronWorld(adapter, nodes["grid.test"], nodes["node.test"], cfg.contract, cfg.deposit_address)


@pytest.fixture
def tron(chains: ChainsConfig) -> TronWorld:
    return make_world(chains)


def test_address_conversion_round_trip() -> None:
    address = addresses.tron(5)
    assert tron_base58(tron_hex(address)) == address
    assert len(tron_hex("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")) == 40


async def test_head_is_latest_and_solidified(tron: TronWorld) -> None:
    head = await tron.adapter.head()
    assert (head.latest, head.final) == (tron.grid.latest_ts, tron.grid.solid_ts)


async def test_scan_filters_fake_tokens_and_pages(tron: TronWorld) -> None:
    tron.grid.page_size = 2
    for i in range(5):
        tron.pay((i + 1) * U, ts=T0 + i * 3000, tx=f"tx{i}")
    tron.grid.transfer(contract=addresses.tron(99), to=tron.address, amount=10**12, ts=T0 + 1, tx="fake")
    found = await tron.adapter.scan(T0 - 1, T0 + 60_000, final=True)
    assert [t.tx_hash for t in found] == [f"tx{i}" for i in range(5)]
    assert [t.amount for t in found] == [(i + 1) * U for i in range(5)]
    assert all(t.token_contract == tron.contract for t in found)


async def test_final_scan_sees_only_solidified(tron: TronWorld) -> None:
    tron.pay(5 * U, ts=tron.grid.solid_ts + 3000, tx="fresh")
    assert await tron.adapter.scan(T0, tron.grid.latest_ts, final=True) == []
    [seen] = await tron.adapter.scan(T0, tron.grid.latest_ts, final=False)
    assert seen.tx_hash == "fresh"


async def test_api_key_goes_in_the_header_only(chains: ChainsConfig) -> None:
    world = make_world(chains, env={"TRONGRID_API_KEY": "secret-key"})
    await world.adapter.head()
    request = world.grid.requests[0]
    assert request.headers["TRON-PRO-API-KEY"] == "secret-key"
    assert "secret-key" not in str(request.url)


async def test_verification(tron: TronWorld) -> None:
    tron.pay(25 * U, ts=T0, tx="tx1")
    [t] = await tron.adapter.scan(T0 - 1, T0 + 1, final=True)
    assert await tron.adapter.verify(t)
    tron.node.infos["tx1"]["receipt"]["result"] = "OUT_OF_ENERGY"  # failed on chain
    assert not await tron.adapter.verify(t)
    tron.node.infos["tx1"]["receipt"]["result"] = "SUCCESS"
    tron.node.infos["tx1"]["log"][0]["data"] = f"{1:064x}"  # another amount
    assert not await tron.adapter.verify(t)
    tron.node.infos["tx1"]["log"][0]["data"] = f"{25 * U:064x}"
    tron.node.infos["tx1"]["log"][0]["address"] = tron_hex(addresses.tron(99))  # another token
    assert not await tron.adapter.verify(t)
    del tron.node.infos["tx1"]
    with pytest.raises(ChainUnavailable):
        await tron.adapter.verify(t)


async def test_token_balance(tron: TronWorld) -> None:
    tron.grid.balances[tron.address] = 77 * U
    assert await tron.adapter.token_balance(tron.address) == 77 * U


async def test_trc20_payment_end_to_end(oc: OC, chains: ChainsConfig) -> None:
    world = make_world(chains)
    oc.adapters[TRON] = world.adapter  # type: ignore[assignment]
    await oc.tick()
    deal_id, request = await oc.awaiting()
    paid_at = world.grid.latest_ts + 3000  # a new block
    world.pay(request.expected_amount, ts=paid_at, tx="trx-pay")
    for n in (world.grid, world.node):
        n.latest_ts = paid_at + 3000
    await oc.tick()
    assert [t.status for t in await oc.transfers()] == [TransferStatus.SEEN]
    for n in (world.grid, world.node):
        n.solid_ts = n.latest_ts = paid_at + 60_000
    await oc.tick()
    assert (await oc.deal(deal_id)).status is DealStatus.FUNDED
    assert await oc.balance(AccountRef.custody(usdt(TRON), world.address)) == request.expected_amount


async def test_lookup_tx_for_claims(tron: TronWorld) -> None:
    tron.pay(25 * U, ts=T0, tx="tx1")
    [t] = await tron.adapter.lookup_tx("0xTX1".lower().replace("0x", ""))
    assert (t.tx_hash, t.amount, t.position, t.to_address) == ("tx1", 25 * U, T0, tron.address)
    assert await tron.adapter.lookup_tx("missing") == []
