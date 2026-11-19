"""Deal draft kept in the wizard's FSM storage, and the wizard step order.

The wizard always shows the *first missing* step, so the same machinery serves a fresh wizard,
a draft prefilled from a one-line command, "repeat deal", templates and editing a deal: whatever
is already filled is skipped and the user lands on the preview.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.domain.enums import DealSource, FeePayer, Network, PartyRole, Provider


class Step(StrEnum):
    ROLE = "role"
    NETWORK = "network"
    AMOUNT = "amount"
    TERMS = "terms"
    HOURS = "hours"
    FEE = "fee"
    ADDRESS = "address"
    COUNTERPARTY = "counterparty"
    PREVIEW = "preview"


ORDER: tuple[Step, ...] = tuple(Step)


@dataclass
class DealDraft:
    role: PartyRole | None = None
    network: Network | None = None
    amount: str | None = None  # Decimal as string (FSM storage is JSON)
    terms: str | None = None
    hours: int | None = None
    fee_payer: FeePayer | None = None
    payout_address: str | None = None
    counterparty_user_id: int | None = None
    counterparty_username: str | None = None
    counterparty_label: str | None = None
    counterparty_chosen: bool = False  # True also for "no counterparty, give me a link"
    source: DealSource = DealSource.WIZARD
    edit_deal_id: int | None = None
    edit_version: int | None = None

    # ── serialization ───────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {k: (v.value if isinstance(v, StrEnum) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DealDraft:
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        for key, enum_cls in (
            ("role", PartyRole),
            ("network", Network),
            ("fee_payer", FeePayer),
            ("source", DealSource),
        ):
            if values.get(key) is not None:
                values[key] = enum_cls(values[key])
        return cls(**values)

    # ── derived ─────────────────────────────────────────────────────────
    @property
    def editing(self) -> bool:
        return self.edit_deal_id is not None

    @property
    def amount_decimal(self) -> Decimal | None:
        return Decimal(self.amount) if self.amount is not None else None

    @property
    def needs_payout_address(self) -> bool:
        return (
            self.role is PartyRole.SELLER
            and self.network is not None
            and self.network.provider is Provider.ONCHAIN
        )

    # ── setters that keep the draft consistent ──────────────────────────
    def set_role(self, role: PartyRole) -> None:
        if role is not self.role:
            self.role = role
            self.payout_address = None

    def set_network(self, network: Network) -> None:
        if network is not self.network:
            self.network = network
            self.payout_address = None  # an address belongs to one network

    def set_counterparty(self, *, user_id: int | None, username: str | None, label: str | None) -> None:
        self.counterparty_user_id = user_id
        self.counterparty_username = username
        self.counterparty_label = label
        self.counterparty_chosen = True


def applicable_steps(draft: DealDraft) -> list[Step]:
    steps = list(ORDER)
    if draft.editing:  # who and where cannot change after creation
        steps = [s for s in steps if s not in (Step.ROLE, Step.NETWORK, Step.COUNTERPARTY)]
    if not draft.needs_payout_address:
        steps.remove(Step.ADDRESS)
    return steps


def is_filled(draft: DealDraft, step: Step) -> bool:
    return {
        Step.ROLE: draft.role is not None,
        Step.NETWORK: draft.network is not None,
        Step.AMOUNT: draft.amount is not None,
        Step.TERMS: draft.terms is not None,
        Step.HOURS: draft.hours is not None,
        Step.FEE: draft.fee_payer is not None,
        Step.ADDRESS: draft.payout_address is not None,
        Step.COUNTERPARTY: draft.counterparty_chosen,
        Step.PREVIEW: False,
    }[step]


def first_missing(draft: DealDraft) -> Step:
    for step in applicable_steps(draft):
        if not is_filled(draft, step):
            return step
    return Step.PREVIEW


def previous_step(draft: DealDraft, current: Step) -> Step | None:
    steps = applicable_steps(draft)
    if current not in steps:
        return None
    index = steps.index(current)
    return steps[index - 1] if index > 0 else None
