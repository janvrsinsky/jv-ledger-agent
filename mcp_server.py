"""FastMCP server exposing the Ledger accounting-ops tools.

Ten typed tools over streamable-http: six read-only lookups and four
policy-gated writes. The agent drives the reconciliation, but every state
change flows through `store.book_payment`, which calls the deterministic policy
gate in `store.evaluate_booking`. No tool here sends a document, emails a
customer, or moves money:

    * create_draft_invoice -> a draft, never sent, cannot be booked against.
    * flag_refund          -> a proposal, executed = false, always.
    * escalate_payment     -> a queue item, nothing else.
    * book_payment         -> flips internal record state only after the gate
                              passes.

Run the server (host, PRIOR to using the agent):

    python mcp_server.py

The demo wires an MCP client (a rebranded LibreChat front end, disposable) to
this server. The durable engineering is the server, the gate, and the audit
trail. The agent runs over any MCP-capable client.
"""

from __future__ import annotations

from typing import Any

from store import Store, StoreError

try:
    from fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "fastmcp is required to run the MCP server. Install it with "
        "'pip install -r requirements.txt'. The policy engine and the test "
        "suite (test_flow.py) run without it."
    ) from exc

HOST = "127.0.0.1"
PORT = 8766

mcp = FastMCP("ledger")
store = Store()


def _safe(fn, **kwargs) -> dict[str, Any]:
    """Wrap store calls so a lookup miss becomes a typed error payload rather
    than an exception the client has to interpret.
    """
    try:
        return {"ok": True, "result": fn(**kwargs)}
    except StoreError as err:
        return {"ok": False, "error": str(err)}


# ---------------------------------------------------------------------------
# Read-only tools (6)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_unmatched_payments() -> dict[str, Any]:
    """List every incoming payment not yet matched to an invoice. This is the
    agent's work queue for the daily close.
    """
    return _safe(store.list_unmatched_payments)


@mcp.tool()
def get_order(order_id: str) -> dict[str, Any]:
    """Fetch a single order by id (for example 'ORD-2007')."""
    return _safe(store.get_order, order_id=order_id)


@mcp.tool()
def get_invoice(invoice_id: str) -> dict[str, Any]:
    """Fetch a single invoice by id (for example 'INV-1004'), including its
    amount due, currency, and status (open, paid, or draft).
    """
    return _safe(store.get_invoice, invoice_id=invoice_id)


@mcp.tool()
def find_matching_invoice(payment_id: str) -> dict[str, Any]:
    """Propose the open invoice a payment's reference names, or none. Read-only:
    it suggests a candidate for booking, it does not book. A null result is a
    routing signal (the reference names no open invoice).
    """
    return _safe(store.find_matching_invoice, payment_id=payment_id)


@mcp.tool()
def list_pending_approvals() -> dict[str, Any]:
    """List every item waiting on a human decision: refused bookings, refund
    proposals, draft invoices, and escalations.
    """
    return _safe(store.list_pending_approvals)


@mcp.tool()
def get_audit_log() -> dict[str, Any]:
    """Return the full append-only audit log: every booking, refusal, draft,
    refund proposal, and escalation with actor, decision, reason, and refs.
    """
    return _safe(store.get_audit_log)


# ---------------------------------------------------------------------------
# Policy-gated write tools (4)
# ---------------------------------------------------------------------------


@mcp.tool()
def book_payment(payment_id: str, invoice_id: str) -> dict[str, Any]:
    """Attempt to book a payment against an invoice.

    The verdict is decided by the in-code policy gate, not by this tool and not
    by the model. Only an exact same-currency match or an in-window FX match
    books automatically. Everything else is refused and queued for a human with
    the reason attached. The returned 'verdict' reports the gate's decision
    verbatim.
    """
    return _safe(store.book_payment, payment_id=payment_id, invoice_id=invoice_id)


@mcp.tool()
def create_draft_invoice(
    order_id: str, amount: float, currency: str = "EUR"
) -> dict[str, Any]:
    """Draft an invoice for an order that has none yet. The draft is never sent
    and cannot be booked against; it holds in the approval queue for a human.
    """
    return _safe(
        store.create_draft_invoice, order_id=order_id, amount=amount, currency=currency
    )


@mcp.tool()
def flag_refund(payment_id: str, invoice_id: str, reason: str) -> dict[str, Any]:
    """Propose a refund for a payment (for example a duplicate against an
    already-paid invoice). Proposal only: 'executed' is always false. No tool
    in this system can move money.
    """
    return _safe(
        store.flag_refund, payment_id=payment_id, invoice_id=invoice_id, reason=reason
    )


@mcp.tool()
def escalate_payment(payment_id: str, reason: str) -> dict[str, Any]:
    """Route a payment to a human with a plain-language reason. Used when no
    confident booking is possible, such as an unreadable reference.
    """
    return _safe(store.escalate_payment, payment_id=payment_id, reason=reason)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=HOST, port=PORT)
