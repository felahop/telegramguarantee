"""SPL USDT adapter against fake Solana RPC nodes; end to end through the watcher."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.chains.adapters.solana import SolanaAdapter
from app.chains.base import ChainUnavailable
from app.chains.pool import EndpointPool
from app.config import ChainsConfig, RpcEndpoint
from app.domain.enums import DealStatus, MatchMethod, Network
from tests import addresses
from tests.chains_fake import FakeSolanaNode, routing_transport
from tests.integration.chain_world import OC

SOL = Network.SOL
U = 10**6
ATA = addresses.sol(501)


@dataclass
class SolWorld:
    adapter: SolanaAdapter
    rpc: FakeSolanaNode
    verifier: FakeSolanaNode
    mint: str
    owner: str

    def pay(self, amount: int, *, slot: int, sig: str, **kw: object) -> None:
        for node in (self.rpc, self.verifier):
            node.transfer(account=ATA, mint=self.mint, amount=amount, slot=slot, signature=sig, **kw)  # type: ignore[arg-type]


def make_world(chains: ChainsConfig) -> SolWorld:
    cfg = chains.networks[SOL].model_copy(
        update={
            "rpc": (RpcEndpoint(name="rpc", url="https://sol-rpc.test"),),
            "verify_rpc": (RpcEndpoint(name="verifier", url="https://sol-verify.test"),),
        }
    )
    nodes = {"sol-rpc.test": FakeSolanaNode(), "sol-verify.test": FakeSolanaNode()}
    for node in nodes.values():
        node.open_account(cfg.deposit_address, ATA, cfg.contract)
    transport = routing_transport(nodes)
    adapter = SolanaAdapter(
        cfg,
        addresses=(cfg.deposit_address,),
        primary=EndpointPool(cfg.rpc, transport=transport),
        verifier=EndpointPool(cfg.verify_rpc, transport=transport),
    )
    return SolWorld(
        adapter, nodes["sol-rpc.test"], nodes["sol-verify.test"], cfg.contract, cfg.deposit_address
    )


@pytest.fixture
def sol(chains: ChainsConfig) -> SolWorld:
    return make_world(chains)


async def test_head_uses_commitments(sol: SolWorld) -> None:
    head = await sol.adapter.head()
    assert (head.latest, head.final) == (sol.rpc.confirmed, sol.rpc.finalized)


async def test_scan_reads_increases_of_our_usdt_accounts(sol: SolWorld) -> None:
    sol.pay(40 * U, slot=299_999_900, sig="sig-a", memo="D7KQ2MXA", refs=("Ref111",))
    sol.pay(5 * U, slot=299_999_901, sig="sig-failed", err={"InstructionError": [0, "x"]})
    other_mint = addresses.sol(77)
    sol.rpc.open_account(sol.owner, "FakeAta", other_mint)  # a fake token: never even asked about
    sol.rpc.transfer(
        account="FakeAta", mint=other_mint, amount=10**12, slot=299_999_902, signature="sig-fake"
    )
    [t] = await sol.adapter.scan(299_999_000, sol.rpc.finalized, final=True)
    assert (t.tx_hash, t.amount, t.position, t.memo) == ("sig-a", 40 * U, 299_999_900, "D7KQ2MXA")
    assert "Ref111" in t.references
    assert t.to_address == sol.owner


async def test_final_scan_ignores_unfinalized_slots(sol: SolWorld) -> None:
    sol.pay(10 * U, slot=sol.rpc.finalized + 10, sig="sig-new")
    assert await sol.adapter.scan(299_999_000, sol.rpc.finalized, final=True) == []
    [seen] = await sol.adapter.scan(sol.rpc.finalized, sol.rpc.confirmed, final=False)
    assert seen.tx_hash == "sig-new"


async def test_signatures_are_paged(sol: SolWorld) -> None:
    from app.chains.adapters import solana  # noqa: PLC0415

    solana.SIGNATURES_LIMIT, old = 2, solana.SIGNATURES_LIMIT
    try:
        for i in range(5):
            sol.pay((i + 1) * U, slot=299_999_800 + i, sig=f"sig{i}")
        found = await sol.adapter.scan(299_999_000, sol.rpc.finalized, final=True)
    finally:
        solana.SIGNATURES_LIMIT = old
    assert [t.tx_hash for t in found] == [f"sig{i}" for i in range(5)]


async def test_verification(sol: SolWorld) -> None:
    sol.pay(40 * U, slot=299_999_900, sig="sig-a")
    [t] = await sol.adapter.scan(299_999_000, sol.rpc.finalized, final=True)
    assert await sol.adapter.verify(t)
    sol.verifier.transactions["sig-a"]["meta"]["postTokenBalances"][1]["uiTokenAmount"]["amount"] = str(U)
    assert not await sol.adapter.verify(t)
    del sol.verifier.transactions["sig-a"]
    with pytest.raises(ChainUnavailable):
        await sol.adapter.verify(t)


async def test_token_balance_sums_all_our_accounts(sol: SolWorld) -> None:
    sol.rpc.open_account(sol.owner, "SecondAta", sol.mint)
    sol.rpc.balances.update({ATA: 7 * U, "SecondAta": 3 * U})
    assert await sol.adapter.token_balance(sol.owner) == 10 * U


async def test_solana_pay_reference_end_to_end(oc: OC, chains: ChainsConfig) -> None:
    world = make_world(chains)
    oc.adapters[SOL] = world.adapter  # type: ignore[assignment]
    await oc.tick(SOL)
    deal_id, request = await oc.awaiting(SOL)
    world.pay(
        request.expected_amount - U,
        slot=world.rpc.confirmed + 5,
        sig="sig-pay",
        refs=(request.sol_reference or "",),
    )
    for node in (world.rpc, world.verifier):
        node.confirmed += 10
        node.finalized = node.confirmed
    await oc.tick(SOL)  # 1 USDT short, but the reference ties it to the deal
    [t] = await oc.transfers()
    assert t.match_method is MatchMethod.REFERENCE
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT
    world.pay(U, slot=world.rpc.confirmed + 5, sig="sig-topup", refs=(request.sol_reference or "",))
    for node in (world.rpc, world.verifier):
        node.confirmed += 10
        node.finalized = node.confirmed
    await oc.tick(SOL)
    assert (await oc.deal(deal_id)).status is DealStatus.FUNDED


async def test_lookup_tx_for_claims(sol: SolWorld) -> None:
    sol.pay(40 * U, slot=299_999_900, sig="sig-a")
    [t] = await sol.adapter.lookup_tx("sig-a")
    assert (t.tx_hash, t.amount) == ("sig-a", 40 * U)
    assert await sol.adapter.lookup_tx("sig-missing") == []
