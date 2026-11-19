"""Callback data factories (each packed value must fit Telegram's 64-byte limit).

Callback data comes from the client and is treated as untrusted input: handlers always re-check
that the presser is the right party and that the deal is in the right status (the service does).
"""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData


class DealAct(StrEnum):
    VIEW = "v"
    ACCEPT = "acc"
    CANCEL = "cnl"  # asks for confirmation
    CANCEL_CONFIRM = "cnlok"
    EDIT = "edt"
    CONFIRM_CANDIDATE = "cpy"
    REJECT_CANDIDATE = "cpn"
    REPEAT = "rpt"
    SAVE_TEMPLATE = "tpl"
    PAY = "pay"
    CHECK_PAYMENT = "chk"
    # after funding
    SELLER_DONE = "sd"
    BUYER_GOT = "bg"  # shows the irreversible-confirmation screen
    BUYER_GOT_OK = "bgok"  # v = one-time confirmation token
    REQ_CANCEL = "rc"
    REQ_CANCEL_OK = "rcok"
    REVOKE_CANCEL = "rv"
    REQ_REFUND = "rr"
    AGREE_REFUND = "ar"
    AGREE_REFUND_OK = "arok"
    OBJECT_REFUND = "or"
    DISPUTE = "dp"
    DISPUTE_NO_REASON = "dpn"
    WITHDRAW_DISPUTE = "wd"
    REFUND_ADDRESS = "ra"
    CHAT = "ch"
    CHAT_CLOSE = "chx"


class DealCb(CallbackData, prefix="d"):
    a: DealAct
    id: int
    v: int = 0  # deal version the user saw (stale cards are rejected)


class WizAct(StrEnum):
    ROLE = "role"
    NETWORK = "net"
    AMOUNT = "amt"
    KEEP = "keep"  # keep the current value (editing)
    HOURS = "hrs"
    HOURS_CUSTOM = "hrsc"
    FEE = "fee"
    ADDRESS = "addr"
    COUNTERPARTY = "cp"
    CONTACT = "contact"
    OPEN_LINK = "link"
    BACK = "back"
    GOTO = "goto"
    FIELDS = "fields"
    CREATE = "create"
    CANCEL = "cancel"


class WizCb(CallbackData, prefix="w"):
    a: WizAct
    val: str = ""


class MenuAct(StrEnum):
    NEW = "new"
    DEALS = "deals"
    TEMPLATES = "tpls"
    HELP = "help"
    MAIN = "main"


class MenuCb(CallbackData, prefix="m"):
    a: MenuAct
    page: int = 0


class TplAct(StrEnum):
    USE = "use"
    DELETE = "del"


class TplCb(CallbackData, prefix="t"):
    a: TplAct
    id: int
