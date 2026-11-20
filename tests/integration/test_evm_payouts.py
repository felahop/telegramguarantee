"""EVM payouts end to end: a BEP-20 deal funded on chain, paid out by the signer through fake nodes."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from eth_account import Account
from pydantic import SecretStr

from app.chains.adapters.evm import EvmAdapter
from app.chains.pool import EndpointPool
from app.config import ChainsConfig, FinalityConfig, RpcEndpoint
from app.db.models import HotWallet, Payout, PayoutAttempt, PayoutPause
from app.domain.enums import DealStatus, Network, PayoutAttemptStatus, PayoutStatus
from app.providers.onchain.gateway import OnchainGateway
from app.services.gateways import RoutingGateway
from app.services.ledger import AccountRef
from app.signer.signer import EvmFees, Signer, SignerRefused
from app.worker.onchain_payouts import EvmPayouts
from tests.chains_fake import EvmMempool, FakeEvmNode, routing_transport
from tests.integration.chain_world import OC, usdt
from tests.integration.deal_world import ADMIN, BUYER, SELLER, SELLER_ADDRESSES

BSC = Network.BSC
E18 = 10**18
KEY = "0x" + "4c" * 32
HOT = Account.from_key(KEY).address


@dataclass
class Pay:
    oc: OC
    adapter: EvmAdapter
    nodes: list[FakeEvmNode]
    mempool: EvmMempool
    signer: Signer
    contract: str

    @property
    def executor(self) -> EvmPayouts:
        return EvmPayouts(self.oc.ctx, self.adapter, self.signer)

    def heads(self, latest: int, final: int) -> None:
        for node in self.nodes:
            node.latest, node.finalized = latest, final

    def fund_hot_wallet(self, usdt_amount: int, gas: int = E18) -> None:
        for node in self.nodes:
            node.balances[HOT.lower()] = usdt_amount
            node.native[HOT.lower()] = gas

    async def payouts(self) -> list[Payout]:
        async with self.oc.ctx.sessionmaker() as s:
            return list((await s.execute(sa.select(Payout).order_by(Payout.id))).scalars())

    async def attempts(self) -> list[PayoutAttempt]:
        async with self.oc.ctx.sessionmaker() as s:
            return list((await s.execute(sa.select(PayoutAttempt).order_by(PayoutAttempt.id))).scalars())

    async def confirmed_deal(self, amount: str = "100") -> int:
        """A BEP-20 deal paid on chain and confirmed by both parties → a queued seller payout."""
        deal_id, request = await self.oc.awaiting(BSC, amount)
        latest = self.nodes[0].latest
        for node in self.nodes:
            node.transfer(
                contract=self.contract,
                to=HOT,
                amount=request.expected_amount,
                block=latest + 1,
                tx=f"0xin{deal_id}",
            )
        self.heads(latest + 30, latest + 20)
        await self.oc.tick(BSC)
        assert (await self.oc.deal(deal_id)).status is DealStatus.FUNDED
        async with self.oc.ctx.uow() as uow:
            await uow.deals.seller_confirm(SELLER, deal_id)
            await uow.deals.buyer_confirm(BUYER, deal_id)
        return deal_id

    async def step_until_idle(self) -> None:
        while await self.executor.run_once():
            pass


@pytest.fixture
async def pay(oc: OC, chains: ChainsConfig) -> Pay:
    cfg = chains.networks[BSC].model_copy(
        update={
            "deposit_address": HOT,
            "chain_id": 56,
            "finality": FinalityConfig(kind="finalized_tag", min_confirmations=0),
            "rpc": (RpcEndpoint(name="primary", url="https://primary.test"),),
            "verify_rpc": (RpcEndpoint(name="verifier", url="https://verifier.test"),),
        }
    )
    networks = dict(chains.networks) | {BSC: cfg}
    new_chains = ChainsConfig(networks=networks)
    settings = oc.ctx.settings
    oc.ctx = dataclasses.replace(
        oc.ctx,
        chains=new_chains,
        gateway=RoutingGateway({BSC: OnchainGateway(settings, new_chains, clock=oc.clock)}),
    )
    hosts = {"primary.test": FakeEvmNode(), "verifier.test": FakeEvmNode()}
    nodes = list(hosts.values())
    mempool = EvmMempool(nodes, cfg.contract)
    mempool.attach()
    transport = routing_transport(hosts)
    adapter = EvmAdapter(
        BSC,
        cfg,
        addresses=(HOT,),
        primary=EndpointPool(cfg.rpc, transport=transport),
        verifier=EndpointPool(cfg.verify_rpc, transport=transport),
    )
    oc.adapters[BSC] = adapter  # type: ignore[assignment]
    signer = Signer({HOT: Account.from_key(KEY)}, settings, new_chains, clock=oc.clock)
    world = Pay(oc, adapter, nodes, mempool, signer, cfg.contract)
    world.heads(1000, 1000)
    await oc.tick(BSC)
    return world


async def test_seller_payout_end_to_end(pay: Pay) -> None:
    deal_id = await pay.confirmed_deal()
    [payout] = await pay.payouts()
    assert (payout.status, payout.recipient_address) == (PayoutStatus.QUEUED, SELLER_ADDRESSES[BSC])
    pay.fund_hot_wallet(10_000 * E18)

    assert await pay.executor.sign_next()
    [attempt] = await pay.attempts()
    assert attempt.raw_tx and attempt.tx_hash and attempt.nonce == 0
    assert pay.mempool.pending == {}  # stored before anything was sent
    assert (await pay.payouts())[0].status is PayoutStatus.SIGNING

    assert await pay.executor.broadcast_next()
    assert list(pay.mempool.pending) == [attempt.tx_hash]
    pay.mempool.mine(block=1100)
    pay.heads(1105, 1099)  # mined, not final yet
    pay.oc.clock.advance(timedelta(seconds=20))
    assert await pay.executor.track_next()
    assert (await pay.payouts())[0].status is PayoutStatus.BROADCAST

    pay.heads(1130, 1120)
    pay.oc.clock.advance(timedelta(seconds=20))
    assert await pay.executor.track_next()
    [payout] = await pay.payouts()
    assert (payout.status, payout.confirmed_tx_hash) == (PayoutStatus.CONFIRMED, attempt.tx_hash)
    assert payout.native_fee_spent == 52_000 * 3 * 10**9
    assert (await pay.oc.deal(deal_id)).status is DealStatus.COMPLETED
    assert await pay.oc.balance(AccountRef.payable(usdt(BSC), deal_id)) == 0
    assert await pay.oc.balance(AccountRef.network_fee_expense("BNB@bsc")) == 52_000 * 3 * 10**9


async def test_a_failed_broadcast_resends_the_same_signed_bytes(pay: Pay) -> None:
    await pay.confirmed_deal()
    pay.fund_hot_wallet(10_000 * E18)
    await pay.executor.sign_next()
    for node in pay.nodes:
        node.reject_broadcast = True
    assert await pay.executor.broadcast_next()
    [payout] = await pay.payouts()
    assert payout.status is PayoutStatus.SIGNING
    assert "broadcast" in (payout.last_error or "")
    for node in pay.nodes:
        node.reject_broadcast = False
    pay.oc.clock.advance(timedelta(seconds=31))
    assert await pay.executor.broadcast_next()
    [attempt] = await pay.attempts()  # still one attempt: never signed again
    assert list(pay.mempool.pending) == [attempt.tx_hash]


async def test_nonces_come_from_the_locked_hot_wallet_row(pay: Pay) -> None:
    for node in pay.nodes:
        node.sent_nonces = 7  # the wallet already sent 7 transactions
    await pay.confirmed_deal()
    await pay.confirmed_deal("50")
    pay.fund_hot_wallet(10_000 * E18)
    assert await pay.executor.sign_next()
    assert await pay.executor.sign_next()
    assert [a.nonce for a in await pay.attempts()] == [7, 8]
    async with pay.oc.ctx.sessionmaker() as s:
        [wallet] = list((await s.execute(sa.select(HotWallet))).scalars())
    assert (wallet.network, wallet.next_nonce) == (BSC, 9)


async def test_revert_is_final_and_pays_nothing(pay: Pay) -> None:
    deal_id = await pay.confirmed_deal()
    pay.fund_hot_wallet(10_000 * E18)
    pay.mempool.revert.add(SELLER_ADDRESSES[BSC].lower())  # e.g. the token blacklisted the address
    await pay.executor.sign_next()
    await pay.executor.broadcast_next()
    pay.mempool.mine(block=1100)
    pay.heads(1130, 1120)
    pay.oc.clock.advance(timedelta(seconds=20))
    await pay.executor.track_next()
    [payout] = await pay.payouts()
    assert payout.status is PayoutStatus.FAILED_FINAL
    assert "failed on chain" in (payout.last_error or "")
    [attempt] = await pay.attempts()
    assert attempt.status is PayoutAttemptStatus.REVERTED
    assert await pay.oc.balance(AccountRef.payable(usdt(BSC), deal_id)) == payout.amount  # still owed
    assert "admin_payout_failed" in await pay.oc.notices(ADMIN)


async def test_preflight_waits_for_usdt_and_gas(pay: Pay) -> None:
    await pay.confirmed_deal()
    pay.fund_hot_wallet(1 * E18)  # less than the payout
    assert await pay.executor.sign_next()
    [payout] = await pay.payouts()
    assert payout.status is PayoutStatus.FAILED_RETRYABLE
    assert (payout.last_error or "").startswith("insufficient_funds")
    assert await pay.attempts() == []
    pay.fund_hot_wallet(10_000 * E18, gas=0)
    pay.oc.clock.advance(timedelta(minutes=11))
    await pay.executor.sign_next()
    assert ((await pay.payouts())[0].last_error or "").startswith("no_gas")
    assert await pay.attempts() == []


async def test_paused_wallet_signs_nothing(pay: Pay) -> None:
    await pay.confirmed_deal()
    pay.fund_hot_wallet(10_000 * E18)
    async with pay.oc.ctx.sessionmaker() as s, s.begin():
        s.add(PayoutPause(wallet=HOT, reason="test"))
    assert not await pay.executor.sign_next()
    assert await pay.attempts() == []


async def test_second_source_must_agree_before_confirming(pay: Pay) -> None:
    await pay.confirmed_deal()
    pay.fund_hot_wallet(10_000 * E18)
    await pay.executor.sign_next()
    await pay.executor.broadcast_next()
    [tx] = pay.mempool.mine(block=1100)
    pay.nodes[1].receipts.pop(tx)  # the verifier has not seen it yet
    pay.heads(1130, 1120)
    pay.oc.clock.advance(timedelta(seconds=20))
    await pay.executor.track_next()
    assert (await pay.payouts())[0].status is PayoutStatus.BROADCAST


async def test_signer_checks_the_payout_itself(pay: Pay) -> None:
    await pay.confirmed_deal()
    [payout] = await pay.payouts()
    fees = EvmFees(gas=100_000, gas_price=10**9)
    strict = Signer(
        {HOT: Account.from_key(KEY)},
        pay.oc.ctx.settings.model_copy(update={"signer_max_payout_usdt": Decimal("50")}),
        pay.oc.ctx.chains,
        clock=pay.oc.clock,
    )
    async with pay.oc.ctx.sessionmaker() as s:
        with pytest.raises(SignerRefused, match="per-transaction"):
            await strict.sign_evm_transfer(s, payout.id, from_address=HOT, nonce=0, fees=fees)
        with pytest.raises(SignerRefused, match="no key"):
            await pay.signer.sign_evm_transfer(
                s, payout.id, from_address=SELLER_ADDRESSES[Network.ETH], nonce=0, fees=fees
            )
        payout_row = await s.get(Payout, payout.id)
        assert payout_row is not None
        payout_row.status = PayoutStatus.AWAITING_APPROVAL
        with pytest.raises(SignerRefused, match="not executable"):
            await pay.signer.sign_evm_transfer(s, payout.id, from_address=HOT, nonce=0, fees=fees)


def test_keystore_is_decrypted_at_start(tmp_path: Path, pay: Pay) -> None:
    (tmp_path / "hot.json").write_text(json.dumps(Account.encrypt(KEY, "correct horse")))
    settings = pay.oc.ctx.settings.model_copy(
        update={"signer_keystore_dir": str(tmp_path), "signer_password": SecretStr("correct horse")}
    )
    signer = Signer.from_keystore(settings, pay.oc.ctx.chains, clock=pay.oc.clock)
    assert signer.has_key(HOT)
    assert KEY[2:] not in repr(signer)
    bad = settings.model_copy(update={"signer_password": SecretStr("wrong")})
    with pytest.raises(ValueError):
        Signer.from_keystore(bad, pay.oc.ctx.chains, clock=pay.oc.clock)
