"""
Job C: Store Pull Replenishment Engine
Executes Argha's nightly base-stock pull calculations using dynamic PostgreSQL
day-of-week and month-cycle calendar constants, reconciles with HCE bottom-up demand,
decouples doorstep rejection R(i) into procurement alerts without suppressing demand,
enforces both category-level and aggregate Swadishtt kitchen capacity constraints,
applies case pack / MOQ rounding, and saves PO proposals in PostgreSQL.
"""

from datetime import datetime, date, timedelta, timezone
import math
from typing import List, Dict, Optional
import uuid
import logging

from app.database import db
from app.models.schemas import (
    PurchaseOrderProposal,
    PurchaseOrderItem,
    ProcurementAlert,
    POStatus,
    ProductCategory,
)

logger = logging.getLogger(__name__)

WEEKDAY_KEYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_WEEKDAY_FACTORS = {
    "Mon": 0.95,
    "Tue": 0.95,
    "Wed": 0.97,
    "Thu": 0.98,
    "Fri": 1.03,
    "Sat": 1.10,
    "Sun": 1.02,
}


def get_month_cycle_key(target_day: int) -> str:
    if 1 <= target_day <= 7:
        return "Days_1_7"
    elif 8 <= target_day <= 22:
        return "Days_8_22"
    return "Days_23_31"


