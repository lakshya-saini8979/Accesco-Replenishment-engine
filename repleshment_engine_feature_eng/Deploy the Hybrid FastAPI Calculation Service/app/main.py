"""
Accesco Hybrid Replenishment Engine - Production-Ready FastAPI Backend Service
Wired directly to local/Azure PostgreSQL via asynchronous SQLAlchemy + asyncpg.
Coordinates Daytime Fresh Routing (No-Expiry), Evening HCE Nudges with CDAO Guardrails,
and Nightly Store Pull Replenishment with Warehouse Staff Approval Workflows.
"""

from contextlib import asynccontextmanager
from datetime import datetime, date, timezone
import uuid
import logging
from typing import Optional, List

from fastapi import FastAPI, BackgroundTasks, Query, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import db
from app.core import no_expiry, hce_engine, pull_theory
from app.scheduler import start_scheduler, shutdown_scheduler
from app.models.schemas import (
    FreshRoutingReport,
    HceReport,
    PurchaseOrderProposal,
    POStatus,
    POReviewRequest,
    NudgeAuditRecord,
    NudgeGateStatus,
    DeliveryConfirmationEvent,
    WardrobeResponse,
    ReorderFeedbackRequest,
    BoughtElsewhereFeedbackRequest,
    StillHavePlentyFeedbackRequest,
    DismissFeedbackRequest,
    HouseholdSkuFeedback,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("accesco-engine")


# --- Observability: Correlation ID Middleware ---
class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        corr_id = request.headers.get("X-Correlation-ID", f"req-{uuid.uuid4().hex[:8]}")
        request.state.correlation_id = corr_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = corr_id
        return response


# --- Application Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Accesco Replenishment Engine wired to PostgreSQL...")
    connected = await db.check_connection()
    logger.info(f"PostgreSQL connectivity check: {'CONNECTED' if connected else 'UNAVAILABLE'}")
    start_scheduler()
    yield
    logger.info("Shutting down Accesco Replenishment Engine...")
    shutdown_scheduler()
    await db.dispose()


app = FastAPI(
    title="Accesco Hybrid Replenishment Engine",
    version="2.1.0",
    description=(
        "Production-grade FastAPI calculation service backed by PostgreSQL, implementing Argha's "
        "No-Expiry Engine, Household Consumption Engine (HCE), and Pull Theory "
        "with CDAO governance guardrails and warehouse approval workflows."
    ),
    lifespan=lifespan,
)

app.add_middleware(CorrelationIdMiddleware)


# --- Global Exception Handler (RFC 7807) ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    corr_id = getattr(request.state, "correlation_id", "unknown")
    logger.error(f"Unhandled error [{corr_id}]: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "detail": str(exc),
            "correlation_id": corr_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


# --- Service Health & Metadata ---
@app.get("/health")
@app.get("/")
async def health_check():
    pg_ok = await db.check_connection()
    sys_cfg = await db.get_system_config() if pg_ok else None
    kitchen_caps = await db.get_kitchen_capacities() if pg_ok else {}
    return {
        "service": "Accesco Hybrid Replenishment Engine",
        "version": "2.1.0",
        "status": "HEALTHY" if pg_ok else "DEGRADED",
        "storage_mode": "PostgreSQL (asyncpg)",
        "database_connected": pg_ok,
        "kill_switch_active": sys_cfg.emergency_kill_switch if sys_cfg else False,
        "kitchen_capacity_caps_kg": kitchen_caps,
        "cooldown_days": sys_cfg.cooldown_days if sys_cfg else 3,
        "quiet_hours": f"{sys_cfg.quiet_hours_start}:00 to {sys_cfg.quiet_hours_end}:00" if sys_cfg else "21:00 to 8:00",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ==============================================================================
# 1. CORE REPLENISHMENT JOBS
# ==============================================================================

@app.post("/jobs/fresh-routing", response_model=FreshRoutingReport, tags=["Core Jobs"])
async def trigger_fresh_routing(
    background_tasks: BackgroundTasks,
    run_sync: bool = Query(True, description="If True, executes synchronously and returns calculation report."),
):
    """
    Evaluates shelf batches from wms_inventory_batch:
    - Fresh Produce: <=48h (Sellable), 48-72h (Kitchen), >72h (Compost).
    - Packaged FMCG: L >= 0.50 (Sellable), 0.30 <= L < 0.50 (Kitchen), L < 0.30 (Compost).
    - Enforces FSSAI floor (>= 30% life and >= 45 days remaining).
    - Updates wms_inventory_batch.status in PostgreSQL so downstream jobs exclude kitchen/compost stock.
    """
    if run_sync:
        return await no_expiry.execute_routing()
    background_tasks.add_task(no_expiry.execute_routing)
    return JSONResponse(content={"status": "PROCESSING", "job": "Fresh Routing"}, status_code=202)


@app.post("/jobs/hce-nudge", response_model=HceReport, tags=["Core Jobs"])
async def trigger_hce_nudges(
    background_tasks: BackgroundTasks,
    run_sync: bool = Query(True, description="If True, executes synchronously and returns calculation report."),
    ignore_quiet_hours: bool = Query(False, description="Set True for manual testing outside quiet hours."),
):
    """
    Executes Household Consumption Engine against oms_households & oms_customer_purchase_log:
    - Bayesian Shrinkage r(h,i)
    - Low-stock day calculation (theta = 0.20)
    - Gamma CDF reorder probability P_reorder (paused when dormant >7d)
    - CDAO 8-Gate Safety Check (kill-switch, dormancy, opt-out, stockout, cart, quiet hours, cooldown, freq cap)
    - Records immutable audit ledger in cdao_nudge_audit_log
    - Aggregates bottom-up demand E_h(i, t+1)
    """
    if run_sync:
        return await hce_engine.execute_hce_batch(ignore_quiet_hours=ignore_quiet_hours)
    background_tasks.add_task(hce_engine.execute_hce_batch, ignore_quiet_hours)
    return JSONResponse(content={"status": "PROCESSING", "job": "HCE Nudges"}, status_code=202)


@app.post("/jobs/store-pull", response_model=PurchaseOrderProposal, tags=["Core Jobs"])
async def trigger_store_pull(
    background_tasks: BackgroundTasks,
    run_sync: bool = Query(True, description="If True, executes synchronously and returns PO proposal."),
    delivery_date: Optional[date] = Query(None, description="Target delivery date (defaults to tomorrow)"),
    festival_factor: Optional[float] = Query(None, description="Optional festival multiplier override F"),
):
    """
    Executes Store Pull Replenishment against PostgreSQL:
    - Dynamic calendar constants from cdao_calendar_factors
    - Cross-checks with HCE E_h (>15% divergence)
    - Decouples Doorstep Rejection Factor R(i) into ProcurementAlerts without reducing demand D(i, t+1)
    - Enforces Swadishtt Kitchen category & total capacity caps (leafy_greens -> dairy -> base_veg)
    - Applies case-pack rounding and MOQ rules
    - Persists proposal in cdao_po_proposals with PENDING_APPROVAL status
    """
    if run_sync:
        return await pull_theory.execute_nightly_pull(
            target_delivery_date=delivery_date,
            festival_factor_override=festival_factor,
        )
    background_tasks.add_task(pull_theory.execute_nightly_pull, delivery_date, festival_factor)
    return JSONResponse(content={"status": "PROCESSING", "job": "Store Pull"}, status_code=202)


@app.post("/jobs/run-all-e2e", tags=["Core Jobs"])
async def run_all_e2e(ignore_quiet_hours: bool = Query(True, description="Bypass quiet hours for full pipeline testing")):
    """Executes the entire daily replenishment lifecycle end-to-end against PostgreSQL."""
    routing_rep = await no_expiry.execute_routing()
    hce_rep = await hce_engine.execute_hce_batch(ignore_quiet_hours=ignore_quiet_hours)
    pull_rep = await pull_theory.execute_nightly_pull()

    return {
        "status": "SUCCESS",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "step_1_fresh_routing": {
            "sellable_batches": routing_rep.sellable_count,
            "routed_to_kitchen": routing_rep.routed_to_kitchen_count,
            "composted": routing_rep.compost_count,
        },
        "step_2_hce_nudges": {
            "nudges_generated": hce_rep.nudges_generated,
            "nudges_blocked": hce_rep.nudges_blocked,
            "bottom_up_demand_Eh": hce_rep.bottom_up_demand_Eh,
        },
        "step_3_store_pull": {
            "po_id": pull_rep.po_id,
            "status": pull_rep.status,
            "total_units_proposed": pull_rep.total_proposed_units,
            "kitchen_overflow_kg": pull_rep.total_fresh_overflow_kg,
            "kitchen_cap_exceeded": pull_rep.overflow_cap_exceeded,
            "procurement_alerts": len(pull_rep.procurement_alerts),
            "items_count": len(pull_rep.items),
        },
    }


# ==============================================================================
# 2. WAREHOUSE STAFF APPROVAL & OVERRIDE WORKFLOW (CDAO MANDATE)
# ==============================================================================

@app.get("/po/proposals", response_model=List[PurchaseOrderProposal], tags=["Warehouse Approvals"])
async def list_po_proposals():
    return await db.get_all_po_proposals()


@app.get("/po/proposals/{po_id}", response_model=PurchaseOrderProposal, tags=["Warehouse Approvals"])
async def get_po_proposal_by_id(po_id: str):
    po = await db.get_po_proposal(po_id)
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase Order {po_id} not found.")
    return po


@app.post("/po/proposals/{po_id}/approve", response_model=PurchaseOrderProposal, tags=["Warehouse Approvals"])
async def approve_po_proposal(po_id: str, review: POReviewRequest):
    po = await db.get_po_proposal(po_id)
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase Order {po_id} not found.")
    if po.status != POStatus.PENDING_APPROVAL:
        raise HTTPException(status_code=400, detail=f"PO {po_id} is already in {po.status} state.")

    updated = await db.update_po_status(po_id=po_id, status=POStatus.APPROVED, reviewed_by=review.reviewed_by)
    logger.info(f"PO {po_id} APPROVED by {review.reviewed_by}.")
    return updated


@app.post("/po/proposals/{po_id}/override", response_model=PurchaseOrderProposal, tags=["Warehouse Approvals"])
async def override_po_proposal(po_id: str, review: POReviewRequest):
    po = await db.get_po_proposal(po_id)
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase Order {po_id} not found.")
    if not review.override_reason:
        raise HTTPException(status_code=400, detail="An override_reason is required when overriding quantities.")
    if not review.override_quantities:
        raise HTTPException(status_code=400, detail="override_quantities mapping must be provided.")

    updated = await db.update_po_status(
        po_id=po_id,
        status=POStatus.OVERRIDDEN,
        reviewed_by=review.reviewed_by,
        override_reason=review.override_reason,
        overridden_items=review.override_quantities,
    )
    logger.info(f"PO {po_id} OVERRIDDEN by {review.reviewed_by}. Reason: {review.override_reason}")
    return updated


@app.post("/po/proposals/{po_id}/reject", response_model=PurchaseOrderProposal, tags=["Warehouse Approvals"])
async def reject_po_proposal(po_id: str, review: POReviewRequest):
    po = await db.get_po_proposal(po_id)
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase Order {po_id} not found.")
    if not review.override_reason:
        raise HTTPException(status_code=400, detail="A rejection reason is required.")

    updated = await db.update_po_status(
        po_id=po_id,
        status=POStatus.REJECTED,
        reviewed_by=review.reviewed_by,
        override_reason=review.override_reason,
    )
    logger.info(f"PO {po_id} REJECTED by {review.reviewed_by}. Reason: {review.override_reason}")
    return updated


# ==============================================================================
# 3. AUDIT & GOVERNANCE
# ==============================================================================

@app.get("/audit/nudges", response_model=List[NudgeAuditRecord], tags=["Audit & Governance"])
async def get_nudge_audits(limit: int = Query(50, ge=1, le=500)):
    return await db.get_nudge_audits(limit=limit)


@app.post("/governance/kill-switch", tags=["Audit & Governance"])
async def set_kill_switch(active: bool = Query(..., description="True to halt all customer reminders")):
    await db.set_kill_switch(active)
    status_str = "ACTIVATED (All customer reminders paused)" if active else "DEACTIVATED (Reminders resumed)"
    logger.warning(f"Emergency Kill Switch {status_str}.")
    return {
        "status": "SUCCESS",
        "emergency_kill_switch": active,
        "message": status_str,
    }


# ==============================================================================
# 4. PHASE 1: DATA INGESTION (DOORSTEP ACCEPTANCE WEBHOOK)
# ==============================================================================

@app.post("/events/delivery-confirmed", status_code=status.HTTP_201_CREATED, tags=["Phase 1: Ingestion"])
@app.post("/events/doorstep-accepted", status_code=status.HTTP_201_CREATED, tags=["Phase 1: Ingestion"])
async def handle_doorstep_delivery_event(event: DeliveryConfirmationEvent):
    """
    Phase 1: Webhook receiving the 'accepted at the door' trigger.
    The depletion clock MUST start at delivery time (d_n), NOT checkout time.
    """
    deliv_dt = event.delivered_at or datetime.now(timezone.utc)
    event_id = event.event_id or f"EVT-DELIV-{uuid.uuid4().hex[:8].upper()}"

    household = await db.get_household(event.household_id)
    if not household:
        raise HTTPException(status_code=404, detail=f"Household {event.household_id} not found.")

    sku = await db.get_sku(event.sku_id)
    if not sku:
        raise HTTPException(status_code=404, detail=f"SKU {event.sku_id} not found.")

    order = await db.record_delivery_event(
        event_id=event_id,
        household_id=event.household_id,
        sku_id=event.sku_id,
        quantity=event.quantity,
        delivered_at=deliv_dt,
        feedback_status="NORMAL",
    )

    logger.info(
        f"Doorstep delivery accepted: event_id={event_id}, household={event.household_id}, "
        f"sku={event.sku_id}, qty={event.quantity}, d_n={deliv_dt.isoformat()}"
    )

    return {
        "status": "SUCCESS",
        "event": "DOORSTEP_DELIVERY_ACCEPTED",
        "event_id": event_id,
        "household_id": event.household_id,
        "sku_id": event.sku_id,
        "quantity": event.quantity,
        "depletion_clock_started_at": deliv_dt.isoformat(),
        "delivery_date": order.order_date.isoformat(),
        "message": "Accepted-at-door trigger confirmed. Depletion clock started at delivery time d_n.",
    }


# ==============================================================================
# 5. PHASE 4: FRONTEND WARDROBE UI PAYLOAD
# ==============================================================================

@app.get("/nudges/wardrobe/{household_id}", response_model=WardrobeResponse, tags=["Phase 4: Frontend"])
@app.get("/frontend/wardrobe/{household_id}", response_model=WardrobeResponse, tags=["Phase 4: Frontend"])
async def get_household_wardrobe_payload(household_id: str):
    """
    Phase 4: Structured JSON payload for the frontend 'Wardrobe' UI.
    Evaluates items 1 to 2 days prior to T_low date.
    Returns sku_id, estimated_days_remaining, stock level, and explicit CTA triggers (Reorder, Dismiss).
    """
    household = await db.get_household(household_id)
    if not household:
        raise HTTPException(status_code=404, detail=f"Household {household_id} not found.")

    return await hce_engine.generate_wardrobe_payload(household_id)


# ==============================================================================
# 6. PHASE 5: THE FEEDBACK LOOP (SELF-CORRECTION)
# ==============================================================================

@app.post("/feedback/reorder", tags=["Phase 5: Feedback Loop"])
async def feedback_reorder(req: ReorderFeedbackRequest):
    """
    Phase 5.1: 'Reorder' Feedback
    Logs a successful prediction in the immutable audit trail and queues reorder.
    """
    household = await db.get_household(req.household_id)
    if not household:
        raise HTTPException(status_code=404, detail=f"Household {req.household_id} not found.")

    sku = await db.get_sku(req.sku_id)
    if not sku:
        raise HTTPException(status_code=404, detail=f"SKU {req.sku_id} not found.")

    qty = req.quantity or sku.typical_order_qty
    now = datetime.now(timezone.utc)

    # 1. Record successful prediction in audit ledger
    audit_record = NudgeAuditRecord(
        audit_id=f"AUD-FB-REORDER-{uuid.uuid4().hex[:8].upper()}",
        household_id=req.household_id,
        sku_id=req.sku_id,
        sku_name=sku.name,
        evaluated_at=now,
        reorder_probability=1.0,
        gate_status=NudgeGateStatus.PASSED,
        action_taken="FEEDBACK_REORDER_CONFIRMED",
        reason=f"Customer confirmed replenishment via Reorder CTA: {req.notes or 'Successful prediction'}",
    )
    await db.record_nudge_audit(audit_record)

    # 2. Record feedback state
    fb = HouseholdSkuFeedback(
        household_id=req.household_id,
        sku_id=req.sku_id,
        feedback_type="REORDER",
        rate_multiplier=1.0000,
        virtual_stock_reset_at=None,
        last_feedback_at=now,
        notes=req.notes,
    )
    await db.save_household_sku_feedback(fb)

    # 3. Ingest purchase order event
    order_id = f"ORD-REORDER-{uuid.uuid4().hex[:8].upper()}"
    await db.record_delivery_event(
        event_id=order_id,
        household_id=req.household_id,
        sku_id=req.sku_id,
        quantity=qty,
        delivered_at=now,
        feedback_status="REORDERED",
    )

    logger.info(f"Feedback REORDER logged for {req.household_id} / {req.sku_id}.")
    return {
        "status": "SUCCESS",
        "action": "REORDER",
        "household_id": req.household_id,
        "sku_id": req.sku_id,
        "prediction_outcome": "ACCURATE",
        "order_id": order_id,
        "reorder_quantity": qty,
        "message": "Successful replenishment prediction logged. Reorder purchase event confirmed.",
    }


@app.post("/feedback/bought-elsewhere", tags=["Phase 5: Feedback Loop"])
async def feedback_bought_elsewhere(req: BoughtElsewhereFeedbackRequest):
    """
    Phase 5.2: 'Bought Elsewhere' Feedback
    Resets the virtual stock to 100% full, but MUST NOT shrink the consumption rate r.
    """
    household = await db.get_household(req.household_id)
    if not household:
        raise HTTPException(status_code=404, detail=f"Household {req.household_id} not found.")

    sku = await db.get_sku(req.sku_id)
    if not sku:
        raise HTTPException(status_code=404, detail=f"SKU {req.sku_id} not found.")

    now = datetime.now(timezone.utc)

    # Reset virtual stock to 100% without modifying consumption rate r
    fb = HouseholdSkuFeedback(
        household_id=req.household_id,
        sku_id=req.sku_id,
        feedback_type="BOUGHT_ELSEWHERE",
        rate_multiplier=1.0000,  # CRITICAL: MUST NOT shrink consumption rate r
        virtual_stock_reset_at=now,
        last_feedback_at=now,
        notes=req.notes or "Replenished from external retailer",
    )
    await db.save_household_sku_feedback(fb)

    audit_record = NudgeAuditRecord(
        audit_id=f"AUD-FB-ELSEWHERE-{uuid.uuid4().hex[:8].upper()}",
        household_id=req.household_id,
        sku_id=req.sku_id,
        sku_name=sku.name,
        evaluated_at=now,
        reorder_probability=0.0,
        gate_status=NudgeGateStatus.PASSED,
        action_taken="VIRTUAL_STOCK_RESET_100PCT",
        reason="Customer bought elsewhere; stock reset to 100%, consumption rate r preserved without shrinkage.",
    )
    await db.record_nudge_audit(audit_record)

    logger.info(f"Feedback BOUGHT_ELSEWHERE: Virtual stock reset to 100% for {req.household_id}/{req.sku_id}, rate r unchanged.")
    return {
        "status": "SUCCESS",
        "action": "BOUGHT_ELSEWHERE",
        "household_id": req.household_id,
        "sku_id": req.sku_id,
        "virtual_stock_percentage": 100.0,
        "rate_r_shrunk": False,
        "rate_multiplier": 1.0,
        "reset_timestamp": now.isoformat(),
        "message": "Virtual inventory reset to 100% full. Consumption rate r preserved without shrinkage.",
    }


@app.post("/feedback/still-have-plenty", tags=["Phase 5: Feedback Loop"])
async def feedback_still_have_plenty(req: StillHavePlentyFeedbackRequest):
    """
    Phase 5.3: 'Still Have Plenty' Feedback
    Mathematically lowers the daily consumption rate r(h,i) so the next prediction cycle arrives later.
    """
    household = await db.get_household(req.household_id)
    if not household:
        raise HTTPException(status_code=404, detail=f"Household {req.household_id} not found.")

    sku = await db.get_sku(req.sku_id)
    if not sku:
        raise HTTPException(status_code=404, detail=f"SKU {req.sku_id} not found.")

    now = datetime.now(timezone.utc)

    # Check existing feedback to compound or apply reduction factor
    existing_fb = await db.get_household_sku_feedback(req.household_id, req.sku_id)
    current_multiplier = existing_fb.rate_multiplier if existing_fb else 1.0
    new_multiplier = max(0.20, round(current_multiplier * req.reduction_factor, 4))

    fb = HouseholdSkuFeedback(
        household_id=req.household_id,
        sku_id=req.sku_id,
        feedback_type="STILL_HAVE_PLENTY",
        rate_multiplier=new_multiplier,
        virtual_stock_reset_at=existing_fb.virtual_stock_reset_at if existing_fb else None,
        last_feedback_at=now,
        notes=req.notes or f"Consumption rate dampened by {int((1 - req.reduction_factor) * 100)}%",
    )
    await db.save_household_sku_feedback(fb)

    reduction_pct = int((1.0 - new_multiplier) * 100)
    audit_record = NudgeAuditRecord(
        audit_id=f"AUD-FB-PLENTY-{uuid.uuid4().hex[:8].upper()}",
        household_id=req.household_id,
        sku_id=req.sku_id,
        sku_name=sku.name,
        evaluated_at=now,
        reorder_probability=0.0,
        gate_status=NudgeGateStatus.PASSED,
        action_taken="CONSUMPTION_RATE_LOWERED",
        reason=f"Customer indicated plenty remaining; r(h,i) lowered (multiplier={new_multiplier}, -{reduction_pct}%) to delay next cycle.",
    )
    await db.record_nudge_audit(audit_record)

    logger.info(f"Feedback STILL_HAVE_PLENTY for {req.household_id}/{req.sku_id}: rate multiplier lowered to {new_multiplier}.")
    return {
        "status": "SUCCESS",
        "action": "STILL_HAVE_PLENTY",
        "household_id": req.household_id,
        "sku_id": req.sku_id,
        "rate_multiplier_applied": new_multiplier,
        "reduction_percentage": f"{reduction_pct}%",
        "self_correction": "CONSUMPTION_RATE_LOWERED",
        "message": f"Daily consumption rate r(h,i) mathematically lowered by {int((1-req.reduction_factor)*100)}%. Next prediction cycle arrives later.",
    }


@app.post("/feedback/dismiss", tags=["Phase 5: Feedback Loop"])
async def feedback_dismiss(req: DismissFeedbackRequest):
    """
    Phase 5.4: 'Dismiss' Feedback
    Records notification dismissal, enters cooldown period to prevent customer fatigue.
    """
    household = await db.get_household(req.household_id)
    if not household:
        raise HTTPException(status_code=404, detail=f"Household {req.household_id} not found.")

    now = datetime.now(timezone.utc)
    await db.update_household_nudge(req.household_id)

    audit_record = NudgeAuditRecord(
        audit_id=f"AUD-FB-DISMISS-{uuid.uuid4().hex[:8].upper()}",
        household_id=req.household_id,
        sku_id=req.sku_id,
        sku_name=req.sku_id,
        evaluated_at=now,
        reorder_probability=0.0,
        gate_status=NudgeGateStatus.PASSED,
        action_taken="NUDGE_DISMISSED",
        reason=f"Customer dismissed nudge: {req.reason or 'User dismiss action'}",
    )
    await db.record_nudge_audit(audit_record)

    return {
        "status": "SUCCESS",
        "action": "DISMISS",
        "household_id": req.household_id,
        "sku_id": req.sku_id,
        "message": "Nudge dismissed. Household entered cooldown period.",
    }


# ==============================================================================
# 7. DATA INSPECTION & TEST RE-SEEDING
# ==============================================================================

@app.get("/data/inventory", tags=["Data Inspection"])
async def get_inventory():
    batches = await db.get_all_batches()
    skus = await db.get_all_skus()
    sound_stock = {s.sku_id: await db.get_sound_on_hand(s.sku_id) for s in skus}
    return {
        "total_batches": len(batches),
        "sound_on_hand": sound_stock,
        "batches": batches,
    }


@app.get("/data/households", tags=["Data Inspection"])
async def get_households():
    return await db.get_households()


@app.get("/data/skus", tags=["Data Inspection"])
async def get_skus():
    return {
        "skus": await db.get_all_skus(),
        "priors": await db.get_all_priors(),
        "recipe_bom": await db.get_recipe_bom(),
    }


@app.post("/data/reset", tags=["Data Inspection"])
async def reset_seed_data():
    """Re-executes Phase 1 seed DML against PostgreSQL to restore initial clean state."""
    await db.reset_seed_data()
    return {"status": "SUCCESS", "message": "PostgreSQL database re-seeded to initial Phase 1 state."}
