"""TRC-20 payouts: a locally built, locally signed TriggerSmartContract through fake TRON nodes."""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from eth_account import Account

from app.chains.adapters.tron import TronAdapter, tron_hex
from app.chains.pool import EndpointPool
from app.config import ChainsConfig, RpcEndpoint
from app.db.models import Payout, PayoutAttempt
from app.domain.enums import DealStatus, Network, PayoutAttemptStatus, PayoutStatus
from app.providers.onchain.gateway import OnchainGateway
from app.services.gateways import RoutingGateway
from app.services.ledger import AccountRef
from app.signer.signer import Signer, tron_address
from app.worker.onchain_payouts import TronPayouts
from tests.chains_fake import FakeTronNode, _proto, routing_transport
from tests.integration.chain_world import OC, usdt
from tests.integration.deal_world import BUYER, SELLER, SELLER_ADDRESSES, Clock

TRON = Network.TRON
U = 10**6
KEY = "0x" + "5d" * 32
ACCOUNT = Account.from_key(KEY)
HOT = tron_address(ACCOUNT)


@dataclass
class TronPay:
    oc: OC
    adapter: TronAdapter
    grid: FakeTronNode
    node: FakeTronNode
    signer: Signer
    contract: str

    @property
    def executor(self) -> TronPayouts:
        return TronPayouts(self.oc.ctx, self.adapter, self.signer)

    @property
    def nodes(self) -> tuple[FakeTronNode, FakeTronNode]:
        return (self.grid, self.node)

    def advance(self, ms: int) -> None:
        for n in self.nodes:
            n.latest_ts += ms
            n.solid_ts += ms
        self.oc.clock.advance(timedelta(milliseconds=ms))

    def fund(self, usdt_amount: int, trx: int = 100 * U) -> None:
        for n in self.nodes:
            n.balances[HOT] = usdt_amount
            n.trx[HOT] = trx

    async def payouts(self) -> list[Payout]:
        async with self.oc.ctx.sessionmaker() as s:
            return list((await s.execute(sa.select(Payout).order_by(Payout.id))).scalars())

    async def attempts(self) -> list[PayoutAttempt]:
        async with self.oc.ctx.sessionmaker() as s:
            return list((await s.execute(sa.select(PayoutAttempt).order_by(PayoutAttempt.id))).scalars())

    async def confirmed_deal(self) -> int:
        deal_id, request = await self.oc.awaiting(TRON)
        paid_at = self.grid.latest_ts + 3000
        for n in self.nodes:
            n.transfer(
                contract=self.contract, to=HOT, amount=request.expected_amount, ts=paid_at, tx=f"in{deal_id}"
            )
        self.advance(120_000)  # solidified (the solid head trails the latest by a minute)
        await self.oc.tick(TRON)
        assert (await self.oc.deal(deal_id)).status is DealStatus.FUNDED
        async with self.oc.ctx.uow() as uow:
            await uow.deals.seller_confirm(SELLER, deal_id)
            await uow.deals.buyer_confirm(BUYER, deal_id)
        return deal_id


@pytest.fixture
async def tp(oc: OC, chains: ChainsConfig) -> TronPay:
    cfg = chains.networks[TRON].model_copy(
        update={
            "deposit_address": HOT,
            "rpc": (RpcEndpoint(name="grid", url="https://grid.test"),),
            "verify_rpc": (RpcEndpoint(name="node", url="https://node.test"),),
        }
    )
    new_chains = ChainsConfig(networks=dict(chains.networks) | {TRON: cfg})
    oc.ctx = dataclasses.replace(
        oc.ctx,
        chains=new_chains,
        gateway=RoutingGateway({TRON: OnchainGateway(oc.ctx.settings, new_chains, clock=oc.clock)}),
    )
    hosts = {"grid.test": FakeTronNode(), "node.test": FakeTronNode()}
    transport = routing_transport(hosts)
    adapter = TronAdapter(
        cfg,
        addresses=(HOT,),
        primary=EndpointPool(cfg.rpc, transport=transport, env={}),
        verifier=EndpointPool(cfg.verify_rpc, transport=transport, env={}),
    )
    oc.adapters[TRON] = adapter  # type: ignore[assignment]
    signer = Signer({HOT: ACCOUNT}, oc.ctx.settings, new_chains, clock=oc.clock)
    world = TronPay(oc, adapter, hosts["grid.test"], hosts["node.test"], signer, cfg.contract)
    await oc.tick(TRON)
    return world


