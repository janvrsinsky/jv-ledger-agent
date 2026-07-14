"""Policy engine and state store for the Ledger accounting-ops agent.

This module holds the single booking choke point (`evaluate_booking`) plus an
append-only audit log and a human approval queue. The design point is the
boundary: the agent can only ever *call* the policy gate, never override its
verdict. Every write to internal state is recorded in the audit log.

All data is synthetic (Meridian Supply Co., a fictional outdoor-gear shop).
Nothing here touches a real bank, ledger, or accounting system. The worst the
agent can do is mislabel an internal JSON record, which the audit log captures
and a human reverses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Policy constants (deterministic, in code, not in a prompt)
# ---------------------------------------------------------------------------

# Ledger currency. Invoices are denominated here.
BASE_CURRENCY = "EUR"

# Exact-match tolerance for same-currency payments, in base-currency units.
# A payment books automatically only if it lands within this window.
EXACT_TOLERANCE = 0.01

# Foreign-currency payments convert at a fixed daily rate and auto-book only
# within this fraction of the amount due (0.5 percent).
FX_TOLERANCE_FRACTION = 0.005

# Fixed daily FX rates: units of BASE_CURRENCY per one unit of the given
# currency. In production these are pulled once per day and frozen for the
# close. A currency absent from this table cannot be auto-booked.
DAILY_FX_RATES: dict[str, float] = {
    "USD": 0.9234,
    "GBP": 1.1650,
}

# ---------------------------------------------------------------------------
# Verdict codes. Only EXACT_MATCH and FX_MATCH are auto-bookable; every other
# code routes the payment to the human approval queue with the reason attached.
# ---------------------------------------------------------------------------

EXACT_MATCH = "EXACT_MATCH"
FX_MATCH = "FX_MATCH"
PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
OVERPAYMENT = "OVERPAYMENT"
INVOICE_ALREADY_PAID = "INVOICE_ALREADY_PAID"
ALREADY_MATCHED = "ALREADY_MATCHED"
DRAFT_INVOICE = "DRAFT_INVOICE"
UNMATCHABLE_REFERENCE = "UNMATCHABLE_REFERENCE"
NO_FX_RATE = "NO_FX_RATE"
NO_INVOICE = "NO_INVOICE"
REFUND_REQUIRES_HUMAN = "REFUND_REQUIRES_HUMAN"

AUTO_BOOK_CODES = frozenset({EXACT_MATCH, FX_MATCH})


@dataclass(frozen=True)
class Verdict:
    """The single verdict returned by the policy gate for one payment.

    `auto_book` is derived from `code` so it can never drift from the code the
    tool reports back to the agent.
    """

    code: str
    reason: str
    amount_due: Optional[float] = None
    applied_amount: Optional[float] = None
    fx_rate: Optional[float] = None
    converted_amount: Optional[float] = None

    @property
    def auto_book(self) -> bool:
        return self.code in AUTO_BOOK_CODES

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "auto_book": self.auto_book,
            "reason": self.reason,
        }
        for name in ("amount_due", "applied_amount", "fx_rate", "converted_amount"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


# ---------------------------------------------------------------------------
# The policy gate. Pure function. One verdict per payment. This is the only
# path to a booking; `book_payment` below calls it and reports the verdict
# verbatim. A cleverly worded request cannot change the outcome, because the
# outcome is not the model's to make.
# ---------------------------------------------------------------------------


def _normalize_reference(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _reference_matches(payment: dict[str, Any], invoice: dict[str, Any]) -> bool:
    """A payment reference matches an invoice when it names the invoice id or
    the underlying order id. Punctuation and case are ignored so that a clean
    "INV-1002" and a noisy "inv 1002 //" both resolve, while a garbled
    reference that names neither does not.
    """
    ref = _normalize_reference(str(payment.get("reference", "")))
    if not ref:
        return False
    invoice_key = _normalize_reference(str(invoice.get("invoice_id", "")))
    order_key = _normalize_reference(str(invoice.get("order_id", "")))
    return (bool(invoice_key) and invoice_key in ref) or (
        bool(order_key) and order_key in ref
    )


def evaluate_booking(
    payment: dict[str, Any], invoice: Optional[dict[str, Any]]
) -> Verdict:
    """Decide whether `payment` may auto-book against `invoice`.

    Returns exactly one `Verdict`. Deterministic branches, no side effects.
    The branch order matters: hard refusals (negative amount, draft, already
    matched, already paid, bad reference) are checked before the amount and FX
    logic so that the reason reported is the most specific one.
    """
    amount = float(payment.get("amount", 0.0))
    currency = str(payment.get("currency", BASE_CURRENCY)).upper()

    # A negative amount is a refund request, never an auto-booking.
    if amount < 0:
        return Verdict(
            REFUND_REQUIRES_HUMAN,
            "Negative amount is a refund request and always needs a human.",
            applied_amount=amount,
        )

    # No invoice supplied: the agent should draft one instead of booking.
    if invoice is None:
        return Verdict(
            NO_INVOICE,
            "No invoice to book against. Draft one and hold for a human.",
        )

    status = str(invoice.get("status", "open")).lower()
    amount_due = float(invoice.get("amount_due", 0.0))

    # A draft invoice is not yet a real obligation; nothing books against it.
    if status == "draft":
        return Verdict(
            DRAFT_INVOICE,
            "Invoice is a draft and cannot be booked against.",
            amount_due=amount_due,
        )

    # Idempotency comes before the invoice-status check: a payment that already
    # matched must be refused as a re-book, not misread as a duplicate against
    # the invoice it settled a moment ago.
    if str(payment.get("status", "unmatched")).lower() == "matched":
        return Verdict(
            ALREADY_MATCHED,
            "Payment is already matched to an invoice. Refusing to re-book.",
            amount_due=amount_due,
        )

    # The invoice is already settled: this is a likely duplicate payment.
    if status == "paid":
        return Verdict(
            INVOICE_ALREADY_PAID,
            "Invoice is already paid. Likely duplicate. Propose a refund, do "
            "not book.",
            amount_due=amount_due,
        )

    # The reference must name the invoice or its order, or there is no
    # confident link to book on.
    if not _reference_matches(payment, invoice):
        return Verdict(
            UNMATCHABLE_REFERENCE,
            "Payment reference does not name this invoice or its order.",
            amount_due=amount_due,
        )

    invoice_currency = str(invoice.get("currency", BASE_CURRENCY)).upper()

    # Same-currency path: compare directly against the exact-match window.
    if currency == invoice_currency:
        if abs(amount - amount_due) <= EXACT_TOLERANCE:
            return Verdict(
                EXACT_MATCH,
                "Reference matches and amount is exact within tolerance.",
                amount_due=amount_due,
                applied_amount=amount,
            )
        if amount < amount_due:
            return Verdict(
                PARTIAL_PAYMENT,
                "Amount is below the amount due. Underpayment needs a human.",
                amount_due=amount_due,
                applied_amount=amount,
            )
        return Verdict(
            OVERPAYMENT,
            "Amount is above the amount due. Overpayment needs a human.",
            amount_due=amount_due,
            applied_amount=amount,
        )

    # Cross-currency path: convert at the fixed daily rate, then check the FX
    # window. A currency with no configured rate cannot auto-book.
    rate = DAILY_FX_RATES.get(currency)
    if rate is None:
        return Verdict(
            NO_FX_RATE,
            "No daily FX rate configured for " + currency + ". Needs a human.",
            amount_due=amount_due,
            applied_amount=amount,
        )

    converted = round(amount * rate, 2)
    fx_window = FX_TOLERANCE_FRACTION * amount_due
    if abs(converted - amount_due) <= fx_window:
        return Verdict(
            FX_MATCH,
            "Converts to "
            + f"{converted:.2f} {invoice_currency}"
            + " at daily rate, within the FX window.",
            amount_due=amount_due,
            applied_amount=amount,
            fx_rate=rate,
            converted_amount=converted,
        )
    if converted < amount_due:
        return Verdict(
            PARTIAL_PAYMENT,
            "Converted amount is below the amount due. Underpayment.",
            amount_due=amount_due,
            applied_amount=amount,
            fx_rate=rate,
            converted_amount=converted,
        )
    return Verdict(
        OVERPAYMENT,
        "Converted amount is above the amount due. Overpayment.",
        amount_due=amount_due,
        applied_amount=amount,
        fx_rate=rate,
        converted_amount=converted,
    )


# ---------------------------------------------------------------------------
# State store. JSON-backed, deliberately simple. Holds orders, invoices,
# payments, the approval queue, and the append-only audit log.
# ---------------------------------------------------------------------------

DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "state"

_ORDERS = "orders.json"
_INVOICES = "invoices.json"
_PAYMENTS = "payments.json"
_APPROVALS = "approvals.json"
_AUDIT = "audit_log.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StoreError(Exception):
    """Raised for lookups that fail or writes the store refuses."""


@dataclass
class Store:
    """Append-only-flavoured JSON store over the synthetic ledger.

    Bookings flip internal record state only after the policy gate passes.
    Drafts, refund proposals, and escalations never change money state: they
    only add an item to the approval queue and a row to the audit log.
    """

    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)

    # -- low-level IO -------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.state_dir / name

    def _read(self, name: str) -> Any:
        path = self._path(name)
        if not path.exists():
            raise StoreError(
                "State file missing: "
                + str(path)
                + ". Run generate_data.py first."
            )
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, name: str, data: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._path(name).open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    # -- audit + approval helpers ------------------------------------------

    def _append_audit(
        self,
        decision: str,
        reason: str,
        references: dict[str, Any],
        actor: str = "ledger-agent",
    ) -> dict[str, Any]:
        log = self._read(_AUDIT)
        entry = {
            "audit_id": "AUD-" + str(len(log) + 1).zfill(4),
            "timestamp": _utc_now(),
            "actor": actor,
            "decision": decision,
            "reason": reason,
            "references": references,
        }
        log.append(entry)
        self._write(_AUDIT, log)
        return entry

    def _queue_approval(
        self, payment_id: Optional[str], reason_code: str, explanation: str, extra: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        queue = self._read(_APPROVALS)
        item = {
            "approval_id": "APR-" + str(len(queue) + 1).zfill(4),
            "created": _utc_now(),
            "payment_id": payment_id,
            "reason_code": reason_code,
            "explanation": explanation,
        }
        if extra:
            item.update(extra)
        queue.append(item)
        self._write(_APPROVALS, queue)
        return item

    # -- read-only tools ----------------------------------------------------

    def list_unmatched_payments(self) -> list[dict[str, Any]]:
        payments = self._read(_PAYMENTS)
        return [p for p in payments if str(p.get("status", "unmatched")).lower() == "unmatched"]

    def get_order(self, order_id: str) -> dict[str, Any]:
        for order in self._read(_ORDERS):
            if order.get("order_id") == order_id:
                return order
        raise StoreError("Unknown order: " + str(order_id))

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        for invoice in self._read(_INVOICES):
            if invoice.get("invoice_id") == invoice_id:
                return invoice
        raise StoreError("Unknown invoice: " + str(invoice_id))

    def _get_payment(self, payment_id: str) -> dict[str, Any]:
        for payment in self._read(_PAYMENTS):
            if payment.get("payment_id") == payment_id:
                return payment
        raise StoreError("Unknown payment: " + str(payment_id))

    def find_matching_invoice(self, payment_id: str) -> Optional[dict[str, Any]]:
        """Best-effort lookup of the open invoice a payment names. Read-only:
        it proposes a candidate, it does not book. Returns None when the
        reference names no open invoice, which is itself a routing signal.
        """
        payment = self._get_payment(payment_id)
        for invoice in self._read(_INVOICES):
            if str(invoice.get("status", "open")).lower() == "draft":
                continue
            if _reference_matches(payment, invoice):
                return invoice
        return None

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        return self._read(_APPROVALS)

    def get_audit_log(self) -> list[dict[str, Any]]:
        return self._read(_AUDIT)

    # -- policy-gated write tools ------------------------------------------

    def book_payment(self, payment_id: str, invoice_id: str) -> dict[str, Any]:
        """Attempt to book a payment against an invoice.

        The verdict comes from `evaluate_booking`. Only EXACT_MATCH and
        FX_MATCH flip record state. Everything else appends a refusal to the
        audit log and drops the payment in the approval queue with its reason.
        """
        payment = self._get_payment(payment_id)
        invoice = self.get_invoice(invoice_id)
        verdict = evaluate_booking(payment, invoice)
        refs = {"payment_id": payment_id, "invoice_id": invoice_id}

        if not verdict.auto_book:
            self._append_audit("REFUSED_BOOKING", verdict.reason, {**refs, "code": verdict.code})
            approval = self._queue_approval(
                payment_id, verdict.code, verdict.reason,
                extra={"invoice_id": invoice_id},
            )
            return {
                "booked": False,
                "verdict": verdict.to_dict(),
                "approval": approval,
            }

        # Gate passed. Flip internal record state, then audit.
        payments = self._read(_PAYMENTS)
        invoices = self._read(_INVOICES)
        for p in payments:
            if p.get("payment_id") == payment_id:
                p["status"] = "matched"
                p["matched_invoice"] = invoice_id
        for inv in invoices:
            if inv.get("invoice_id") == invoice_id:
                inv["status"] = "paid"
                inv["settled_by"] = payment_id
        self._write(_PAYMENTS, payments)
        self._write(_INVOICES, invoices)

        note = verdict.reason
        if verdict.code == FX_MATCH:
            note = (
                "FX booking: "
                + f"{verdict.applied_amount:.2f} {payment.get('currency')}"
                + " at rate "
                + f"{verdict.fx_rate}"
                + " = "
                + f"{verdict.converted_amount:.2f} {invoice.get('currency')}"
                + f" (due {verdict.amount_due:.2f})."
            )
        audit = self._append_audit("BOOKED", note, {**refs, "code": verdict.code})
        return {"booked": True, "verdict": verdict.to_dict(), "audit": audit}

    def create_draft_invoice(
        self, order_id: str, amount: float, currency: str = BASE_CURRENCY
    ) -> dict[str, Any]:
        """Create a draft invoice for an order that has none yet. A draft is
        never sent and cannot be booked against; it holds for a human.
        """
        order = self.get_order(order_id)
        invoices = self._read(_INVOICES)
        draft_id = "INV-DRAFT-" + str(len(invoices) + 1).zfill(4)
        draft = {
            "invoice_id": draft_id,
            "order_id": order_id,
            "customer": order.get("customer"),
            "amount_due": round(float(amount), 2),
            "currency": currency.upper(),
            "status": "draft",
        }
        invoices.append(draft)
        self._write(_INVOICES, invoices)
        self._append_audit(
            "DRAFT_INVOICE_CREATED",
            "Drafted invoice for order without one. Not sent.",
            {"order_id": order_id, "invoice_id": draft_id},
        )
        approval = self._queue_approval(
            None, DRAFT_INVOICE, "Draft invoice awaiting human review and send.",
            extra={"invoice_id": draft_id, "order_id": order_id},
        )
        return {"draft": draft, "approval": approval}

    def flag_refund(
        self, payment_id: str, invoice_id: str, reason: str
    ) -> dict[str, Any]:
        """Propose a refund. Proposal only: `executed` is always false. No tool
        in this system can move money.
        """
        payment = self._get_payment(payment_id)
        proposal = {
            "type": "refund",
            "payment_id": payment_id,
            "invoice_id": invoice_id,
            "amount": payment.get("amount"),
            "currency": payment.get("currency"),
            "reason": reason,
            "executed": False,
        }
        self._append_audit(
            "REFUND_PROPOSED",
            "Refund proposed, not executed: " + reason,
            {"payment_id": payment_id, "invoice_id": invoice_id},
        )
        approval = self._queue_approval(
            payment_id, "REFUND_PROPOSED", reason, extra={"proposal": proposal}
        )
        return {"proposal": proposal, "approval": approval}

    def escalate_payment(self, payment_id: str, reason: str) -> dict[str, Any]:
        """Route a payment to a human with a reason. Used when no confident
        booking is possible (for example an unreadable reference).
        """
        self._get_payment(payment_id)
        self._append_audit(
            "ESCALATED", reason, {"payment_id": payment_id}
        )
        approval = self._queue_approval(payment_id, "ESCALATED", reason)
        return {"escalated": True, "approval": approval}
