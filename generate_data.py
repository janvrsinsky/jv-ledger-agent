"""Synthetic data generator for the Ledger accounting-ops demo.

Rebuilds a pristine, deterministic, date-relative fixture for the fictional
outdoor-gear shop Meridian Supply Co. Every run starts clean, and dates are
stamped relative to today so that "close yesterday's books" resolves on any
day the demo is filmed.

Nothing here is real: the company, its customers, orders, invoices, and the
whole payment batch are invented. There is no bank and no accounting system
behind these JSON files.

Run directly to (re)build the fixture:

    python generate_data.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from store import (
    BASE_CURRENCY,
    DEFAULT_STATE_DIR,
    _APPROVALS,
    _AUDIT,
    _INVOICES,
    _ORDERS,
    _PAYMENTS,
)

COMPANY = "Meridian Supply Co."


def _iso(offset_days: int) -> str:
    """Date stamped relative to today. Negative offset is in the past."""
    return (date.today() + timedelta(days=offset_days)).isoformat()


def build_fixture() -> dict[str, list[dict[str, Any]]]:
    """Return the full synthetic fixture as plain dicts.

    The batch is engineered so that one deterministic close exercises every
    policy branch: three exact EUR matches, one USD payment that reconciles
    only after a daily-rate conversion, a partial payment, a duplicate against
    an already-paid invoice, an order with no invoice yet, and an unreadable
    reference. Latent branches (overpayment, unconfigured FX currency, and any
    negative amount) are enforced by the engine and pinned by the test suite.
    """

    # -- orders -------------------------------------------------------------
    orders = [
        {"order_id": "ORD-2001", "customer": "Aspen Trailworks", "currency": "EUR", "total": 129.00, "placed": _iso(-6)},
        {"order_id": "ORD-2002", "customer": "Cascade Outfitters", "currency": "EUR", "total": 240.00, "placed": _iso(-5)},
        {"order_id": "ORD-2003", "customer": "Granite Peak Rentals", "currency": "EUR", "total": 88.50, "placed": _iso(-5)},
        {"order_id": "ORD-2004", "customer": "Northwind Gear (US)", "currency": "EUR", "total": 172.40, "placed": _iso(-4)},
        {"order_id": "ORD-2005", "customer": "Riverbend Supply", "currency": "EUR", "total": 495.00, "placed": _iso(-4)},
        {"order_id": "ORD-2006", "customer": "Summit Provisions", "currency": "EUR", "total": 315.00, "placed": _iso(-7)},
        {"order_id": "ORD-2007", "customer": "Tamarack Trading", "currency": "EUR", "total": 410.00, "placed": _iso(-3)},
    ]

    # -- invoices -----------------------------------------------------------
    # ORD-2006 is already paid (its duplicate payment must be refused).
    # ORD-2007 has NO invoice (the agent drafts one and holds).
    invoices = [
        {"invoice_id": "INV-1001", "order_id": "ORD-2001", "customer": "Aspen Trailworks", "amount_due": 129.00, "currency": "EUR", "status": "open", "issued": _iso(-6)},
        {"invoice_id": "INV-1002", "order_id": "ORD-2002", "customer": "Cascade Outfitters", "amount_due": 240.00, "currency": "EUR", "status": "open", "issued": _iso(-5)},
        {"invoice_id": "INV-1003", "order_id": "ORD-2003", "customer": "Granite Peak Rentals", "amount_due": 88.50, "currency": "EUR", "status": "open", "issued": _iso(-5)},
        {"invoice_id": "INV-1004", "order_id": "ORD-2004", "customer": "Northwind Gear (US)", "amount_due": 172.40, "currency": "EUR", "status": "open", "issued": _iso(-4)},
        {"invoice_id": "INV-1005", "order_id": "ORD-2005", "customer": "Riverbend Supply", "amount_due": 495.00, "currency": "EUR", "status": "open", "issued": _iso(-4)},
        {"invoice_id": "INV-1006", "order_id": "ORD-2006", "customer": "Summit Provisions", "amount_due": 315.00, "currency": "EUR", "status": "paid", "issued": _iso(-7), "settled_by": "PAY-2999"},
    ]

    # -- payment batch (yesterday's incoming bank feed) ---------------------
    y = _iso(-1)
    payments = [
        # 1..3 exact EUR matches -> EXACT_MATCH, auto-book.
        {"payment_id": "PAY-3001", "amount": 129.00, "currency": "EUR", "reference": "INV-1001", "received": y, "status": "unmatched"},
        {"payment_id": "PAY-3002", "amount": 240.00, "currency": "EUR", "reference": "Payment for ORD-2002 / INV-1002", "received": y, "status": "unmatched"},
        {"payment_id": "PAY-3003", "amount": 88.50, "currency": "EUR", "reference": "inv 1003", "received": y, "status": "unmatched"},
        # 4 USD payment -> FX_MATCH after daily-rate conversion (186.70 USD -> 172.40 EUR).
        {"payment_id": "PAY-3004", "amount": 186.70, "currency": "USD", "reference": "INV-1004", "received": y, "status": "unmatched"},
        # 5 partial payment (250 of 495 due) -> PARTIAL_PAYMENT, escalate.
        {"payment_id": "PAY-3005", "amount": 250.00, "currency": "EUR", "reference": "INV-1005", "received": y, "status": "unmatched"},
        # 6 duplicate against already-paid invoice -> INVOICE_ALREADY_PAID, propose refund.
        {"payment_id": "PAY-3006", "amount": 315.00, "currency": "EUR", "reference": "INV-1006", "received": y, "status": "unmatched"},
        # 7 payment for an order with no invoice yet -> draft invoice, hold.
        {"payment_id": "PAY-3007", "amount": 410.00, "currency": "EUR", "reference": "ORD-2007", "received": y, "status": "unmatched"},
        # 8 unreadable reference, no confident match -> escalate for research.
        {"payment_id": "PAY-3008", "amount": 74.90, "currency": "EUR", "reference": "REF ?? 88-91 //", "received": y, "status": "unmatched"},
    ]

    return {
        "orders": orders,
        "invoices": invoices,
        "payments": payments,
        "approvals": [],
        "audit_log": [],
    }


def write_fixture(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    """Write the fixture JSON files into `state_dir`, creating it if needed."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    fixture = build_fixture()
    mapping = {
        _ORDERS: fixture["orders"],
        _INVOICES: fixture["invoices"],
        _PAYMENTS: fixture["payments"],
        _APPROVALS: fixture["approvals"],
        _AUDIT: fixture["audit_log"],
    }
    for name, data in mapping.items():
        with (state_dir / name).open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    return state_dir


if __name__ == "__main__":
    target = write_fixture()
    print("Wrote synthetic fixture for " + COMPANY + " to " + str(target))
    print(
        "Base currency "
        + BASE_CURRENCY
        + ". 7 orders, 6 invoices, 8 incoming payments. Dates relative to today."
    )
