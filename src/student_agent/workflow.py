from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

REQUIRED_TOOLS = {
    "get_order",
    "get_order_items",
    "get_shipment_summary",
    "get_payment_timeline",
    "get_refund_timeline",
    "get_customer_history",
    "get_policy",
}

# One EvidenceGateway instance is reused for every case in a run (see cli.py),
# so tool discovery only needs to happen once per gateway, not once per case.
_discovered_tools: dict[int, frozenset[str]] = {}


async def _discover_tools(gateway: EvidenceGateway) -> frozenset[str]:
    key = id(gateway)
    cached = _discovered_tools.get(key)
    if cached is not None:
        return cached
    tools = frozenset(await gateway.list_tools())
    missing = REQUIRED_TOOLS - tools
    if missing:
        raise RuntimeError(f"MCP gateway is missing required tools: {sorted(missing)}")
    _discovered_tools[key] = tools
    return tools


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _cap_idset(values: list[str], limit: int = 20) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _within_days(dt: datetime, anchor: datetime, before_days: int, after_days: int) -> bool:
    delta = (dt - anchor).total_seconds() / 86400
    return -before_days <= delta <= after_days


def _split_by_timeframe(
    rows: list[dict[str, Any]],
    date_key: str,
    anchor: datetime | None,
    *,
    before_days: int = 7,
    after_days: int = 180,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep rows whose date falls inside the order's lifecycle window.

    The MCP fixtures sometimes leak rows from a different order into the same
    response (same order_item_id but a shipping_limit_date months apart from
    the order's own purchase date). Dropping out-of-window rows is how we
    detect and reject that cross-order contamination instead of averaging or
    trusting whichever row happens to be listed first.
    """
    if anchor is None or not rows:
        return rows, []
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        dt = _parse_dt(row.get(date_key))
        if dt is None or _within_days(dt, anchor, before_days, after_days):
            kept.append(row)
        else:
            rejected.append(row)
    if not kept:
        # The window was too strict (or the anchor was wrong) - keep everything
        # rather than silently discarding the only evidence we have.
        return rows, []
    return kept, rejected


@dataclass
class CaseAgent:
    """Collects evidence refs per MCP domain and mirrors them into the trace."""

    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    all_refs: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    refs_by_domain: dict[str, list[str]] = field(default_factory=dict)

    async def fetch(self, tool_name: str, *, actor: str, **kwargs: str) -> dict[str, Any] | None:
        try:
            evidence = await self.gateway.call(tool_name, case_id=self.case_id, **kwargs)
        except RuntimeError:
            # The gateway raises when the domain has no data for this order
            # (e.g. no refund was ever filed) - that is a valid "no evidence"
            # outcome, not a case failure.
            return None
        ref = evidence["evidence_ref"]
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref],
        )
        if ref not in self.all_refs:
            self.all_refs.append(ref)
        self.refs_by_domain.setdefault(evidence.get("domain", "other"), []).append(ref)
        return evidence

    def refs_for(self, *domains: str, limit: int = 30) -> list[str]:
        """Evidence refs relevant to a specific claim topic, so claim_assessments
        cite the domains that actually back that verdict instead of the whole
        case's evidence pile."""
        selected = [ref for domain in domains for ref in self.refs_by_domain.get(domain, [])]
        return _cap_idset(selected or self.all_refs, limit=limit)

    def record_conflict(
        self, field_name: str, kept_label: str, rejected_labels: list[str], resolution_code: str
    ) -> None:
        sources = _cap_idset([kept_label, *rejected_labels], limit=5)
        if len(sources) < 2:
            return
        self.conflicts.append(
            {
                "field": field_name[:100],
                "sources": sources,
                "selected_source": kept_label[:80],
                "resolution_code": resolution_code[:80],
            }
        )


@dataclass
class EntityResolution:
    status: str
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    confidence: float
    primary_order_id: str | None
    primary_order_data: dict[str, Any] | None
    customer_unique_id: str | None
    related_order_ids: list[str]


async def _resolve_entity(case: dict[str, Any], agent: CaseAgent) -> EntityResolution:
    case_id = case["case_id"]
    agent.trace.emit(case_id=case_id, event_type="task_assigned", actor="entity-agent", target="entity-agent")

    request = case.get("customer_request", {})
    claimed = request.get("claimed_order_id")
    raw_candidates = [claimed, *case.get("candidate_order_ids", [])]
    candidates = [c for i, c in enumerate(raw_candidates) if c and c not in raw_candidates[:i]]

    resolved: list[tuple[str, dict[str, Any]]] = []
    rejected: list[str] = []
    for order_id in candidates:
        evidence = await agent.fetch("get_order", actor="entity-agent", order_id=order_id)
        if evidence is None:
            rejected.append(order_id)
        else:
            resolved.append((order_id, evidence["data"]))

    customer_hint = case.get("customer_unique_id_hint")
    history_order_ids: set[str] = set()
    customer_confirmed = False
    if customer_hint:
        history = await agent.fetch(
            "get_customer_history", actor="entity-agent", customer_unique_id=customer_hint
        )
        if history is not None:
            history_order_ids = {row["order_id"] for row in history["data"].get("orders", []) if row.get("order_id")}
            customer_confirmed = True

    confirmed = [pair for pair in resolved if pair[0] in history_order_ids] if history_order_ids else []
    if confirmed:
        primary_set = confirmed
        # Orders that exist but don't belong to this customer are a real
        # resolution rejection, not just a missing record.
        rejected.extend(oid for oid, _ in resolved if oid not in history_order_ids)
    else:
        primary_set = resolved

    if not primary_set:
        status = "not_found"
        confidence = 0.0
        primary_order_id, primary_order_data = None, None
    elif len(primary_set) == 1:
        status = "resolved"
        confidence = 0.95 if customer_confirmed else 0.7
        primary_order_id, primary_order_data = primary_set[0]
    else:
        status = "ambiguous"
        confidence = 0.35
        primary_order_id, primary_order_data = primary_set[0]

    resolved_order_ids = _cap_idset([oid for oid, _ in primary_set])
    related_order_ids = _cap_idset([oid for oid in history_order_ids if oid != primary_order_id])

    return EntityResolution(
        status=status,
        resolved_order_ids=resolved_order_ids,
        rejected_candidates=_cap_idset(rejected),
        confidence=confidence,
        primary_order_id=primary_order_id,
        primary_order_data=primary_order_data,
        customer_unique_id=customer_hint if customer_confirmed else None,
        related_order_ids=related_order_ids,
    )


@dataclass
class ShipmentAssessment:
    verdict: str
    late_seller_ids: list[str]
    timeline_complete: bool
    item_ids: list[str]
    seller_ids: list[str]
    order_item_total_brl: float


async def _assess_shipment(
    order_id: str, order_data: dict[str, Any], agent: CaseAgent
) -> ShipmentAssessment:
    agent.trace.emit(case_id=agent.case_id, event_type="handoff", actor="coordinator", target="shipment-agent")
    purchase = _parse_dt(order_data.get("order_purchase_timestamp"))

    items_evidence = await agent.fetch("get_order_items", actor="shipment-agent", order_id=order_id)
    raw_items = items_evidence["data"] if items_evidence else []
    items, rejected_items = _split_by_timeframe(raw_items, "shipping_limit_date", purchase)
    if rejected_items:
        for row in rejected_items:
            agent.record_conflict(
                "order_items.shipping_limit_date",
                f"kept@{items[0].get('shipping_limit_date') if items else 'n/a'}",
                [f"rejected@{row.get('shipping_limit_date')}"],
                "dropped_row_outside_order_purchase_window",
            )

    item_ids = _cap_idset([row.get("order_item_id", "") for row in items])
    seller_ids = _cap_idset([row.get("seller_id", "") for row in items])
    order_item_total = sum(_to_float(row.get("price")) + _to_float(row.get("freight_value")) for row in items)

    shipment_evidence = await agent.fetch("get_shipment_summary", actor="shipment-agent", order_id=order_id)
    shipment_data = shipment_evidence["data"] if shipment_evidence else {}

    delivered = _parse_dt(shipment_data.get("delivered_customer_at") or order_data.get("order_delivered_customer_date"))
    delivered_carrier = _parse_dt(
        shipment_data.get("delivered_carrier_at") or order_data.get("order_delivered_carrier_date")
    )
    estimated = _parse_dt(shipment_data.get("estimated_delivery_at") or order_data.get("order_estimated_delivery_date"))

    raw_limits = shipment_data.get("shipping_limits", [])
    limits, rejected_limits = _split_by_timeframe(raw_limits, "shipping_limit_at", purchase)
    if rejected_limits:
        agent.record_conflict(
            "shipment_summary.shipping_limits",
            "kept_within_order_window",
            ["rejected_out_of_window"] * len(rejected_limits),
            "dropped_shipping_limit_outside_order_purchase_window",
        )

    late_seller_ids: list[str] = []
    if delivered_carrier is not None:
        for limit in limits:
            limit_at = _parse_dt(limit.get("shipping_limit_at"))
            seller_id = limit.get("seller_id")
            if limit_at and seller_id and delivered_carrier > limit_at:
                late_seller_ids.append(seller_id)
    late_seller_ids = _cap_idset(late_seller_ids)

    raw_events = shipment_data.get("events", [])
    events, rejected_events = _split_by_timeframe(raw_events, "event_at", purchase)
    if rejected_events:
        agent.record_conflict(
            "shipment_summary.events",
            "kept_within_order_window",
            ["rejected_out_of_window"] * len(rejected_events),
            "dropped_shipment_event_outside_order_purchase_window",
        )
    logistics_flagged = any(
        event.get("actor") == "logistics_provider" and event.get("event_type") in {"delivered_late", "lost"}
        for event in events
    )
    lost = any(event.get("event_type") == "lost" for event in events)
    returned = any(event.get("event_type") == "returned" for event in events)

    overall_late = bool(delivered and estimated and delivered > estimated)

    if lost:
        verdict = "lost"
    elif returned:
        verdict = "returned"
    elif late_seller_ids:
        verdict = "seller_delay"
    elif overall_late or logistics_flagged:
        verdict = "logistics_delay"
    elif delivered is not None:
        verdict = "on_time"
    else:
        verdict = "insufficient_evidence"

    timeline_complete = bool(purchase and delivered_carrier and delivered and estimated)

    return ShipmentAssessment(
        verdict=verdict,
        late_seller_ids=late_seller_ids,
        timeline_complete=timeline_complete,
        item_ids=item_ids,
        seller_ids=seller_ids,
        order_item_total_brl=round(order_item_total, 2),
    )


def _reconcile_captures(
    raw_payments: list[dict[str, Any]], captured_events: list[dict[str, Any]]
) -> tuple[float, bool, list[dict[str, Any]]]:
    """Reconcile payment rows against their captured events.

    The same payment_sequential slot can appear more than once in the raw
    rows for two different reasons that must be told apart:
      - same amount repeated -> a genuine duplicate charge: both captures
        really took money, so they're summed.
      - different amount -> a retried/corrected capture (e.g. an earlier
        attempt flagged by a "reconciliation_mismatch" event, then captured
        again later for the corrected amount): only the most recent capture
        is real money: the earlier one was superseded, not additional.
    Rows carry no timestamp of their own, so "most recent" is resolved via
    the event_at of the matching captured event (matched by amount, since
    events don't carry payment_sequential either).
    """
    amounts = [round(_to_float(event.get("amount_brl")), 2) for event in captured_events]
    if not amounts:
        total = sum(_to_float(row.get("payment_value")) for row in raw_payments)
        return round(total, 2), False, raw_payments

    kept_amount_set = set(amounts)
    kept_payments = [
        row for row in raw_payments if round(_to_float(row.get("payment_value")), 2) in kept_amount_set
    ]

    latest_event_at: dict[float, str] = {}
    for event in captured_events:
        amount = round(_to_float(event.get("amount_brl")), 2)
        event_at = event.get("event_at") or ""
        if amount not in latest_event_at or event_at > latest_event_at[amount]:
            latest_event_at[amount] = event_at

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in kept_payments:
        sequential = row.get("payment_sequential")
        if sequential is not None:
            groups.setdefault(sequential, []).append(row)

    total = 0.0
    duplicate = False
    for rows in groups.values():
        row_amounts = {round(_to_float(row.get("payment_value")), 2) for row in rows}
        if len(rows) > 1 and len(row_amounts) == 1:
            duplicate = True
            total += sum(_to_float(row.get("payment_value")) for row in rows)
        else:
            latest_row = max(
                rows,
                key=lambda row: latest_event_at.get(round(_to_float(row.get("payment_value")), 2), ""),
            )
            total += _to_float(latest_row.get("payment_value"))

    return round(total, 2), duplicate, kept_payments


@dataclass
class PaymentAssessment:
    verdict: str
    captured_total_brl: float
    refunded_total_brl: float
    refundable_total_brl: float
    refund_status: str | None
    payment_references: list[str]


async def _assess_payment(
    order_id: str, order_data: dict[str, Any], order_item_total_brl: float, agent: CaseAgent
) -> PaymentAssessment:
    agent.trace.emit(case_id=agent.case_id, event_type="handoff", actor="coordinator", target="payment-agent")
    purchase = _parse_dt(order_data.get("order_purchase_timestamp"))

    timeline_evidence = await agent.fetch("get_payment_timeline", actor="payment-agent", order_id=order_id)
    timeline_data = timeline_evidence["data"] if timeline_evidence else {}

    raw_payments = timeline_data.get("payments", [])
    raw_events = timeline_data.get("events", [])
    events, rejected_events = _split_by_timeframe(raw_events, "event_at", purchase)
    if rejected_events:
        agent.record_conflict(
            "payment_timeline.events",
            "kept_within_order_window",
            ["rejected_out_of_window"] * len(rejected_events),
            "dropped_payment_event_outside_order_purchase_window",
        )

    captured_events = [event for event in events if event.get("event_type") == "captured"]
    captured_total, duplicate_capture, kept_payments = _reconcile_captures(raw_payments, captured_events)

    refund_evidence = await agent.fetch("get_refund_timeline", actor="payment-agent", order_id=order_id)
    refunded_total = 0.0
    refund_status: str | None = None
    if refund_evidence is not None:
        refund_data = refund_evidence["data"]
        refund_events = refund_data.get("events", [])
        refund_events, rejected_refund_events = _split_by_timeframe(refund_events, "event_at", purchase)
        if rejected_refund_events:
            agent.record_conflict(
                "refund_timeline.events",
                "kept_within_order_window",
                ["rejected_out_of_window"] * len(rejected_refund_events),
                "dropped_refund_event_outside_order_purchase_window",
            )
        completed = [e for e in refund_events if e.get("event_type") in {"completed", "refunded"}]
        failed = [e for e in refund_events if e.get("event_type") == "failed"]
        pending = [e for e in refund_events if e.get("event_type") in {"pending", "requested", "approved"}]
        refunded_total = sum(_to_float(e.get("amount_brl")) for e in completed)
        if completed:
            refund_status = "completed"
        elif failed:
            refund_status = "failed"
        elif pending:
            refund_status = "pending"

    # Compare against the order's own item+freight total (already time-window
    # filtered in the shipment agent), not the raw payment rows - those can
    # still carry a contaminated row from a different order with no way to
    # date-filter them (payment rows have no timestamp of their own).
    mismatch = (
        order_item_total_brl > 0
        and abs(round(captured_total - order_item_total_brl, 2)) > 0.01
        and not duplicate_capture
    )

    if duplicate_capture:
        verdict = "duplicate_capture"
    elif refund_status == "failed":
        verdict = "refund_failed"
    elif refund_status == "pending":
        verdict = "refund_pending"
    elif refund_status == "completed":
        verdict = "refunded"
    elif mismatch:
        verdict = "capture_mismatch"
    elif captured_events or raw_payments:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"

    refundable_total = max(round(captured_total - refunded_total, 2), 0.0)
    payment_references = _cap_idset(
        [f"{order_id}::payment::{row.get('payment_sequential', i)}" for i, row in enumerate(kept_payments)]
    )

    return PaymentAssessment(
        verdict=verdict,
        captured_total_brl=round(captured_total, 2),
        refunded_total_brl=round(refunded_total, 2),
        refundable_total_brl=refundable_total,
        refund_status=refund_status,
        payment_references=payment_references,
    )


PRIMARY_ISSUE_FALLBACK = {
    "case_status": "needs_investigation",
    "recommended_action": "manual_review_required",
    "refund_brl": 0.0,
    "responsible_parties": [{"party_type": "unknown", "party_id": None}],
}


def _classify_primary_issue(
    order_data: dict[str, Any], shipment: ShipmentAssessment, payment: PaymentAssessment
) -> tuple[str, list[str]]:
    order_status = order_data.get("order_status")
    is_paid = payment.captured_total_brl > 0
    secondary: list[str] = []

    if order_status == "canceled" and is_paid:
        primary = "canceled_order_paid"
    elif order_status == "unavailable" and is_paid:
        primary = "unavailable_order_paid"
    elif payment.verdict == "duplicate_capture":
        primary = "duplicate_charge"
    elif payment.verdict == "capture_mismatch":
        primary = "payment_mismatch"
    elif payment.verdict == "refund_failed":
        primary = "refund_failed"
    elif payment.verdict == "refund_pending":
        primary = "refund_pending"
    elif shipment.verdict == "seller_delay":
        primary = "late_delivery_seller"
    elif shipment.verdict == "logistics_delay":
        primary = "late_delivery_logistics"
    elif payment.verdict == "reconciled" and shipment.verdict == "on_time":
        primary = "valid_split_payment" if len(payment.payment_references) > 1 else "insufficient_evidence"
    else:
        primary = "insufficient_evidence"

    if shipment.verdict in {"seller_delay", "logistics_delay"} and primary not in {
        "late_delivery_seller",
        "late_delivery_logistics",
    }:
        secondary.append(f"late_delivery_{'seller' if shipment.verdict == 'seller_delay' else 'logistics'}")
    if payment.verdict in {"capture_mismatch", "refund_pending", "refund_failed"} and primary not in {
        "payment_mismatch",
        "refund_pending",
        "refund_failed",
    }:
        secondary.append(payment.verdict if payment.verdict != "capture_mismatch" else "payment_mismatch")

    return primary, secondary[:5]


CLAIM_TOPIC_DOMAINS: dict[str, tuple[str, ...]] = {
    "late_delivery_seller": ("shipment", "item"),
    "late_delivery_logistics": ("shipment", "item"),
    "refund_pending": ("refund", "payment"),
    "refund_failed": ("refund", "payment"),
    "requested_full_refund": ("payment", "refund", "policy"),
    "canceled_order_paid": ("order", "payment"),
    "unavailable_order_paid": ("order", "payment"),
    "duplicate_charge": ("payment",),
    "payment_mismatch": ("payment",),
    "valid_split_payment": ("payment",),
}


def _assess_claims(
    claims: list[dict[str, Any]],
    primary_issue: str,
    secondary_issues: list[str],
    shipment: ShipmentAssessment,
    payment: PaymentAssessment,
    refund_brl: float,
    agent: CaseAgent,
) -> list[dict[str, Any]]:
    supported_topics = {primary_issue, *secondary_issues}
    out: list[dict[str, Any]] = []
    for claim in claims[:5]:
        topic = claim.get("topic", "")
        claim_id = claim.get("claim_id", "")
        if not claim_id:
            continue
        if topic == "requested_full_refund":
            # Authoritative: does the policy-driven decision actually grant a
            # refund, rather than guessing from the payment/shipment verdicts
            # alone - this keeps the claim verdict consistent with
            # financial_resolution instead of a second, looser heuristic.
            if refund_brl > 0:
                verdict = "supported"
            elif payment.verdict == "insufficient_evidence" and shipment.verdict == "insufficient_evidence":
                verdict = "insufficient_evidence"
            else:
                verdict = "unsupported"
        elif topic in supported_topics:
            verdict = "supported"
        elif primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        else:
            verdict = "unsupported"
        confidence = 0.7 if verdict == "supported" else 0.4 if verdict == "insufficient_evidence" else 0.55
        domains = CLAIM_TOPIC_DOMAINS.get(topic, ("order",))
        out.append(
            {
                "claim_id": claim_id[:64],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": agent.refs_for(*domains),
            }
        )
    return out


def _not_found_output(case: dict[str, Any], resolution: EntityResolution, evidence_refs: list[str]) -> dict[str, Any]:
    refs = _cap_idset(evidence_refs, limit=30)
    claims = case.get("customer_request", {}).get("claims", [])
    claim_assessments = [
        {
            "claim_id": claim.get("claim_id", "")[:64],
            "verdict": "insufficient_evidence",
            "confidence": 0.0,
            "evidence_refs": refs,
        }
        for claim in claims[:5]
        if claim.get("claim_id")
    ]
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": resolution.status,
            "resolved_order_ids": resolution.resolved_order_ids,
            "rejected_candidates": resolution.rejected_candidates,
            "confidence": resolution.confidence,
        },
        "customer_context": {
            "customer_unique_id": resolution.customer_unique_id,
            "related_order_ids": resolution.related_order_ids,
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []},
        "resolution_actions": ["escalate_for_manual_entity_resolution"],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    await _discover_tools(gateway)
    agent = CaseAgent(case_id=case_id, gateway=gateway, trace=trace)

    resolution = await _resolve_entity(case, agent)
    if resolution.primary_order_id is None or resolution.primary_order_data is None:
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code=resolution.status,
        )
        return _not_found_output(case, resolution, agent.all_refs)

    order_id = resolution.primary_order_id
    order_data = resolution.primary_order_data

    shipment = await _assess_shipment(order_id, order_data, agent)
    payment = await _assess_payment(order_id, order_data, shipment.order_item_total_brl, agent)

    primary_issue, secondary_issues = _classify_primary_issue(order_data, shipment, payment)

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="policy-agent")
    policy_version = case.get("policy_version", "")
    policy_evidence = await agent.fetch("get_policy", actor="policy-agent", policy_version=policy_version)
    policy_rules = policy_evidence["data"].get("rules", {}) if policy_evidence else {}
    rule = policy_rules.get(primary_issue, PRIMARY_ISSUE_FALLBACK)

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
        attributes={"case_status": rule["case_status"], "refund_brl": rule["refund_brl"]},
    )

    # Base confidence is deliberately modest: this case-set is built around
    # source conflicts and claim/evidence mismatches, so even a "clean"
    # classification rests on heuristics (time-window filtering, sequential
    # matching) that are frequently wrong on edge cases - a high headline
    # confidence would overstate how sure the rule-based classifier is.
    conflict_penalty = min(len(agent.conflicts) * 0.15, 0.5)
    base_confidence = 0.65 if primary_issue not in {"insufficient_evidence", "unsupported_claim"} else 0.3
    confidence = round(max(base_confidence - conflict_penalty, 0.15), 2)

    refund_brl = round(_to_float(rule.get("refund_brl", 0.0)), 2)

    claims = case.get("customer_request", {}).get("claims", [])
    claim_assessments = _assess_claims(
        claims, primary_issue, secondary_issues, shipment, payment, refund_brl, agent
    )

    # get_policy is keyed by policy_version, not by this case's own seller -
    # its example party_id (when present) is a policy-document illustration,
    # not necessarily an entity that exists in this case. Only trust it for
    # party types that have no case-specific evidence; for "seller", prefer
    # the seller_id this case's own shipment evidence actually implicated
    # (shipment.late_seller_ids), so root_cause never names a seller absent
    # from affected_entities.seller_ids.
    responsible_parties = [
        {
            "party_type": party["party_type"],
            "party_id": (
                shipment.late_seller_ids[0]
                if party["party_type"] == "seller" and shipment.late_seller_ids
                else party.get("party_id") if party["party_type"] != "seller" else None
            ),
        }
        for party in rule.get("responsible_parties", [])[:5]
    ] or [{"party_type": "unknown", "party_id": None}]

    ranked_causes = [{"cause_code": primary_issue.upper(), "rank": 1}]
    for index, issue in enumerate(secondary_issues[:4], start=2):
        ranked_causes.append({"cause_code": issue.upper(), "rank": index})

    # Prefer the policy's own responsible party as the refund's entity_id
    # (e.g. the specific seller_id at fault) so financial_resolution stays
    # consistent with root_cause_analysis instead of always pointing at the
    # order itself.
    refund_entity_id = next(
        (party["party_id"] for party in responsible_parties if party.get("party_id")), order_id
    )
    refund_lines = (
        [{"reason_code": primary_issue, "amount_brl": refund_brl, "entity_id": refund_entity_id}]
        if refund_brl > 0
        else []
    )
    resolution_actions = _cap_idset(
        [rule.get("recommended_action", "manual_review_required"), "notify_customer_of_decision"], limit=8
    )

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues,
            "case_status": rule["case_status"],
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": resolution.resolved_order_ids,
            "item_ids": shipment.item_ids,
            "seller_ids": shipment.seller_ids,
            "payment_references": payment.payment_references,
            "shipment_ids": [f"{order_id}::shipment"],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": resolution.status,
            "resolved_order_ids": resolution.resolved_order_ids,
            "rejected_candidates": resolution.rejected_candidates,
            "confidence": resolution.confidence,
        },
        "customer_context": {
            "customer_unique_id": resolution.customer_unique_id,
            "related_order_ids": resolution.related_order_ids,
        },
        "shipment_analysis": {
            "verdict": shipment.verdict,
            "late_seller_ids": shipment.late_seller_ids,
            "timeline_complete": shipment.timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment.verdict,
            "captured_total_brl": payment.captured_total_brl,
            "refunded_total_brl": payment.refunded_total_brl,
            "refundable_total_brl": payment.refundable_total_brl,
        },
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": _cap_idset(agent.all_refs, limit=30),
        "data_conflicts": agent.conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_brl,
            "refund_lines": refund_lines,
        },
        "resolution_actions": resolution_actions,
    }

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=rule["case_status"],
        attributes={"conflicts_detected": len(agent.conflicts)},
    )
    return output
