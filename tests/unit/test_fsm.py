"""Exhaustive checks of the deal state machine table."""

from __future__ import annotations

from collections import deque

import pytest

from app.domain.enums import TERMINAL_STATUSES, TRANSIT_STATUSES
from app.domain.enums import DealStatus as S
from app.domain.errors import Forbidden, InvalidTransition
from app.domain.fsm import RULES, Action, Role, apply, rules_for

ALL_ROLES = set(Role)


def test_happy_path() -> None:
    steps = [
        (S.CREATED, Action.ACCEPT, Role.COUNTERPARTY, S.ACCEPTED),
        (S.ACCEPTED, Action.START_PAYMENT, Role.BUYER, S.AWAITING_PAYMENT),
        (S.AWAITING_PAYMENT, Action.FUND, Role.SYSTEM, S.FUNDED),
        (S.FUNDED, Action.SELLER_CONFIRM, Role.SELLER, S.SELLER_CONFIRMED),
        (S.SELLER_CONFIRMED, Action.BUYER_CONFIRM, Role.BUYER, S.BUYER_CONFIRMED),
        (S.BUYER_CONFIRMED, Action.START_PAYOUT, Role.SYSTEM, S.PAYOUT_PENDING),
        (S.PAYOUT_PENDING, Action.COMPLETE, Role.SYSTEM, S.COMPLETED),
    ]
    for source, action, role, target in steps:
        targets = {r for rule in rules_for(action, source) for r in rule.targets}
        expected = target if len(targets) == 1 else None
        assert apply(source, action, {role}, target=None if expected else target) is target


def test_buyer_cannot_confirm_before_seller() -> None:
    for status in (S.FUNDED, S.AWAITING_PAYMENT, S.ACCEPTED, S.DISPUTED):
        with pytest.raises(InvalidTransition):
            apply(status, Action.BUYER_CONFIRM, {Role.BUYER})


@pytest.mark.parametrize("roles", [{Role.SELLER}, {Role.SYSTEM}, {Role.ADMIN}, {Role.CREATOR}, set()])
def test_only_buyer_confirms_receipt(roles: set[Role]) -> None:
    with pytest.raises(Forbidden):
        apply(S.SELLER_CONFIRMED, Action.BUYER_CONFIRM, roles)


def test_unknown_combinations_are_invalid() -> None:
    for status in S:
        for action in Action:
            if not rules_for(action, status):
                with pytest.raises(InvalidTransition):
                    apply(status, action, ALL_ROLES)


def test_ambiguous_outcome_requires_target() -> None:
    with pytest.raises(InvalidTransition, match="explicit target"):
        apply(S.AWAITING_PAYMENT, Action.CANCEL, {Role.BUYER})
    assert apply(S.AWAITING_PAYMENT, Action.CANCEL, {Role.BUYER}, target=S.CANCELLED) is S.CANCELLED
    with pytest.raises(InvalidTransition):
        apply(S.AWAITING_PAYMENT, Action.CANCEL, {Role.BUYER}, target=S.COMPLETED)


SELLER_MONEY = {S.BUYER_CONFIRMED, S.RESOLVED_SELLER, S.SPLIT}


def test_no_system_action_releases_money_to_seller() -> None:
    """Timers can never pay the seller: no SYSTEM rule leads into a seller-money status
    except from a status that itself requires the buyer or an admin."""
    for rule in RULES:
        if Role.SYSTEM in rule.actors:
            assert not (rule.targets & SELLER_MONEY), rule


def test_seller_money_only_via_buyer_or_admin() -> None:
    for rule in RULES:
        if rule.targets & SELLER_MONEY:
            assert rule.actors <= {Role.BUYER, Role.ADMIN}, rule
            if rule.targets & {S.BUYER_CONFIRMED}:
                assert rule.actors == {Role.BUYER}


def _reachable(start: S, allowed: list[tuple[S, S]]) -> set[S]:
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for src, dst in allowed:
            if src is node and dst not in seen:
                seen.add(dst)
                queue.append(dst)
    return seen


def test_completion_requires_buyer_or_admin_decision() -> None:
    """Graph search: with the buyer's confirmation and the admin's seller/split decisions removed,
    a funded deal can never reach a payout to the seller."""
    edges = [
        (src, dst)
        for rule in RULES
        if rule.action not in (Action.BUYER_CONFIRM, Action.RESOLVE_SELLER, Action.RESOLVE_SPLIT)
        for src in rule.sources
        for dst in rule.targets
    ]
    reachable = _reachable(S.FUNDED, edges)
    assert S.PAYOUT_PENDING not in reachable
    assert S.COMPLETED not in reachable
    assert S.REFUNDED in reachable  # refunds remain possible


def test_admin_only_decides_disputes() -> None:
    for rule in RULES:
        if Role.ADMIN in rule.actors:
            assert rule.action in (Action.RESOLVE_BUYER, Action.RESOLVE_SELLER, Action.RESOLVE_SPLIT)
            assert rule.sources == {S.DISPUTED}


def test_terminal_statuses() -> None:
    for status in TERMINAL_STATUSES:
        outgoing = {r.action for r in RULES if status in r.sources}
        if status in (S.EXPIRED, S.CANCELLED):
            assert outgoing == {Action.LATE_PAYMENT}  # only a late payment can reopen money flow
        else:
            assert outgoing == set()


def test_transit_statuses_only_move_on_by_system() -> None:
    for status in TRANSIT_STATUSES:
        rules = [r for r in RULES if status in r.sources]
        assert rules, status
        for rule in rules:
            assert rule.actors == {Role.SYSTEM}
            assert rule.targets <= {S.PAYOUT_PENDING, S.REFUND_PENDING}


def test_disputes_only_after_funding() -> None:
    sources = {s for r in RULES if r.action is Action.OPEN_DISPUTE for s in r.sources}
    assert sources == {S.FUNDED, S.SELLER_CONFIRMED}


def test_cancel_after_funding_is_never_unilateral() -> None:
    assert not rules_for(Action.CANCEL, S.FUNDED)
    assert not rules_for(Action.CANCEL, S.SELLER_CONFIRMED)


def test_every_status_is_reachable_from_created() -> None:
    edges = [(src, dst) for rule in RULES for src in rule.sources for dst in rule.targets]
    assert _reachable(S.CREATED, edges) == set(S)
