"""Deal state machine — a declarative, side-effect-free transition table.

The service layer (``app.services.deals``) locks the deal row, asks :func:`apply` whether the
action is allowed for this actor in the current status, then performs the side effects.
The table is the single place that answers "who may do what, when"; tests walk it exhaustively.

Money rule encoded here: the only edges into BUYER_CONFIRMED / RESOLVED_SELLER / SPLIT are a
buyer's confirmation and an admin's decision. No SYSTEM (timer) action can release money to the
seller — at most it opens a dispute or starts a refund.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.domain.enums import DealStatus as S
from app.domain.errors import Forbidden, InvalidTransition


class Role(StrEnum):
    """Roles of an actor *relative to a particular deal*."""

    BUYER = "buyer"
    SELLER = "seller"
    CREATOR = "creator"
    COUNTERPARTY = "counterparty"  # the (prospective) non-creator side
    SYSTEM = "system"  # timers, payment watchers
    ADMIN = "admin"  # dispute decisions; never a party of the deal


class Action(StrEnum):
    ACCEPT = "accept"
    CONFIRM_COUNTERPARTY = "confirm_counterparty"
    EDIT = "edit"
    SET_PAYOUT_ADDRESS = "set_payout_address"
    CANCEL = "cancel"
    START_PAYMENT = "start_payment"
    EXTEND_PAYMENT = "extend_payment"
    FUND = "fund"
    ACCEPT_UNDERPAYMENT = "accept_underpayment"
    EXPIRE = "expire"
    SELLER_CONFIRM = "seller_confirm"
    BUYER_CONFIRM = "buyer_confirm"
    START_PAYOUT = "start_payout"
    START_REFUND = "start_refund"
    REQUEST_CANCEL = "request_cancel"
    REVOKE_CANCEL = "revoke_cancel"
    REQUEST_REFUND = "request_refund"
    AGREE_REFUND = "agree_refund"
    OBJECT_REFUND = "object_refund"
    REFUND_WINDOW_ELAPSED = "refund_window_elapsed"
    OPEN_DISPUTE = "open_dispute"
    WITHDRAW_DISPUTE = "withdraw_dispute"
    RESOLVE_BUYER = "resolve_buyer"
    RESOLVE_SELLER = "resolve_seller"
    RESOLVE_SPLIT = "resolve_split"
    COMPLETE = "complete"
    COMPLETE_REFUND = "complete_refund"
    LATE_PAYMENT = "late_payment"
    NOTIFY = "notify"  # reminders / deadline notices: no state change


@dataclass(frozen=True)
class Rule:
    action: Action
    sources: frozenset[S]
    targets: frozenset[S]  # the source itself is listed when the action may keep the status
    actors: frozenset[Role]
    stays: bool = False  # the action never changes status (flags, reminders)


def _rule(action: Action, sources: Iterable[S], targets: Iterable[S] | None, actors: Iterable[Role]) -> Rule:
    src = frozenset(sources)
    if targets is None:  # the action never changes status (flags, reminders)
        return Rule(action, src, src, frozenset(actors), stays=True)
    return Rule(action, src, frozenset(targets), frozenset(actors))


PARTIES = (Role.BUYER, Role.SELLER)

RULES: tuple[Rule, ...] = (
    # ── before payment ─────────────────────────────────────────────────────
    _rule(Action.ACCEPT, [S.CREATED], [S.CREATED, S.ACCEPTED], [Role.COUNTERPARTY]),
    _rule(Action.CONFIRM_COUNTERPARTY, [S.CREATED], [S.CREATED, S.ACCEPTED], [Role.CREATOR]),
    _rule(Action.EDIT, [S.CREATED, S.ACCEPTED], [S.CREATED], [Role.CREATOR]),
    _rule(Action.SET_PAYOUT_ADDRESS, [S.CREATED, S.ACCEPTED, S.AWAITING_PAYMENT], None, [Role.SELLER]),
    _rule(
        Action.CANCEL,
        [S.CREATED, S.ACCEPTED],
        [S.CANCELLED],
        [Role.CREATOR, Role.COUNTERPARTY, *PARTIES],
    ),
    # Unilateral cancel while waiting for payment: nothing received → CANCELLED, part received → refund.
    _rule(Action.CANCEL, [S.AWAITING_PAYMENT], [S.CANCELLED, S.REFUND_PENDING], PARTIES),
    _rule(Action.START_PAYMENT, [S.ACCEPTED], [S.AWAITING_PAYMENT], [Role.BUYER]),
    _rule(Action.EXTEND_PAYMENT, [S.AWAITING_PAYMENT], None, [Role.BUYER]),
    _rule(Action.FUND, [S.AWAITING_PAYMENT], [S.FUNDED], [Role.SYSTEM]),
    _rule(Action.ACCEPT_UNDERPAYMENT, [S.AWAITING_PAYMENT], [S.FUNDED], [Role.SELLER]),
    _rule(Action.EXPIRE, [S.CREATED, S.ACCEPTED], [S.EXPIRED], [Role.SYSTEM]),
    _rule(Action.EXPIRE, [S.AWAITING_PAYMENT], [S.EXPIRED, S.REFUND_PENDING], [Role.SYSTEM]),
    # ── execution ──────────────────────────────────────────────────────────
    _rule(Action.SELLER_CONFIRM, [S.FUNDED], [S.SELLER_CONFIRMED], [Role.SELLER]),
    _rule(Action.BUYER_CONFIRM, [S.SELLER_CONFIRMED], [S.BUYER_CONFIRMED], [Role.BUYER]),
    _rule(
        Action.START_PAYOUT,
        [S.BUYER_CONFIRMED, S.RESOLVED_SELLER, S.SPLIT],
        [S.PAYOUT_PENDING],
        [Role.SYSTEM],
    ),
    _rule(Action.COMPLETE, [S.PAYOUT_PENDING], [S.COMPLETED], [Role.SYSTEM]),
    # ── cancellation and refunds after funding ─────────────────────────────
    _rule(
        Action.REQUEST_CANCEL,
        [S.FUNDED, S.SELLER_CONFIRMED],
        [S.FUNDED, S.SELLER_CONFIRMED, S.REFUND_PENDING],
        PARTIES,
    ),
    _rule(Action.REVOKE_CANCEL, [S.FUNDED, S.SELLER_CONFIRMED], None, PARTIES),
    _rule(Action.REQUEST_REFUND, [S.FUNDED], None, [Role.BUYER]),
    _rule(Action.AGREE_REFUND, [S.FUNDED], [S.REFUND_PENDING], [Role.SELLER]),
    _rule(Action.OBJECT_REFUND, [S.FUNDED], [S.DISPUTED], [Role.SELLER]),
    _rule(Action.REFUND_WINDOW_ELAPSED, [S.FUNDED], [S.REFUND_PENDING], [Role.SYSTEM]),
    _rule(Action.START_REFUND, [S.RESOLVED_BUYER], [S.REFUND_PENDING], [Role.SYSTEM]),
    _rule(Action.LATE_PAYMENT, [S.EXPIRED, S.CANCELLED], [S.REFUND_PENDING], [Role.SYSTEM]),
    _rule(Action.COMPLETE_REFUND, [S.REFUND_PENDING], [S.REFUNDED], [Role.SYSTEM]),
    # ── disputes ───────────────────────────────────────────────────────────
    _rule(Action.OPEN_DISPUTE, [S.FUNDED, S.SELLER_CONFIRMED], [S.DISPUTED], [*PARTIES, Role.SYSTEM]),
    _rule(Action.WITHDRAW_DISPUTE, [S.DISPUTED], [S.FUNDED, S.SELLER_CONFIRMED], PARTIES),
    _rule(Action.RESOLVE_BUYER, [S.DISPUTED], [S.RESOLVED_BUYER], [Role.ADMIN]),
    _rule(Action.RESOLVE_SELLER, [S.DISPUTED], [S.RESOLVED_SELLER], [Role.ADMIN]),
    _rule(Action.RESOLVE_SPLIT, [S.DISPUTED], [S.SPLIT], [Role.ADMIN]),
    # ── notices (reminders, deadline passed, SLA) ──────────────────────────
    _rule(Action.NOTIFY, [S.FUNDED, S.SELLER_CONFIRMED, S.DISPUTED], None, [Role.SYSTEM]),
)


def rules_for(action: Action, status: S) -> list[Rule]:
    return [r for r in RULES if r.action is action and status in r.sources]


def apply(status: S, action: Action, roles: Iterable[Role], *, target: S | None = None) -> S:
    """Validate an action and return the resulting status.

    ``target`` disambiguates actions with several outcomes (e.g. CANCEL while awaiting payment);
    without it the action must have exactly one possible outcome.
    Raises :class:`InvalidTransition` for a wrong status (this is also what a repeated button
    press gets) and :class:`Forbidden` for a wrong actor.
    """
    candidates = rules_for(action, status)
    if not candidates:
        raise InvalidTransition(f"{action} is not possible in status {status}", status=status, action=action)
    actor_roles = frozenset(roles)
    allowed = [r for r in candidates if r.actors & actor_roles]
    if not allowed:
        raise Forbidden(f"{action} is not allowed for this actor", status=status, action=action)
    if target is None and all(r.stays for r in allowed):
        return status
    targets = frozenset().union(*(r.targets for r in allowed))
    if target is None:
        if len(targets) != 1:
            raise InvalidTransition(f"{action} needs an explicit target", status=status, action=action)
        (only,) = targets
        return only
    if target not in targets:
        raise InvalidTransition(
            f"{action} cannot lead to {target}", status=status, action=action, target=target
        )
    return target