async def test_trc20_payout_end_to_end(tp: TronPay) -> None:
    deal_id = await tp.confirmed_deal()
    [payout] = await tp.payouts()
    tp.fund(10_000 * U)

    assert await tp.executor.sign_next()
    [attempt] = await tp.attempts()
    assert attempt.valid_until is not None and attempt.raw_tx is not None
    # the bytes we signed transfer exactly the payout to exactly the seller, from the real contract
    raw = _proto(attempt.raw_tx)[1][0]
    assert hashlib.sha256(raw).hexdigest() == attempt.tx_hash
    trigger = _proto(_proto(_proto(_proto(raw)[11][0])[2][0])[2][0])
    data = trigger[4][0]
    assert data[:4].hex() == "a9059cbb"
    assert data[16:36].hex() == tron_hex(SELLER_ADDRESSES[TRON])
    assert int.from_bytes(data[36:68], "big") == payout.amount
    assert trigger[2][0][1:].hex() == tron_hex(tp.contract)
    signature = _proto(attempt.raw_tx)[2][0]
    recovered = Account._recover_hash(bytes.fromhex(attempt.tx_hash), signature=signature)
    assert recovered == ACCOUNT.address  # signed by the hot wallet's key

    assert await tp.executor.broadcast_next()
    assert attempt.tx_hash in tp.grid.sent and attempt.tx_hash in tp.node.sent
    for n in tp.nodes:
        n.solidify(attempt.tx_hash)
    tp.advance(20_000)
    assert await tp.executor.track_next()
    [payout] = await tp.payouts()
    assert payout.status is PayoutStatus.CONFIRMED
    assert payout.native_fee_spent == 13_000_000  # 13 TRX burnt for energy
    assert (await tp.oc.deal(deal_id)).status is DealStatus.COMPLETED
    assert await tp.oc.balance(AccountRef.payable(usdt(TRON), deal_id)) == 0


async def test_expired_transaction_is_signed_again_only_when_provably_dead(tp: TronPay) -> None:
    await tp.confirmed_deal()
    tp.fund(10_000 * U)
    await tp.executor.sign_next()
    await tp.executor.broadcast_next()
    [first] = await tp.attempts()
    for n in tp.nodes:
        n.sent.clear()  # dropped from the mempool, never included
    tp.advance(5 * 60_000)  # 5 min: not expired yet → only re-broadcast
    await tp.executor.track_next()
    assert (await tp.payouts())[0].status is PayoutStatus.BROADCAST
    tp.advance(9 * 60_000)  # past expiration (10 min) + 2 min margin at both sources
    await tp.executor.track_next()
    [payout] = await tp.payouts()
    assert payout.status is PayoutStatus.QUEUED
    assert (await tp.attempts())[0].status is PayoutAttemptStatus.EXPIRED
    await tp.executor.sign_next()
    first_again, second = await tp.attempts()
    assert first_again.tx_hash == first.tx_hash
    assert second.tx_hash != first.tx_hash
    assert second.attempt_no == 2


async def test_failed_energy_is_final(tp: TronPay) -> None:
    await tp.confirmed_deal()
    tp.fund(10_000 * U)
    await tp.executor.sign_next()
    await tp.executor.broadcast_next()
    [attempt] = await tp.attempts()
    for n in tp.nodes:
        n.solidify(attempt.tx_hash, result="OUT_OF_ENERGY", fee=30_000_000)
    tp.advance(20_000)
    await tp.executor.track_next()
    [payout] = await tp.payouts()
    assert payout.status is PayoutStatus.FAILED_FINAL
    assert await tp.oc.balance(AccountRef.network_fee_expense("TRX@tron")) == 30_000_000


async def test_not_enough_trx_for_energy_waits(tp: TronPay) -> None:
    await tp.confirmed_deal()
    tp.fund(10_000 * U, trx=5 * U)  # the fee limit is 30 TRX
    await tp.executor.sign_next()
    assert ((await tp.payouts())[0].last_error or "").startswith("no_gas")
    assert await tp.attempts() == []


async def test_tron_is_signed_again_only_after_expiry(tp: TronPay) -> None:
    from app.chains.tron_tx import RefBlock  # noqa: PLC0415
    from app.signer.signer import SignerRefused  # noqa: PLC0415

    await tp.confirmed_deal()
    [payout] = await tp.payouts()
    ref = RefBlock(number=1, block_id="00" * 32)
    now_ms = 1_800_000_000_000
    expires = datetime.fromtimestamp((now_ms + 600_000) / 1000, tz=UTC)
    clock = Clock(expires - timedelta(minutes=5))
    signer = Signer({HOT: ACCOUNT}, tp.oc.ctx.settings, tp.oc.ctx.chains, clock=clock)
    async with tp.oc.ctx.sessionmaker() as s, s.begin():
        await signer.sign_tron_transfer(
            s, payout.id, from_address=HOT, ref=ref, now_ms=now_ms, fee_limit_sun=1
        )
    with pytest.raises(SignerRefused, match="already signed"):
        async with tp.oc.ctx.sessionmaker() as s, s.begin():
            await signer.sign_tron_transfer(
                s, payout.id, from_address=HOT, ref=ref, now_ms=now_ms, fee_limit_sun=1
            )
    clock.now = expires + timedelta(minutes=3)  # the first one can no longer be included
    async with tp.oc.ctx.sessionmaker() as s, s.begin():
        await signer.sign_tron_transfer(
            s, payout.id, from_address=HOT, ref=ref, now_ms=now_ms + 1, fee_limit_sun=1
        )
