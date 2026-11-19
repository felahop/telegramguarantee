"""Pure bot helpers: parsing, wizard step order, rendering, keyboards, error texts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.bot import texts as T
from app.bot.callbacks import DealAct, DealCb, WizAct, WizCb
from app.bot.drafts import DealDraft, Step, applicable_steps, first_missing, previous_step
from app.bot.keyboards import deal_card_kb, share_url
from app.bot.parsing import ParseError, normalize_username, parse_deal_text, parse_hours
from app.bot.render import PartyInfo, party_line, render_deal_card
from app.config import ChainsConfig
from app.db.models import Deal
from app.domain.enums import DealSource, DealStatus, FeePayer, Network, NetworkFeePayer, PartyRole, Provider
from app.domain.errors import (
    DomainError,
    Forbidden,
    InvalidTransition,
    LimitExceeded,
    ValidationFailed,
    VersionConflict,
)


class TestParseDeal:
    def test_full_command(self) -> None:
        p = parse_deal_text("@bob_seller 150 USDT trc20 Аккаунт Steam с играми")
        assert (p.username, p.amount, p.network, p.terms) == (
            "bob_seller",
            Decimal("150"),
            Network.TRON,
            "Аккаунт Steam с играми",
        )

    def test_order_and_case_insensitive(self) -> None:
        p = parse_deal_text("150,50 BEP20 usdt логотип")
        assert (p.amount, p.network, p.terms) == (Decimal("150.50"), Network.BSC, "логотип")

    def test_glued_currency_and_no_username(self) -> None:
        p = parse_deal_text("99.9usdt дизайн")
        assert (p.username, p.amount, p.network, p.terms) == (None, Decimal("99.9"), None, "дизайн")

    def test_terms_keep_line_breaks(self) -> None:
        p = parse_deal_text("10 usdt первая строка\nвторая строка")
        assert p.terms == "первая строка\nвторая строка"

    def test_empty(self) -> None:
        p = parse_deal_text("   ")
        assert (p.username, p.amount, p.network, p.terms) == (None, None, None, None)

    def test_only_username(self) -> None:
        assert parse_deal_text("@alice").username == "alice"

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("@bob_seller сто долларов", "no_amount"),
            ("150 RUB подписка", "unsupported_currency"),
            ("150 btc", "unsupported_currency"),
            ("@ab 150", "bad_username"),
            ("-5 usdt", "no_amount"),
            ("150.123 usdt", "no_amount"),
        ],
    )
    def test_errors(self, text: str, reason: str) -> None:
        with pytest.raises(ParseError) as err:
            parse_deal_text(text)
        assert err.value.context["reason"] == reason

    @pytest.mark.parametrize(
        ("alias", "network"),
        [
            ("cb", Network.CRYPTOBOT),
            ("trx", Network.TRON),
            ("erc-20", Network.ETH),
            ("ton", Network.TON),
            ("solana", Network.SOL),
            ("bsc", Network.BSC),
        ],
    )
    def test_network_aliases(self, alias: str, network: Network) -> None:
        assert parse_deal_text(f"10 {alias} x").network is network

    def test_username_normalization(self) -> None:
        assert normalize_username("@Some_User1") == "Some_User1"
        with pytest.raises(ParseError):
            normalize_username("@1abc")


class TestParseHours:
    @pytest.mark.parametrize(
        ("text", "hours"),
        [
            ("12", 12),
            ("12ч", 12),
            ("12 h", 12),
            ("3д", 72),
            ("3 дня", 72),
            ("2 суток", 48),
            ("1 день", 24),
        ],
    )
    def test_ok(self, text: str, hours: int) -> None:
        assert parse_hours(text, max_hours=720) == hours

    @pytest.mark.parametrize("text", ["0", "1000", "abc", "3 недели", "31д"])
    def test_bad(self, text: str) -> None:
        with pytest.raises(ParseError):
            parse_hours(text, max_hours=720)


class TestDraftSteps:
    def test_fresh_draft_starts_with_role(self) -> None:
        assert first_missing(DealDraft()) is Step.ROLE

    def test_address_only_for_onchain_seller(self) -> None:
        buyer = DealDraft(role=PartyRole.BUYER, network=Network.TRON)
        seller = DealDraft(role=PartyRole.SELLER, network=Network.TRON)
        cb_seller = DealDraft(role=PartyRole.SELLER, network=Network.CRYPTOBOT)
        assert Step.ADDRESS not in applicable_steps(buyer)
        assert Step.ADDRESS in applicable_steps(seller)
        assert Step.ADDRESS not in applicable_steps(cb_seller)

    def test_complete_draft_goes_to_preview(self) -> None:
        d = DealDraft(
            role=PartyRole.BUYER,
            network=Network.CRYPTOBOT,
            amount="10",
            terms="abc",
            hours=24,
            fee_payer=FeePayer.BUYER,
            counterparty_chosen=True,
        )
        assert first_missing(d) is Step.PREVIEW

    def test_changing_network_resets_address(self) -> None:
        d = DealDraft(role=PartyRole.SELLER, network=Network.TRON, payout_address="T...")
        d.set_network(Network.BSC)
        assert d.payout_address is None
        assert first_missing(d) is Step.AMOUNT

    def test_editing_skips_role_network_counterparty(self) -> None:
        d = DealDraft(edit_deal_id=1, role=PartyRole.BUYER, network=Network.TRON)
        assert Step.ROLE not in applicable_steps(d)
        assert Step.COUNTERPARTY not in applicable_steps(d)
        assert previous_step(d, Step.AMOUNT) is None

    def test_back_navigation(self) -> None:
        d = DealDraft(role=PartyRole.SELLER, network=Network.TRON)
        assert previous_step(d, Step.COUNTERPARTY) is Step.ADDRESS
        assert previous_step(d, Step.NETWORK) is Step.ROLE

    def test_round_trip(self) -> None:
        d = DealDraft(
            role=PartyRole.SELLER,
            network=Network.SOL,
            amount="5.5",
            fee_payer=FeePayer.SPLIT,
            source=DealSource.COMMAND,
        )
        assert DealDraft.from_dict(d.to_dict()) == d
        assert DealDraft.from_dict({**d.to_dict(), "unknown": 1}) == d


def _deal(**kw: object) -> Deal:
    values: dict[str, object] = {
        "id": 7,
        "code": "ABCD2345",
        "invite_token": "tok",
        "version": 1,
        "status": DealStatus.CREATED,
        "creator_id": 1,
        "creator_role": PartyRole.BUYER,
        "buyer_id": 1,
        "seller_id": None,
        "invited_user_id": None,
        "invited_username": None,
        "pending_counterparty_id": None,
        "terms": "<script>alert(1)</script> & логотип",
        "provider": Provider.CRYPTOBOT,
        "network": Network.CRYPTOBOT,
        "decimals": 6,
        "amount": 100_000_000,
        "fee_payer": FeePayer.BUYER,
        "fee_rate_bps": 200,
        "fee_min": 1_000_000,
        "fee_amount": 2_000_000,
        "network_fee_amount": 0,
        "network_fee_payer": NetworkFeePayer.SELLER,
        "buyer_pays": 102_000_000,
        "seller_receives": 100_000_000,
        "execution_hours": 24,
        "execution_deadline_at": None,
        "seller_payout_address": None,
    }
    values.update(kw)
    return Deal(**values)


NOW = datetime(2026, 3, 1, tzinfo=UTC)


class TestRender:
    def test_card_escapes_user_content(self, chains: ChainsConfig) -> None:
        text = render_deal_card(
            _deal(), viewer_id=1, parties={}, spec=chains.spec(Network.CRYPTOBOT), now=NOW
        )
        assert "<script>" not in text
        assert "&lt;script&gt;" in text
        assert "&amp; логотип" in text
        assert "К оплате: <b>102.00 USDT</b>" in text
        assert "Продавец получит: <b>100.00 USDT</b>" in text
        assert "Комиссия сервиса 2%: 2.00 USDT (платит покупатель)" in text
        assert T.SAFETY_FOOTER in text

    def test_onchain_card_shows_network_fee(self, chains: ChainsConfig) -> None:
        deal = _deal(
            provider=Provider.ONCHAIN,
            network=Network.TRON,
            network_fee_amount=1_500_000,
            seller_receives=98_500_000,
        )
        text = render_deal_card(deal, viewer_id=1, parties={}, spec=chains.spec(Network.TRON), now=NOW)
        assert "Сетевая комиссия выплаты: 1.50 USDT (с продавца)" in text

    def test_username_change_warning(self) -> None:
        info = PartyInfo(
            user_id=2,
            username="bob",
            first_name="Bob",
            deals_completed=3,
            disputes_lost=0,
            username_changed_at=NOW - timedelta(days=2),
        )
        line = party_line(PartyRole.SELLER, info, now=NOW, placeholder="")
        assert "@bob — 3 сделки" in line
        assert "сменил username 2 дн. назад" in line

    def test_new_user_flag(self) -> None:
        info = PartyInfo(
            user_id=2,
            username=None,
            first_name="<b>Bob</b>",
            deals_completed=0,
            disputes_lost=0,
            username_changed_at=None,
        )
        line = party_line(PartyRole.SELLER, info, now=NOW, placeholder="")
        assert "&lt;b&gt;Bob&lt;/b&gt;" in line
        assert "новый пользователь" in line

    def test_payout_address_shown_to_seller_only(self, chains: ChainsConfig) -> None:
        deal = _deal(
            seller_id=2, seller_payout_address="TAddr", provider=Provider.ONCHAIN, network=Network.TRON
        )
        spec = chains.spec(Network.TRON)
        assert "TAddr" in render_deal_card(deal, viewer_id=2, parties={}, spec=spec, now=NOW)
        assert "TAddr" not in render_deal_card(deal, viewer_id=1, parties={}, spec=spec, now=NOW)


class TestKeyboards:
    def _actions(self, deal: Deal, viewer: int) -> set[str]:
        kb = deal_card_kb(deal, viewer, bot_username="escrow_bot")
        result = set()
        for row in kb.inline_keyboard:
            for button in row:
                if button.callback_data:
                    result.add(DealCb.unpack(button.callback_data).a.value)
                else:
                    result.add("url")
        return result

    def test_creator_sees_edit_cancel_share(self) -> None:
        assert self._actions(_deal(), 1) == {"edt", "cnl", "url", "v"}

    def test_invitee_sees_accept_decline(self) -> None:
        assert self._actions(_deal(invited_user_id=2), 2) == {"acc", "cnl", "v"}

    def test_creator_confirms_candidate(self) -> None:
        assert {"cpy", "cpn"} <= self._actions(_deal(pending_counterparty_id=3), 1)

    def test_terminal_deal_can_be_repeated(self) -> None:
        deal = _deal(status=DealStatus.COMPLETED, seller_id=2)
        assert self._actions(deal, 2) == {"rpt", "tpl", "v"}

    def test_callback_data_fits_telegram_limit(self) -> None:
        big = 2**62
        assert len(DealCb(a=DealAct.CANCEL_CONFIRM, id=big, v=10**6).pack().encode()) <= 64
        assert len(WizCb(a=WizAct.ADDRESS, val=str(big)).pack().encode()) <= 64
        assert len(WizCb(a=WizAct.AMOUNT, val="999999999999.99").pack().encode()) <= 64

    def test_share_url_is_encoded(self) -> None:
        url = share_url("https://t.me/bot?start=j_abc", "Сделка & условия")
        assert "start%3Dj_abc" in url
        assert "%26" in url


class TestErrorTexts:
    @pytest.mark.parametrize(
        ("exc", "fragment"),
        [
            (LimitExceeded("x", kind="min", limit="5"), "Минимальная сумма сделки — 5 USDT"),
            (LimitExceeded("x", kind="max", limit="300"), "300 USDT"),
            (VersionConflict("x"), "изменились"),
            (InvalidTransition("x"), "уже выполнено"),
            (Forbidden("another"), "другому пользователю"),
            (ValidationFailed("x", field="counterparty"), "самим собой"),
            (ParseError("x", reason="unsupported_currency"), "только в USDT"),
        ],
    )
    def test_mapping(self, exc: Exception, fragment: str) -> None:
        assert fragment in T.error_text(exc)  # type: ignore[arg-type]

    def test_unknown_error_is_generic(self) -> None:
        assert T.error_text(DomainError("internal details here")) == "Не удалось выполнить действие."
