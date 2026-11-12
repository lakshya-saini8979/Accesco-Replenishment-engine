"""
PostgreSQL Asynchronous Repository Implementation (SQLAlchemy Async + asyncpg).
Reads and writes all operational, prior, batch, household, audit, and PO state
directly against the local/Azure PostgreSQL database.
"""

import json
import os
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.schemas import (
    Batch,
    BatchStatus,
    CatchmentPrior,
    CustomerOrder,
    Household,
    HouseholdSkuFeedback,
    NudgeAuditRecord,
    NudgeGateStatus,
    POStatus,
    ProcurementAlert,
    ProductCategory,
    PurchaseOrderItem,
    PurchaseOrderProposal,
    RecipeBOMItem,
    SKU,
    SystemConfig,
)
from app.repository.base import BaseRepository

load_dotenv()


class PostgresRepository(BaseRepository):
    def __init__(self, database_url: Optional[str] = None, use_null_pool: Optional[bool] = None):
        self.database_url = database_url or os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://accesco:accesco_local_pass@localhost:5432/accesco_local",
        )
        if use_null_pool is None:
            use_null_pool = os.getenv("PG_NULLPOOL", "false").lower() == "true"
        self._use_null_pool = use_null_pool
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._engine_loop_id: Optional[int] = None

    def _get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        import asyncio
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = None

        if self._engine is None or (self._use_null_pool and self._engine_loop_id != loop_id):
            kwargs = {"echo": False, "pool_pre_ping": True}
            if self._use_null_pool:
                kwargs["poolclass"] = NullPool
            else:
                kwargs["pool_size"] = 10
                kwargs["max_overflow"] = 20
            self._engine = create_async_engine(self.database_url, **kwargs)
            self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
            self._engine_loop_id = loop_id
        return self._session_factory

    async def dispose(self):
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            self._engine_loop_id = None

    async def check_connection(self) -> bool:
        try:
            factory = self._get_session_factory()
            async with factory() as session:
                res = await session.execute(text("SELECT 1"))
                return res.scalar() == 1
        except Exception:
            return False

    async def get_all_batches(self) -> List[Batch]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT batch_id, sku_id, received_qty, current_qty, is_fresh_produce,
                               manufacture_date, harvest_date, expiry_date, location_id,
                               status, hours_since_harvest, life_fraction_l
                        FROM wms_inventory_batch
                        ORDER BY batch_id
                        """
                    )
                )
            ).mappings().all()
            batches = []
            for r in rows:
                ref_dt = r["harvest_date"] if r["is_fresh_produce"] else r["manufacture_date"]
                if ref_dt is None:
                    ref_dt = r["harvest_date"] or r["manufacture_date"] or datetime.now(timezone.utc)
                batches.append(
                    Batch(
                        batch_id=r["batch_id"],
                        sku_id=r["sku_id"],
                        quantity=float(r["current_qty"]),
                        received_qty=float(r["received_qty"]),
                        is_fresh_produce=bool(r["is_fresh_produce"]),
                        manufacture_or_harvest_date=ref_dt,
                        expiry_date=r["expiry_date"],
                        location_id=r["location_id"] or "STORE-RAJ-01",
                        status=BatchStatus(r["status"]),
                        hours_since_harvest=float(r["hours_since_harvest"]) if r["hours_since_harvest"] is not None else None,
                        life_fraction_L=float(r["life_fraction_l"]) if r["life_fraction_l"] is not None else None,
                    )
                )
            return batches

    async def update_batch_status(self, batch_id: str, new_status: BatchStatus, metric: Optional[float] = None):
        factory = self._get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT is_fresh_produce FROM wms_inventory_batch WHERE batch_id = :batch_id"),
                    {"batch_id": batch_id},
                )
            ).mappings().first()
            if not row:
                return
            if row["is_fresh_produce"]:
                await session.execute(
                    text(
                        """
                        UPDATE wms_inventory_batch
                        SET status = :status, hours_since_harvest = :metric
                        WHERE batch_id = :batch_id
                        """
                    ),
                    {"status": new_status.value, "metric": metric, "batch_id": batch_id},
                )
            else:
                await session.execute(
                    text(
                        """
                        UPDATE wms_inventory_batch
                        SET status = :status, life_fraction_l = :metric
                        WHERE batch_id = :batch_id
                        """
                    ),
                    {"status": new_status.value, "metric": metric, "batch_id": batch_id},
                )
            await session.commit()

    async def get_sound_on_hand(self, sku_id: str) -> float:
        factory = self._get_session_factory()
        async with factory() as session:
            res = await session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(current_qty), 0)
                    FROM wms_inventory_batch
                    WHERE sku_id = :sku_id AND status = 'SELLABLE'
                    """
                ),
                {"sku_id": sku_id},
            )
            return float(res.scalar() or 0.0)

    def _row_to_sku(self, r) -> SKU:
        return SKU(
            sku_id=r["sku_id"],
            name=r["sku_name"],
            category=ProductCategory(r["category"]),
            is_fresh=bool(r["is_fresh"]),
            unit=r["unit"],
            buffer_percentage=float(r["buffer_b"]),
            case_pack_size=max(1, int(round(float(r["case_pack"])))),
            moq=max(1, int(round(float(r["moq"])))),
            trailing_weekly_avg=float(r["seed_daily_mean"]),
            typical_order_qty=float(r["typical_order_qty"]),
            doorstep_rejection_factor_R=float(r["doorstep_rejection_factor_r"]),
        )

    async def get_sku(self, sku_id: str) -> Optional[SKU]:
        factory = self._get_session_factory()
        async with factory() as session:
            r = (
                await session.execute(
                    text("SELECT * FROM cdao_replenishment_priors WHERE sku_id = :sku_id"),
                    {"sku_id": sku_id},
                )
            ).mappings().first()
            return self._row_to_sku(r) if r else None

    async def get_all_skus(self) -> List[SKU]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT * FROM cdao_replenishment_priors ORDER BY sku_id"))
            ).mappings().all()
            return [self._row_to_sku(r) for r in rows]

    async def get_recipe_bom(self) -> List[RecipeBOMItem]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT * FROM oms_recipe_bom ORDER BY dish_sku_id, ingredient_sku_id"))
            ).mappings().all()
            return [
                RecipeBOMItem(
                    dish_sku_id=r["dish_sku_id"],
                    ingredient_sku_id=r["ingredient_sku_id"],
                    quantity_base_unit=float(r["quantity_base_unit"]),
                    unit_of_measure=r["unit_of_measure"],
                )
                for r in rows
            ]

    def _row_to_household(self, r) -> Household:
        return Household(
            household_id=r["household_id"],
            cluster_id=r["cluster_id"],
            serves_multiplier=float(r["serves_multiplier"]),
            is_active=bool(r["is_active"]),
            days_since_active=int(r["days_since_active"]),
            is_opted_out=bool(r["is_opted_out"]),
            in_active_cart=bool(r["in_active_cart"]),
            last_nudge_timestamp=r["last_nudge_timestamp"],
            daily_nudge_count=int(r["daily_nudge_count"]),
        )

    async def get_households(self) -> List[Household]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT * FROM oms_households ORDER BY household_id"))
            ).mappings().all()
            return [self._row_to_household(r) for r in rows]

    async def get_household(self, household_id: str) -> Optional[Household]:
        factory = self._get_session_factory()
        async with factory() as session:
            r = (
                await session.execute(
                    text("SELECT * FROM oms_households WHERE household_id = :hid"),
                    {"hid": household_id},
                )
            ).mappings().first()
            return self._row_to_household(r) if r else None

    async def update_household_nudge(self, household_id: str):
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE oms_households
                    SET last_nudge_timestamp = NOW(),
                        daily_nudge_count = daily_nudge_count + 1
                    WHERE household_id = :hid
                    """
                ),
                {"hid": household_id},
            )
            await session.commit()

    def _row_to_prior(self, r) -> CatchmentPrior:
        return CatchmentPrior(
            sku_id=r["sku_id"],
            cluster_id="CLUSTER_CENTRAL_01",
            rho_daily_rate=float(r["catchment_rho_1p"]),
            rho_2p_daily_rate=float(r["catchment_rho_2p"]),
            prior_k_days=float(r["prior_k_days"]),
            prior_interval_mean=float(r["prior_interval_mean"]),
            prior_interval_std=float(r["prior_interval_std"]),
        )

    async def get_prior(self, sku_id: str) -> Optional[CatchmentPrior]:
        factory = self._get_session_factory()
        async with factory() as session:
            r = (
                await session.execute(
                    text("SELECT * FROM cdao_replenishment_priors WHERE sku_id = :sku_id"),
                    {"sku_id": sku_id},
                )
            ).mappings().first()
            return self._row_to_prior(r) if r else None

    async def get_all_priors(self) -> Dict[str, CatchmentPrior]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT * FROM cdao_replenishment_priors ORDER BY sku_id"))
            ).mappings().all()
            return {r["sku_id"]: self._row_to_prior(r) for r in rows}

    async def get_household_orders_for_sku(self, household_id: str, sku_id: str) -> List[CustomerOrder]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT event_id, household_id, sku_id, purchase_date, delivered_at, quantity, feedback_status
                        FROM oms_customer_purchase_log
                        WHERE household_id = :hid AND sku_id = :sid
                        ORDER BY COALESCE(delivered_at, purchase_date) ASC
                        """
                    ),
                    {"hid": household_id, "sid": sku_id},
                )
            ).mappings().all()
            return [
                CustomerOrder(
                    order_id=r["event_id"],
                    household_id=r["household_id"],
                    sku_id=r["sku_id"],
                    order_date=r["purchase_date"].date() if isinstance(r["purchase_date"], datetime) else r["purchase_date"],
                    delivered_at=r["delivered_at"] or r["purchase_date"],
                    quantity=float(r["quantity"]),
                    feedback_status=r["feedback_status"],
                )
                for r in rows
            ]

    async def get_all_household_sku_pairs(self) -> List[tuple]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT household_id, sku_id
                        FROM oms_customer_purchase_log
                        ORDER BY household_id, sku_id
                        """
                    )
                )
            ).all()
            return [(r[0], r[1]) for r in rows]

    async def get_calendar_factors(self) -> Dict[str, Dict[str, float]]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT factor_type, factor_key, multiplier FROM cdao_calendar_factors"))
            ).mappings().all()
            factors: Dict[str, Dict[str, float]] = {}
            for r in rows:
                ft = r["factor_type"]
                factors.setdefault(ft, {})[r["factor_key"]] = float(r["multiplier"])
            return factors

    async def get_kitchen_capacities(self) -> Dict[str, float]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT category, daily_cap_kg FROM cdao_kitchen_capacity"))
            ).mappings().all()
            return {r["category"]: float(r["daily_cap_kg"]) for r in rows}

    async def get_system_config(self) -> SystemConfig:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT config_key, config_value FROM cdao_system_config"))
            ).mappings().all()
            cfg = {r["config_key"]: r["config_value"] for r in rows}
            return SystemConfig(
                emergency_kill_switch=cfg.get("emergency_kill_switch", "false").lower() == "true",
                quiet_hours_start=int(cfg.get("quiet_hours_start", "21")),
                quiet_hours_end=int(cfg.get("quiet_hours_end", "8")),
                cooldown_days=int(cfg.get("cooldown_days", "3")),
                daily_frequency_cap=int(cfg.get("daily_frequency_cap", "1")),
            )

    async def set_kill_switch(self, active: bool):
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO cdao_system_config (config_key, config_value, updated_at)
                    VALUES ('emergency_kill_switch', :val, NOW())
                    ON CONFLICT (config_key) DO UPDATE SET config_value = :val, updated_at = NOW()
                    """
                ),
                {"val": "true" if active else "false"},
            )
            await session.commit()

    async def set_bottom_up_demand(self, eh_map: Dict[str, float]):
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO cdao_system_config (config_key, config_value, updated_at)
                    VALUES ('latest_bottom_up_eh', :val, NOW())
                    ON CONFLICT (config_key) DO UPDATE SET config_value = :val, updated_at = NOW()
                    """
                ),
                {"val": json.dumps(eh_map)},
            )
            await session.commit()

    async def get_bottom_up_demand(self) -> Dict[str, float]:
        factory = self._get_session_factory()
        async with factory() as session:
            res = await session.execute(
                text("SELECT config_value FROM cdao_system_config WHERE config_key = 'latest_bottom_up_eh'")
            )
            raw = res.scalar()
            return json.loads(raw) if raw else {}

    def _row_to_po(self, r) -> PurchaseOrderProposal:
        items_raw = r["items_json"] if isinstance(r["items_json"], list) else json.loads(r["items_json"])
        alerts_raw = (
            r["procurement_alerts_json"]
            if isinstance(r["procurement_alerts_json"], list)
            else json.loads(r["procurement_alerts_json"] or "[]")
        )
        items = [PurchaseOrderItem(**it) for it in items_raw]
        alerts = [ProcurementAlert(**al) for al in alerts_raw]

        # Compute category caps / overflow snapshots if stored or derive from items
        cat_overflow: Dict[str, float] = {}
        for it in items:
            if it.category != ProductCategory.FMCG:
                c = it.category.value
                cat_overflow[c] = round(cat_overflow.get(c, 0.0) + it.kitchen_overflow_kg, 2)

        return PurchaseOrderProposal(
            po_id=r["po_id"],
            created_at=r["created_at"],
            status=POStatus(r["status"]),
            target_delivery_date=r["target_delivery_date"],
            festival_factor_F=float(r["festival_factor_f"]),
            month_salary_factor_M=float(r["month_salary_factor_m"]),
            doorstep_rejection_factor_R=float(r["doorstep_rejection_factor_r"]),
            kitchen_capacity_cap_kg=float(r["kitchen_capacity_cap_kg"]),
            category_overflow_kg=cat_overflow,
            total_fresh_overflow_kg=float(r["total_fresh_overflow_kg"]),
            overflow_cap_exceeded=bool(r["overflow_cap_exceeded"]),
            buffer_reduction_iterations=int(r["buffer_reduction_iterations"]),
            total_proposed_units=int(r["total_proposed_units"]),
            procurement_alerts=alerts,
            items=items,
            reviewed_by=r["reviewed_by"],
            reviewed_at=r["reviewed_at"],
            override_reason=r["override_reason"],
        )

    async def save_po_proposal(self, proposal: PurchaseOrderProposal):
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO cdao_po_proposals (
                        po_id, created_at, status, target_delivery_date,
                        festival_factor_f, month_salary_factor_m, doorstep_rejection_factor_r,
                        kitchen_capacity_cap_kg, total_fresh_overflow_kg, overflow_cap_exceeded,
                        buffer_reduction_iterations, total_proposed_units,
                        procurement_alerts_json, items_json,
                        reviewed_by, reviewed_at, override_reason
                    ) VALUES (
                        :po_id, :created_at, :status, :target_delivery_date,
                        :f, :m, :r, :k_cap, :tot_ov, :ov_exc, :iters, :tot_units,
                        CAST(:alerts_json AS jsonb), CAST(:items_json AS jsonb),
                        :rev_by, :rev_at, :ov_reason
                    )
                    """
                ),
                {
                    "po_id": proposal.po_id,
                    "created_at": proposal.created_at,
                    "status": proposal.status.value,
                    "target_delivery_date": proposal.target_delivery_date,
                    "f": proposal.festival_factor_F,
                    "m": proposal.month_salary_factor_M,
                    "r": proposal.doorstep_rejection_factor_R,
                    "k_cap": proposal.kitchen_capacity_cap_kg,
                    "tot_ov": proposal.total_fresh_overflow_kg,
                    "ov_exc": proposal.overflow_cap_exceeded,
                    "iters": proposal.buffer_reduction_iterations,
                    "tot_units": proposal.total_proposed_units,
                    "alerts_json": json.dumps([a.model_dump() for a in proposal.procurement_alerts]),
                    "items_json": json.dumps([i.model_dump() for i in proposal.items]),
                    "rev_by": proposal.reviewed_by,
                    "rev_at": proposal.reviewed_at,
                    "ov_reason": proposal.override_reason,
                },
            )
            await session.commit()

    async def get_po_proposal(self, po_id: str) -> Optional[PurchaseOrderProposal]:
        factory = self._get_session_factory()
        async with factory() as session:
            r = (
                await session.execute(
                    text("SELECT * FROM cdao_po_proposals WHERE po_id = :po_id"),
                    {"po_id": po_id},
                )
            ).mappings().first()
            return self._row_to_po(r) if r else None

    async def get_all_po_proposals(self) -> List[PurchaseOrderProposal]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(text("SELECT * FROM cdao_po_proposals ORDER BY created_at DESC"))
            ).mappings().all()
            return [self._row_to_po(r) for r in rows]

    async def update_po_status(
        self,
        po_id: str,
        status: POStatus,
        reviewed_by: str,
        override_reason: Optional[str] = None,
        overridden_items: Optional[Dict[str, int]] = None,
    ) -> Optional[PurchaseOrderProposal]:
        po = await self.get_po_proposal(po_id)
        if not po:
            return None

        now = datetime.now(timezone.utc)
        po.status = status
        po.reviewed_by = reviewed_by
        po.reviewed_at = now
        po.override_reason = override_reason

        if overridden_items:
            for item in po.items:
                item.approved_quantity = overridden_items.get(item.sku_id, item.proposed_quantity)
        elif status == POStatus.APPROVED:
            for item in po.items:
                item.approved_quantity = item.proposed_quantity
        elif status == POStatus.REJECTED:
            for item in po.items:
                item.approved_quantity = 0

        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE cdao_po_proposals
                    SET status = :status,
                        reviewed_by = :reviewed_by,
                        reviewed_at = :reviewed_at,
                        override_reason = :override_reason,
                        items_json = CAST(:items_json AS jsonb)
                    WHERE po_id = :po_id
                    """
                ),
                {
                    "status": status.value,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": now,
                    "override_reason": override_reason,
                    "items_json": json.dumps([i.model_dump() for i in po.items]),
                    "po_id": po_id,
                },
            )
            await session.commit()
        return po

    async def record_nudge_audit(self, audit: NudgeAuditRecord):
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO cdao_nudge_audit_log
                    (audit_id, household_id, sku_id, sku_name, evaluated_at, reorder_probability, gate_status, action_taken, reason)
                    VALUES (:audit_id, :hid, :sid, :sname, :eval_at, :prob, :gate, :action, :reason)
                    """
                ),
                {
                    "audit_id": audit.audit_id,
                    "hid": audit.household_id,
                    "sid": audit.sku_id,
                    "sname": audit.sku_name,
                    "eval_at": audit.evaluated_at,
                    "prob": audit.reorder_probability,
                    "gate": audit.gate_status.value,
                    "action": audit.action_taken,
                    "reason": audit.reason,
                },
            )
            await session.commit()

    async def get_nudge_audits(self, limit: int = 50) -> List[NudgeAuditRecord]:
        factory = self._get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT audit_id, household_id, sku_id, sku_name, evaluated_at,
                               reorder_probability, gate_status, action_taken, reason
                        FROM cdao_nudge_audit_log
                        ORDER BY evaluated_at DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": limit},
                )
            ).mappings().all()
            return [
                NudgeAuditRecord(
                    audit_id=r["audit_id"],
                    household_id=r["household_id"],
                    sku_id=r["sku_id"],
                    sku_name=r["sku_name"],
                    evaluated_at=r["evaluated_at"],
                    reorder_probability=float(r["reorder_probability"]),
                    gate_status=NudgeGateStatus(r["gate_status"]),
                    action_taken=r["action_taken"],
                    reason=r["reason"],
                )
                for r in reversed(rows)
            ]

    async def record_delivery_event(
        self,
        event_id: str,
        household_id: str,
        sku_id: str,
        quantity: float,
        delivered_at: datetime,
        feedback_status: str = "NORMAL",
    ) -> CustomerOrder:
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO oms_customer_purchase_log (event_id, household_id, sku_id, quantity, purchase_date, delivered_at, feedback_status)
                    VALUES (:eid, :hid, :sid, :qty, :pdate, :deliv_at, :fb_status)
                    ON CONFLICT (event_id) DO UPDATE
                    SET delivered_at = EXCLUDED.delivered_at,
                        quantity = EXCLUDED.quantity,
                        feedback_status = EXCLUDED.feedback_status
                    """
                ),
                {
                    "eid": event_id,
                    "hid": household_id,
                    "sid": sku_id,
                    "qty": quantity,
                    "pdate": delivered_at,
                    "deliv_at": delivered_at,
                    "fb_status": feedback_status,
                },
            )
            # Receiving doorstep delivery also verifies household active status
            await session.execute(
                text(
                    """
                    UPDATE oms_households
                    SET is_active = TRUE,
                        days_since_active = 0,
                        in_active_cart = FALSE
                    WHERE household_id = :hid
                    """
                ),
                {"hid": household_id},
            )
            await session.commit()
            return CustomerOrder(
                order_id=event_id,
                household_id=household_id,
                sku_id=sku_id,
                order_date=delivered_at.date() if isinstance(delivered_at, datetime) else delivered_at,
                delivered_at=delivered_at,
                quantity=quantity,
                feedback_status=feedback_status,
            )

    async def get_household_sku_feedback(self, household_id: str, sku_id: str) -> Optional[HouseholdSkuFeedback]:
        factory = self._get_session_factory()
        async with factory() as session:
            r = (
                await session.execute(
                    text(
                        """
                        SELECT household_id, sku_id, feedback_type, rate_multiplier,
                               virtual_stock_reset_at, last_feedback_at, notes
                        FROM cdao_household_sku_feedback
                        WHERE household_id = :hid AND sku_id = :sid
                        """
                    ),
                    {"hid": household_id, "sid": sku_id},
                )
            ).mappings().first()
            if not r:
                return None
            return HouseholdSkuFeedback(
                household_id=r["household_id"],
                sku_id=r["sku_id"],
                feedback_type=r["feedback_type"],
                rate_multiplier=float(r["rate_multiplier"]),
                virtual_stock_reset_at=r["virtual_stock_reset_at"],
                last_feedback_at=r["last_feedback_at"],
                notes=r["notes"],
            )

    async def save_household_sku_feedback(self, feedback: HouseholdSkuFeedback):
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO cdao_household_sku_feedback
                    (household_id, sku_id, feedback_type, rate_multiplier, virtual_stock_reset_at, last_feedback_at, notes)
                    VALUES (:hid, :sid, :ftype, :rmult, :vreset, :fb_at, :notes)
                    ON CONFLICT (household_id, sku_id) DO UPDATE
                    SET feedback_type = EXCLUDED.feedback_type,
                        rate_multiplier = EXCLUDED.rate_multiplier,
                        virtual_stock_reset_at = EXCLUDED.virtual_stock_reset_at,
                        last_feedback_at = EXCLUDED.last_feedback_at,
                        notes = EXCLUDED.notes
                    """
                ),
                {
                    "hid": feedback.household_id,
                    "sid": feedback.sku_id,
                    "ftype": feedback.feedback_type,
                    "rmult": feedback.rate_multiplier,
                    "vreset": feedback.virtual_stock_reset_at,
                    "fb_at": feedback.last_feedback_at,
                    "notes": feedback.notes,
                },
            )
            await session.commit()

    async def reset_seed_data(self):
        sql_dir = Path(__file__).resolve().parents[2] / "sql"
        seed_sql = (sql_dir / "02_seed_dml.sql").read_text(encoding="utf-8")
        # Execute statements inside a transaction via raw asyncpg connection for multi-statement SQL
        factory = self._get_session_factory()
        async with factory() as session:
            raw_conn = await session.connection()
            asyncpg_conn = await raw_conn.get_raw_connection()
            await asyncpg_conn.driver_connection.execute(seed_sql)
            await session.commit()