async def execute_nightly_pull(
    target_delivery_date: Optional[date] = None,
    festival_factor_override: Optional[float] = None,
) -> PurchaseOrderProposal:
    """
    Executes nightly store pull calculation against PostgreSQL:
    1. Load weekday c(w) and month-cycle M(t+1) constants from cdao_calendar_factors
    2. Reconcile trailing average A(i,w) with HCE bottom-up demand E_h (>15% divergence)
    3. Multiplicative demand: D(i, t+1) = D_base * c(w) * F * M
       NOTE: Doorstep rejection factor R(i) is DECOUPLED from D(i, t+1) so quality
       rejections trigger a ProcurementAlert instead of artificially shrinking demand.
    4. Order-up-to target: S = D * (1 + b_i)
    5. Raw order: Q_raw = max(0, S - Sound_OnHand) (excluding kitchen-routed/composted batches)
    6. Kitchen capacity K enforcement (total cap + category caps in strict order:
       leafy_greens -> dairy -> base_veg down to 0% buffer)
    7. Case pack ceiling and MOQ enforcement
    8. Persist PurchaseOrderProposal in PENDING_APPROVAL state
    """
    now = datetime.now(timezone.utc)
    if not target_delivery_date:
        target_delivery_date = (datetime.now() + timedelta(days=1)).date()

    skus = await db.get_all_skus()
    calendar_factors = await db.get_calendar_factors()
    kitchen_caps = await db.get_kitchen_capacities()
    eh_map = await db.get_bottom_up_demand()

    # 1. Calendar Constants from PostgreSQL cdao_calendar_factors
    weekday_key = WEEKDAY_KEYS[target_delivery_date.weekday()]
    month_key = get_month_cycle_key(target_delivery_date.day)

    weekday_table = calendar_factors.get("WEEKDAY", DEFAULT_WEEKDAY_FACTORS)
    month_table = calendar_factors.get("MONTH_CYCLE", {"Days_1_7": 1.08, "Days_8_22": 1.00, "Days_23_31": 0.92})

    day_constant_c = weekday_table.get(weekday_key, 1.00)
    month_factor_M = month_table.get(month_key, 1.00)
    festival_factor_F = festival_factor_override if festival_factor_override is not None else 1.00

    K_total = kitchen_caps.get("total_fresh", 35.0)
    category_caps = {
        ProductCategory.LEAFY_GREENS.value: kitchen_caps.get(ProductCategory.LEAFY_GREENS.value, K_total),
        ProductCategory.DAIRY.value: kitchen_caps.get(ProductCategory.DAIRY.value, K_total),
        ProductCategory.BASE_VEG.value: kitchen_caps.get(ProductCategory.BASE_VEG.value, K_total),
        "total_fresh": K_total,
    }

    # Initialize working buffers from SKU master
    initial_buffers: Dict[str, float] = {s.sku_id: s.buffer_percentage for s in skus}
    working_buffers: Dict[str, float] = {s.sku_id: s.buffer_percentage for s in skus}

    # Pre-fetch Sound On-Hand for each SKU (strictly SELLABLE batches from WMS)
    sound_stock_map: Dict[str, float] = {}
    for s in skus:
        sound_stock_map[s.sku_id] = await db.get_sound_on_hand(s.sku_id)

    # 2. Demand Estimation, HCE Cross-Check & Doorstep Rejection Decoupling
    sku_demand_cache = {}
    procurement_alerts: List[ProcurementAlert] = []
    min_r_observed = 1.00

    for s in skus:
        A_iw = s.trailing_weekly_avg
        E_h = eh_map.get(s.sku_id, 0.0)

        # Cross-check A(i, w) against bottom-up E_h only when HCE batch output is available
        if eh_map and s.sku_id in eh_map:
            if A_iw > 0:
                divergence_pct = abs(E_h - A_iw) / A_iw
            else:
                divergence_pct = 1.0 if E_h > 0 else 0.0

            if divergence_pct > 0.15:
                if s.is_fresh:
                    base_demand = max(A_iw, E_h)
                else:
                    base_demand = (A_iw + E_h) / 2.0
            else:
                base_demand = A_iw
        else:
            divergence_pct = 0.0
            base_demand = A_iw

        R_i = s.doorstep_rejection_factor_R
        min_r_observed = min(min_r_observed, R_i)

        # Decouple R(i) from D(i, t+1): do NOT multiply base demand by R(i)
        D_next = base_demand * day_constant_c * festival_factor_F * month_factor_M

        if R_i < 0.95:
            severity = "HIGH" if R_i < 0.85 else "MEDIUM"
            procurement_alerts.append(
                ProcurementAlert(
                    sku_id=s.sku_id,
                    sku_name=s.name,
                    rejection_factor_r=round(R_i, 3),
                    severity=severity,
                    recommended_action=(
                        f"PROCUREMENT_ALERT: Doorstep rejection factor R={R_i:.2f} below 0.95 threshold. "
                        "Audit farm/supplier lot and switch vendor; customer demand D(i, t+1) kept unpenalized."
                    ),
                )
            )

        sku_demand_cache[s.sku_id] = {
            "A_iw": A_iw,
            "E_h": E_h,
            "divergence_pct": divergence_pct,
            "base_demand": base_demand,
            "R_i": R_i,
            "D_next": D_next,
        }

    # 3. Kitchen Capacity Constraint Enforcement (Category-level & Total Caps)
    # Strict trimming priority: leafy_greens -> dairy -> base_veg down to 0% buffer
    iterations = 0
    max_iterations = 200
    overflow_exceeded = False

    category_trim_priority = [
        ProductCategory.LEAFY_GREENS,
        ProductCategory.DAIRY,
        ProductCategory.BASE_VEG,
    ]

    current_raw_orders: Dict[str, tuple] = {}
    cat_overflow: Dict[str, float] = {}
    total_fresh_overflow = 0.0

    while iterations < max_iterations:
        total_fresh_overflow = 0.0
        cat_overflow = {c.value: 0.0 for c in category_trim_priority}
        current_raw_orders.clear()

        for s in skus:
            D_next = sku_demand_cache[s.sku_id]["D_next"]
            buf = working_buffers[s.sku_id]
            S = D_next * (1.0 + buf)
            sound_stock = sound_stock_map[s.sku_id]
            raw = max(0.0, S - sound_stock)
            overflow_i = max(0.0, S - max(D_next, sound_stock)) if s.is_fresh else 0.0
            current_raw_orders[s.sku_id] = (S, raw, sound_stock, overflow_i)

            if s.is_fresh:
                total_fresh_overflow += overflow_i
                if s.category.value in cat_overflow:
                    cat_overflow[s.category.value] += overflow_i

        # Check aggregate cap AND every category-specific cap
        any_cat_exceeded = any(
            cat_overflow[c.value] > category_caps.get(c.value, K_total) for c in category_trim_priority
        )
        if total_fresh_overflow <= K_total and not any_cat_exceeded:
            break

        overflow_exceeded = True
        iterations += 1
        trimmed = False

        # Trim strictly in priority order (leafy_greens -> dairy -> base_veg) while total cap is exceeded
        if total_fresh_overflow > K_total:
            for cat in category_trim_priority:
                cat_skus = [s for s in skus if s.category == cat and working_buffers[s.sku_id] > 0.0]
                if cat_skus:
                    for s in cat_skus:
                        working_buffers[s.sku_id] = max(0.0, round(working_buffers[s.sku_id] - 0.01, 4))
                    trimmed = True
                    break

        # If total cap holds or all earlier categories are 0, enforce per-category caps in strict order
        if not trimmed:
            for cat in category_trim_priority:
                if cat_overflow[cat.value] > category_caps.get(cat.value, K_total):
                    cat_skus = [s for s in skus if s.category == cat and working_buffers[s.sku_id] > 0.0]
                    if cat_skus:
                        for s in cat_skus:
                            working_buffers[s.sku_id] = max(0.0, round(working_buffers[s.sku_id] - 0.01, 4))
                        trimmed = True
                        break

        if not trimmed:
            break

    # 4. Inventory Math: MOQ and Case Pack Rounding
    po_items: List[PurchaseOrderItem] = []
    total_proposed_units = 0

    for s in skus:
        cache = sku_demand_cache[s.sku_id]
        buf = working_buffers[s.sku_id]
        init_buf = initial_buffers[s.sku_id]
        S, raw_order, sound_stock, overflow_contrib = current_raw_orders[s.sku_id]

        if raw_order > 0:
            case_pack = max(1, s.case_pack_size)
            cases_needed = math.ceil(raw_order / case_pack)
            case_rounded = cases_needed * case_pack
            proposed_qty = max(case_rounded, s.moq)
        else:
            proposed_qty = 0

        total_proposed_units += proposed_qty

        po_items.append(
            PurchaseOrderItem(
                sku_id=s.sku_id,
                name=s.name,
                category=s.category,
                trailing_avg_Aiw=round(cache["A_iw"], 2),
                bottom_up_Eh=round(cache["E_h"], 2),
                divergence_pct=round(cache["divergence_pct"] * 100, 1),
                base_demand=round(cache["base_demand"], 2),
                day_constant_c=day_constant_c,
                month_factor_M=month_factor_M,
                rejection_factor_R=cache["R_i"],
                reconciled_demand_D=round(cache["D_next"], 2),
                initial_buffer_percentage=round(init_buf * 100, 1),
                buffer_percentage_applied=round(buf * 100, 1),
                order_up_to_S=round(S, 2),
                sound_on_hand=round(sound_stock, 2),
                raw_order=round(raw_order, 2),
                case_pack_size=s.case_pack_size,
                moq=s.moq,
                proposed_quantity=proposed_qty,
                approved_quantity=None,
                kitchen_overflow_kg=round(overflow_contrib, 2),
            )
        )

    po_id = f"PO-{target_delivery_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    proposal = PurchaseOrderProposal(
        po_id=po_id,
        created_at=now,
        status=POStatus.PENDING_APPROVAL,
        target_delivery_date=target_delivery_date,
        festival_factor_F=festival_factor_F,
        month_salary_factor_M=month_factor_M,
        doorstep_rejection_factor_R=round(min_r_observed, 3),
        kitchen_capacity_cap_kg=K_total,
        category_kitchen_caps_kg=category_caps,
        category_overflow_kg={k: round(v, 2) for k, v in cat_overflow.items()},
        total_fresh_overflow_kg=round(total_fresh_overflow, 2),
        overflow_cap_exceeded=overflow_exceeded,
        buffer_reduction_iterations=iterations,
        total_proposed_units=total_proposed_units,
        procurement_alerts=procurement_alerts,
        items=po_items,
        reviewed_by=None,
        reviewed_at=None,
        override_reason=None,
    )

    await db.save_po_proposal(proposal)

    logger.info(
        f"Nightly Pull calculated PO Proposal {po_id}: {total_proposed_units} units across {len(po_items)} SKUs. "
        f"Alerts: {len(procurement_alerts)}. Fresh overflow: {total_fresh_overflow:.2f} kg / {K_total:.2f} kg cap."
    )
    return proposal
