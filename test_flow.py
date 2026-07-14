"""End-to-end policy check for the Ledger accounting-ops agent.

Because the policy gate is deterministic, correctness here means pinning every
hazard branch, not measuring a distribution. This suite rebuilds fresh state in
a throwaway temp directory (so it never touches the demo state/ fixture), runs
the exact flow the agent runs on camera, asserts one outcome per hazard, and
exits non-zero on any drift.

Runs on stdlib only (no FastMCP, no network). Run it directly:

    python test_flow.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from generate_data import write_fixture
from store import (
    ALREADY_MATCHED,
    DRAFT_INVOICE,
    EXACT_MATCH,
    FX_MATCH,
    INVOICE_ALREADY_PAID,
    NO_FX_RATE,
    OVERPAYMENT,
    PARTIAL_PAYMENT,
    REFUND_REQUIRES_HUMAN,
    Store,
    evaluate_booking,
)

_PASS = 0
_FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print("  PASS  " + label)
    else:
        _FAIL += 1
        suffix = "  (" + detail + ")" if detail else ""
        print("  FAIL  " + label + suffix)


def section(title: str) -> None:
    print("\n" + title)


def fresh_store() -> Store:
    """A Store over a throwaway temp state dir with a freshly written fixture."""
    tmp = Path(tempfile.mkdtemp(prefix="ledger-test-"))
    write_fixture(tmp)
    return Store(state_dir=tmp)


# ---------------------------------------------------------------------------
# Hazard: three exact EUR matches auto-book.
# ---------------------------------------------------------------------------


def test_exact_matches() -> None:
    section("Hazard: exact EUR match (x3) -> EXACT_MATCH, auto-book")
    s = fresh_store()
    for pay_id, inv_id in [
        ("PAY-3001", "INV-1001"),
        ("PAY-3002", "INV-1002"),
        ("PAY-3003", "INV-1003"),
    ]:
        out = s.book_payment(pay_id, inv_id)
        check(
            pay_id + " -> " + inv_id + " booked",
            out["booked"] and out["verdict"]["code"] == EXACT_MATCH,
            str(out["verdict"]),
        )
        check(pay_id + " invoice now paid", s.get_invoice(inv_id)["status"] == "paid")


# ---------------------------------------------------------------------------
# Hazard: USD payment reconciles only after a daily-rate conversion.
# ---------------------------------------------------------------------------


def test_fx_conversion() -> None:
    section("Hazard: USD payment -> FX_MATCH within 0.5 percent")
    s = fresh_store()
    out = s.book_payment("PAY-3004", "INV-1004")
    v = out["verdict"]
    check("PAY-3004 auto-books", out["booked"] and v["code"] == FX_MATCH, str(v))
    check("conversion written to verdict", v.get("converted_amount") == 172.40, str(v))
    check("rate captured", v.get("fx_rate") == 0.9234, str(v))
    # The audit note must carry the conversion for the human trail.
    fx_note = [e for e in s.get_audit_log() if e["decision"] == "BOOKED" and "FX" in e["reason"]]
    check("FX conversion in audit note", len(fx_note) == 1, str(fx_note))


# ---------------------------------------------------------------------------
# Hazard: partial payment is refused, not squeezed through.
# ---------------------------------------------------------------------------


def test_partial_payment() -> None:
    section("Hazard: partial payment (250 of 495 due) -> PARTIAL_PAYMENT")
    s = fresh_store()
    out = s.book_payment("PAY-3005", "INV-1005")
    check("PAY-3005 refused", not out["booked"], str(out["verdict"]))
    check("verdict is PARTIAL_PAYMENT", out["verdict"]["code"] == PARTIAL_PAYMENT)
    check("invoice stays open", s.get_invoice("INV-1005")["status"] == "open")
    check("queued for a human", len(s.list_pending_approvals()) == 1)


# ---------------------------------------------------------------------------
# Hazard: second payment against an already-paid invoice -> duplicate + refund.
# ---------------------------------------------------------------------------


def test_already_paid_and_refund() -> None:
    section("Hazard: already-paid invoice -> INVOICE_ALREADY_PAID, refund proposed")
    s = fresh_store()
    out = s.book_payment("PAY-3006", "INV-1006")
    check("PAY-3006 refused", not out["booked"], str(out["verdict"]))
    check("verdict is INVOICE_ALREADY_PAID", out["verdict"]["code"] == INVOICE_ALREADY_PAID)
    # The agent then proposes a refund. It must never be executed.
    refund = s.flag_refund("PAY-3006", "INV-1006", "Duplicate against paid invoice.")
    check("refund proposed, not executed", refund["proposal"]["executed"] is False)
    proposed = [e for e in s.get_audit_log() if e["decision"] == "REFUND_PROPOSED"]
    check("refund proposal audited", len(proposed) == 1)


# ---------------------------------------------------------------------------
# Hazard: order with no invoice yet -> draft created, then DRAFT_INVOICE.
# ---------------------------------------------------------------------------


def test_missing_invoice_drafts() -> None:
    section("Hazard: order with no invoice -> draft, then DRAFT_INVOICE on book")
    s = fresh_store()
    order = s.get_order("ORD-2007")
    drafted = s.create_draft_invoice("ORD-2007", order["total"])
    draft_id = drafted["draft"]["invoice_id"]
    check("draft has status draft", drafted["draft"]["status"] == "draft")
    check("draft is queued for a human", any(a.get("invoice_id") == draft_id for a in s.list_pending_approvals()))
    # A draft cannot be booked against.
    out = s.book_payment("PAY-3007", draft_id)
    check("cannot book against draft", not out["booked"], str(out["verdict"]))
    check("verdict is DRAFT_INVOICE", out["verdict"]["code"] == DRAFT_INVOICE)


# ---------------------------------------------------------------------------
# Hazard: unreadable reference -> escalation.
# ---------------------------------------------------------------------------


def test_unreadable_reference_escalates() -> None:
    section("Hazard: unreadable reference -> no match, escalate")
    s = fresh_store()
    candidate = s.find_matching_invoice("PAY-3008")
    check("no confident invoice match", candidate is None, str(candidate))
    out = s.escalate_payment("PAY-3008", "Unreadable reference, no confident match.")
    check("escalated", out["escalated"])
    escalations = [e for e in s.get_audit_log() if e["decision"] == "ESCALATED"]
    check("escalation audited", len(escalations) == 1)


# ---------------------------------------------------------------------------
# Hazard: re-booking an already-matched payment -> ALREADY_MATCHED (idempotency).
# ---------------------------------------------------------------------------


def test_idempotency() -> None:
    section("Hazard: re-book an already-matched payment -> ALREADY_MATCHED")
    s = fresh_store()
    first = s.book_payment("PAY-3001", "INV-1001")
    check("first booking succeeds", first["booked"])
    second = s.book_payment("PAY-3001", "INV-1001")
    check("second booking refused", not second["booked"], str(second["verdict"]))
    check("verdict is ALREADY_MATCHED", second["verdict"]["code"] == ALREADY_MATCHED)


# ---------------------------------------------------------------------------
# Latent branches: pinned directly against the pure gate so they are named,
# tested branches rather than behavior that happens to hold today.
# ---------------------------------------------------------------------------


def test_latent_branches() -> None:
    section("Latent branches: overpayment, no-FX-rate, negative amount")

    # Overpayment against an open EUR invoice.
    inv = {"invoice_id": "INV-9001", "order_id": "ORD-9001", "amount_due": 100.00, "currency": "EUR", "status": "open"}
    over = evaluate_booking(
        {"amount": 150.00, "currency": "EUR", "reference": "INV-9001", "status": "unmatched"}, inv
    )
    check("overpayment refused", not over.auto_book and over.code == OVERPAYMENT, over.code)

    # A currency with no configured daily rate cannot auto-book.
    no_rate = evaluate_booking(
        {"amount": 100.00, "currency": "JPY", "reference": "INV-9001", "status": "unmatched"}, inv
    )
    check("unconfigured FX currency refused", not no_rate.auto_book and no_rate.code == NO_FX_RATE, no_rate.code)

    # Any negative amount is a refund request, never a booking.
    negative = evaluate_booking(
        {"amount": -40.00, "currency": "EUR", "reference": "INV-9001", "status": "unmatched"}, inv
    )
    check("negative amount refused", not negative.auto_book and negative.code == REFUND_REQUIRES_HUMAN, negative.code)


# ---------------------------------------------------------------------------
# Full daily close: the exact flow the agent runs on camera. Asserts the batch
# splits four auto-booked and four to the human queue.
# ---------------------------------------------------------------------------


def test_full_daily_close() -> None:
    section("Full daily close: 8 payments -> 4 auto-booked, 4 to a human")
    s = fresh_store()

    plan = [
        ("PAY-3001", "book", "INV-1001"),
        ("PAY-3002", "book", "INV-1002"),
        ("PAY-3003", "book", "INV-1003"),
        ("PAY-3004", "book", "INV-1004"),
        ("PAY-3005", "book", "INV-1005"),
        ("PAY-3006", "book", "INV-1006"),
        ("PAY-3007", "draft", "ORD-2007"),
        ("PAY-3008", "escalate", None),
    ]

    booked = 0
    for pay_id, action, target in plan:
        if action == "book":
            out = s.book_payment(pay_id, target)
            if out["booked"]:
                booked += 1
            elif pay_id == "PAY-3006":
                s.flag_refund(pay_id, target, "Duplicate against paid invoice.")
        elif action == "draft":
            order = s.get_order(target)
            s.create_draft_invoice(target, order["total"])
        elif action == "escalate":
            s.escalate_payment(pay_id, "Unreadable reference, no confident match.")

    check("exactly 4 auto-booked", booked == 4, "booked=" + str(booked))
    pending = s.list_pending_approvals()
    check("at least 4 items in the human queue", len(pending) >= 4, "pending=" + str(len(pending)))
    audit = s.get_audit_log()
    check("audit log recorded every decision", len(audit) >= 8, "audit=" + str(len(audit)))
    booked_audit = [e for e in audit if e["decision"] == "BOOKED"]
    check("audit shows 4 bookings", len(booked_audit) == 4, str(len(booked_audit)))


def main() -> int:
    print("Ledger policy check (synthetic Meridian Supply Co. data)")
    test_exact_matches()
    test_fx_conversion()
    test_partial_payment()
    test_already_paid_and_refund()
    test_missing_invoice_drafts()
    test_unreadable_reference_escalates()
    test_idempotency()
    test_latent_branches()
    test_full_daily_close()

    print("\n" + "-" * 56)
    total = _PASS + _FAIL
    print("Result: " + str(_PASS) + "/" + str(total) + " checks passed.")
    if _FAIL:
        print("DRIFT DETECTED. A policy branch changed behavior.")
        return 1
    print("All policy branches hold. Safe to film.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
