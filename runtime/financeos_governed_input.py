"""FinanceOS V5 governed-data input for canonical Genesis finance specialists.

This is an INPUT envelope, not an output contract and not a write path.
Absent fields stay absent. Callers and models must not invent them.
Source-authority labels are preserved; they are never silently rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from canonical_result_contract import FINANCE_AGENT_IDS

GOVERNED_INPUT_VERSION = "financeos-governed-input-v1"

CERTIFICATION_STATES = frozenset({
    "CERTIFIED",
    "HOLD",
    "STALE",
    "UNMAPPED",
    "CONFLICTED",
    "ABSENT",
})
BLOCKING_CERTIFICATION_STATES = frozenset({"HOLD", "STALE", "UNMAPPED", "CONFLICTED"})
REVENUE_AUTHORITIES = frozenset({"PORTAL", "XERO", "FINANCEOS", "ABSENT"})
COMMISSION_MEASURES = (
    "expected_commission",
    "tracker_commission",
    "booked_commission",
    "paid_commission",
)
OPTIONAL_CONTEXT_FIELDS = (
    "entity",
    "period",
    "as_of",
    "data_product_id",
    "data_product_version",
    "semantic_definition_id",
    "semantic_definition_version",
    "mapping_rule_version",
    "certification_status",
    "reconciliation_status",
    "freshness",
    "dimensions",
    "lineage",
    "source_refs",
    "policy_versions",
    "prior_exceptions",
    "approval_context",
    "revenue_operational_source",
    "revenue_ledger_source",
    "portal_certified_revenue_present",
    "xero_revenue_asserted_primary",
    "commission_measures",
    "intercompany_classification",
    "adopted_instrument_present",
    # Populated only by the live-fetch path (fetch_live_financeos_context).
    # Absent entirely for the pre-existing local/test envelope path -- a
    # caller handing in financeos_context directly never needs to state this.
    "live_fetch_state",
)

# Distinct outcomes a live call against FinanceOS's governed read layer can
# produce. "reachable_empty" and "unreachable"/"unauthorized"/"invalid_request"
# must never be collapsed into one generic failure -- a live, empty certified
# layer is a true and complete answer ("nothing certified yet"), not an error;
# an unreachable/unauthorized/malformed-request endpoint is a real error and
# must never be silently treated as "no data" (which would look identical to
# "not asked").
#
# "invalid_request" (added 2026-08-29, found live against exact-SHA e137e26
# certification on the deployed Render service): FinanceOS's
# GET /api/v1/data-products/revenue requires entity_id to be the entity's
# UUID, not its display name (see FinanceOS's src/api/routes/data-products.ts
# RevenueListQuery zod schema). A caller supplying a human-readable entity
# name (the same convention the pre-existing local/test financeos_context
# envelope's own "entity" field uses) gets FinanceOS's own 400
# "Invalid query parameters" -- a caller-side mistake, not evidence the
# service is down. Before this fix that 400 fell through to the generic
# "unreachable" bucket below, which would misleadingly tell a Genesis
# specialist FinanceOS itself is unreachable when the real problem is a
# malformed entity_id. Live-verified this session: entity_id="NES Health LLC"
# -> FinanceOS 400 -> now "invalid_request"; entity_id=<real entity UUID> ->
# FinanceOS 200 {revenue: [], count: 0} -> "reachable_empty" (correct, matches
# the documented empty-revenue-data-products state).
LIVE_FETCH_STATES = frozenset({
    "not_attempted",
    "not_configured",
    "reachable_found",
    "reachable_empty",
    "unreachable",
    "unauthorized",
    "invalid_request",
})
LIVE_FETCH_FAILURE_STATES = frozenset({
    "not_configured", "unreachable", "unauthorized", "invalid_request",
})

PROHIBITED_FINANCE_ACTIONS = frozenset({
    "activate_mapping",
    "certify_data",
    "redefine_adopted_metric",
    "write_financeos_state",
    "write_xero",
    "treat_excel_as_actuals_authority",
})


class FinanceosGovernedInputError(ValueError):
    """Caller supplied an unusable governed-context representation."""


@dataclass(frozen=True)
class FinanceosGovernedBind:
    ok: bool
    reason: str | None
    slug: str
    present_fields: tuple[str, ...] = ()
    absent_fields: tuple[str, ...] = OPTIONAL_CONTEXT_FIELDS
    certification_status: str = "ABSENT"
    source_authority_flags: tuple[str, ...] = ()
    prompt_block: str = ""
    context: dict[str, Any] = field(default_factory=dict)


def extract_raw_context(body: Any) -> Any:
    """Return the first explicit envelope. Do not merge or invent."""
    if body is None:
        return None
    direct = getattr(body, "financeos_context", None)
    if direct not in (None, "", {}, []):
        return direct
    for attr in ("input", "task"):
        value = getattr(body, attr, None)
        if isinstance(value, Mapping) and value.get("financeos_context") not in (None, "", {}, []):
            return value.get("financeos_context")
    return None


def bind_financeos_governed_context(slug: str, raw: Any) -> FinanceosGovernedBind:
    if slug not in FINANCE_AGENT_IDS:
        return FinanceosGovernedBind(
            True, None, slug, prompt_block="",
            absent_fields=OPTIONAL_CONTEXT_FIELDS,
        )
    if raw is None:
        return FinanceosGovernedBind(
            True,
            None,
            slug,
            absent_fields=OPTIONAL_CONTEXT_FIELDS,
            certification_status="ABSENT",
            prompt_block="",
        )
    if not isinstance(raw, Mapping):
        return FinanceosGovernedBind(
            False,
            "financeos_governed_context_invalid",
            slug,
            absent_fields=OPTIONAL_CONTEXT_FIELDS,
        )
    unknown = tuple(sorted(str(key) for key in raw if key not in OPTIONAL_CONTEXT_FIELDS))
    if unknown:
        return FinanceosGovernedBind(
            False,
            "financeos_governed_context_unknown_field",
            slug,
            absent_fields=OPTIONAL_CONTEXT_FIELDS,
            source_authority_flags=unknown,
        )

    present: dict[str, Any] = {}
    absent: list[str] = []
    for key in OPTIONAL_CONTEXT_FIELDS:
        if key not in raw or raw[key] in (None, "", [], {}):
            absent.append(key)
            continue
        present[key] = raw[key]

    certification = str(present.get("certification_status", "ABSENT"))
    if "certification_status" in present and certification not in CERTIFICATION_STATES:
        return FinanceosGovernedBind(
            False, "financeos_certification_status_invalid", slug,
            present_fields=tuple(present),
            absent_fields=tuple(absent),
        )

    if "live_fetch_state" in present and str(present["live_fetch_state"]) not in LIVE_FETCH_STATES:
        return FinanceosGovernedBind(
            False, "financeos_live_fetch_state_invalid", slug,
            present_fields=tuple(present),
            absent_fields=tuple(absent),
        )

    flags = _source_authority_flags(present)
    return FinanceosGovernedBind(
        True,
        None,
        slug,
        present_fields=tuple(present),
        absent_fields=tuple(absent),
        certification_status=certification if "certification_status" in present else "ABSENT",
        source_authority_flags=flags,
        prompt_block=_prompt_block(slug, present, tuple(absent), certification if "certification_status" in present else "ABSENT", flags),
        context=present,
    )


def _source_authority_flags(present: Mapping[str, Any]) -> tuple[str, ...]:
    flags: list[str] = []
    portal_certified = bool(present.get("portal_certified_revenue_present"))
    if portal_certified:
        flags.append("portal_operational_revenue_truth")
        flags.append("xero_not_primary_operational_revenue")
    if present.get("xero_revenue_asserted_primary") and portal_certified:
        flags.append("conflicted_xero_revenue_primary_rejected")
    measures = present.get("commission_measures")
    if isinstance(measures, Mapping):
        for name in COMMISSION_MEASURES:
            if name in measures:
                flags.append(f"commission_measure_preserved:{name}")
        if set(measures) == {"booked_commission"}:
            flags.append("xero_commission_gl_not_sole_truth")
    classification = str(present.get("intercompany_classification") or "")
    instrument = present.get("adopted_instrument_present")
    if classification.lower() == "royalty" and instrument is not True:
        flags.append("unresolved_intercompany_charge_hold")
    status = str(present.get("certification_status") or "ABSENT")
    if status in BLOCKING_CERTIFICATION_STATES:
        flags.append(f"blocking_certification:{status}")
        flags.append("not_certified_actuals")
    live_state = present.get("live_fetch_state")
    if live_state is not None:
        live_state = str(live_state)
        if live_state == "reachable_empty":
            flags.append("live_certified_layer_reachable_no_rows")
            flags.append("empty_is_not_an_error_do_not_invent_a_figure")
        elif live_state in LIVE_FETCH_FAILURE_STATES:
            flags.append(f"live_certified_layer_unreachable:{live_state}")
            flags.append("do_not_treat_unreachable_as_zero_or_absent_truth")
        elif live_state == "reachable_found":
            flags.append("live_certified_layer_reachable_with_data")
    return tuple(flags)


def _prompt_block(
    slug: str,
    present: Mapping[str, Any],
    absent: tuple[str, ...],
    certification: str,
    flags: tuple[str, ...],
) -> str:
    lines = [
        "FINANCEOS_GOVERNED_CONTEXT",
        f"version: {GOVERNED_INPUT_VERSION}",
        f"agent: {slug}",
        f"certification_status: {certification}",
        "authorities: PORTAL=operational_revenue_truth; XERO=accounting_ledger_truth; FINANCEOS=governed_certified_representation",
        "commissions: expected_commission / tracker_commission / booked_commission / paid_commission are distinct; do not collapse",
        "intercompany: do not label royalty without an adopted instrument; default HOLD / UNRESOLVED_INTERCOMPANY_CHARGE",
        "excel_sheets: not actuals authority",
        "prohibited: activate_mapping, certify_data, redefine_adopted_metric, write_financeos_state, write_xero, claim_business_completion_without_proof",
    ]
    if present:
        lines.append("present_fields:")
        for key, value in present.items():
            lines.append(f"  {key}: {value!r}")
    if absent:
        lines.append("absent_fields: " + ",".join(absent))
        lines.append("do_not_fabricate_absent_fields: true")
    if flags:
        lines.append("source_authority_flags: " + ",".join(flags))
    if certification in BLOCKING_CERTIFICATION_STATES:
        lines.append("do_not_treat_as_certified_actuals: true")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live fetch -- optional, in addition to the pre-existing local/test
# `financeos_context` envelope above. Callers that pass `financeos_context`
# directly are unaffected; this path only runs when a caller explicitly asks
# for it (see main.py's `financeos_live_fetch` field) and produces the SAME
# envelope shape, validated by the SAME `bind_financeos_governed_context`
# above -- there is no second, looser validation path for live data.
# ---------------------------------------------------------------------------

async def fetch_live_financeos_context(
    *,
    entity_id: str | None = None,
    data_product_id: str | None = None,
    division_id: str | None = None,
    channel_id: str | None = None,
    period_start_from: str | None = None,
    period_end_to: str | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Call FinanceOS's live governed read layer
    (`GET /api/v1/data-products/revenue[/:id]`) and shape the result into a
    dict `bind_financeos_governed_context` validates exactly like the
    pre-existing local/test envelope.

    Never fabricates a figure. Three cases, each honestly distinguishable by
    the returned `live_fetch_state`:
      - endpoint reachable, capability token accepted, at least one certified
        row: `reachable_found`, with `certification_status` taken verbatim
        from the row FinanceOS itself certified -- never recomputed here.
      - endpoint reachable, capability token accepted, zero rows: `certified
        data product exists but is empty` is a genuinely different fact from
        `endpoint unreachable`, so this returns `reachable_empty`, NOT
        `certification_status: CERTIFIED` and NOT an error.
      - not configured / transport failure / 401 / 403: `not_configured` /
        `unreachable` / `unauthorized` -- a real failure, never silently
        collapsed into "no data" (which would be indistinguishable from a
        caller that never asked at all).
    """
    from accounting.financeos_data_products_client import (
        data_products_configured,
        get_revenue_data_product_by_id,
        get_revenue_data_products,
    )

    if not data_products_configured():
        return {"live_fetch_state": "not_configured"}

    if data_product_id:
        result = await get_revenue_data_product_by_id(data_product_id, entity_id=entity_id, session=session)
        row = result.get("revenue") if result.get("ok") else None
        rows = [row] if isinstance(row, Mapping) else []
    else:
        result = await get_revenue_data_products(
            entity_id=entity_id,
            division_id=division_id,
            channel_id=channel_id,
            period_start_from=period_start_from,
            period_end_to=period_end_to,
            session=session,
        )
        raw_rows = result.get("revenue") if result.get("ok") else None
        rows = raw_rows if isinstance(raw_rows, list) else []

    if not result.get("ok"):
        status = result.get("status")
        if status in (401, 403):
            return {"live_fetch_state": "unauthorized"}
        if status == 400:
            return {"live_fetch_state": "invalid_request"}
        return {"live_fetch_state": "unreachable"}

    if not rows:
        return {"live_fetch_state": "reachable_empty"}

    row = rows[0]
    if not isinstance(row, Mapping):
        return {"live_fetch_state": "unreachable"}

    ctx: dict[str, Any] = {
        "live_fetch_state": "reachable_found",
        "data_product_id": row.get("id"),
        "data_product_version": row.get("data_product_version"),
        "certification_status": row.get("certification_status"),
        "reconciliation_status": row.get("reconciliation_status"),
        "freshness": row.get("freshness_status"),
        "as_of": row.get("updated_at"),
        "entity": entity_id or row.get("entity_id"),
    }
    period_start = row.get("period_start_date")
    period_end = row.get("period_end_date")
    if period_start or period_end:
        ctx["period"] = f"{period_start or '?'}/{period_end or '?'}"
    lineage_summary = row.get("lineage_summary")
    if isinstance(lineage_summary, Mapping):
        source_keys = lineage_summary.get("source_node_keys")
        if isinstance(source_keys, list) and source_keys:
            ctx["lineage"] = source_keys

    return {k: v for k, v in ctx.items() if k == "live_fetch_state" or v not in (None, "", [], {})}
