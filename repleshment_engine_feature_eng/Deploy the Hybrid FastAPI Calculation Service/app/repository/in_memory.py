"""
Thread-Safe Asynchronous In-Memory Repository Implementation.
Kept solely as an interface-compatible reference/fallback; active production wiring uses PostgresRepository.
"""

import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from app.models.schemas import (
    SKU, Batch, BatchStatus, ProductCategory, Household, CustomerOrder,
    CatchmentPrior, PurchaseOrderProposal, POStatus, NudgeAuditRecord,
    RecipeBOMItem, SystemConfig, HouseholdSkuFeedback
)
from app.repository.base import BaseRepository


class InMemoryRepository(BaseRepository):
    def __init__(self):
        self._lock = threading.RLock()
        self.skus: Dict[str, SKU] = {}
        self.batches: Dict[str, Batch] = {}
        self.households: Dict[str, Household] = {}
        self.orders: List[CustomerOrder] = []
        self.priors: Dict[str, CatchmentPrior] = {}
        self.bom: List[RecipeBOMItem] = []
        self.po_proposals: Dict[str, PurchaseOrderProposal] = {}
        self.nudge_audits: List[NudgeAuditRecord] = []
        self.kitchen_capacities: Dict[str, float] = {
            "leafy_greens": 8.0,
            "dairy": 12.0,
            "base_veg": 20.0,
            "total_fresh": 35.0,
        }
        self.calendar_factors: Dict[str, Dict[str, float]] = {
            "WEEKDAY": {"Mon": 0.95, "Tue": 0.95, "Wed": 0.97, "Thu": 0.98, "Fri": 1.03, "Sat": 1.10, "Sun": 1.02},
            "MONTH_CYCLE": {"Days_1_7": 1.08, "Days_8_22": 1.00, "Days_23_31": 0.92},
        }
        self.system_config = SystemConfig()
        self.latest_bottom_up_Eh: Dict[str, float] = {}

    async def check_connection(self) -> bool:
        return True

    async def get_all_batches(self) -> List[Batch]:
        with self._lock:
            return list(self.batches.values())

    async def update_batch_status(self, batch_id: str, new_status: BatchStatus, metric: Optional[float] = None):
        with self._lock:
            if batch_id in self.batches:
                self.batches[batch_id].status = new_status
                if metric is not None:
                    sku = self.skus.get(self.batches[batch_id].sku_id)
                    if sku and sku.is_fresh:
                        self.batches[batch_id].hours_since_harvest = metric
                    else:
                        self.batches[batch_id].life_fraction_L = metric

    async def get_sound_on_hand(self, sku_id: str) -> float:
        with self._lock:
            return sum(
                b.quantity for b in self.batches.values()
                if b.sku_id == sku_id and b.status == BatchStatus.SELLABLE
            )

    async def get_sku(self, sku_id: str) -> Optional[SKU]:
        with self._lock:
            return self.skus.get(sku_id)

    async def get_all_skus(self) -> List[SKU]:
        with self._lock:
            return list(self.skus.values())

    async def get_recipe_bom(self) -> List[RecipeBOMItem]:
        with self._lock:
            return list(self.bom)

    async def get_households(self) -> List[Household]:
        with self._lock:
            return list(self.households.values())

    async def get_household(self, household_id: str) -> Optional[Household]:
        with self._lock:
            return self.households.get(household_id)

    async def update_household_nudge(self, household_id: str):
        with self._lock:
            if household_id in self.households:
                h = self.households[household_id]
                h.last_nudge_timestamp = datetime.now()
                h.daily_nudge_count += 1

    async def get_prior(self, sku_id: str) -> Optional[CatchmentPrior]:
        with self._lock:
            return self.priors.get(sku_id)

    async def get_all_priors(self) -> Dict[str, CatchmentPrior]:
        with self._lock:
            return dict(self.priors)

    async def get_household_orders_for_sku(self, household_id: str, sku_id: str) -> List[CustomerOrder]:
        with self._lock:
            orders = [o for o in self.orders if o.household_id == household_id and o.sku_id == sku_id]
            return sorted(orders, key=lambda x: x.order_date)

    async def get_all_household_sku_pairs(self) -> List[tuple]:
        with self._lock:
            return sorted(set((o.household_id, o.sku_id) for o in self.orders))

    async def get_calendar_factors(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            return dict(self.calendar_factors)

    async def get_kitchen_capacities(self) -> Dict[str, float]:
        with self._lock:
            return dict(self.kitchen_capacities)

    async def get_system_config(self) -> SystemConfig:
        with self._lock:
            return self.system_config

    async def set_kill_switch(self, active: bool):
        with self._lock:
            self.system_config.emergency_kill_switch = active

    async def set_bottom_up_demand(self, eh_map: Dict[str, float]):
        with self._lock:
            self.latest_bottom_up_Eh = dict(eh_map)

    async def get_bottom_up_demand(self) -> Dict[str, float]:
        with self._lock:
            return dict(self.latest_bottom_up_Eh)

    async def save_po_proposal(self, proposal: PurchaseOrderProposal):
        with self._lock:
            self.po_proposals[proposal.po_id] = proposal

    async def get_po_proposal(self, po_id: str) -> Optional[PurchaseOrderProposal]:
        with self._lock:
            return self.po_proposals.get(po_id)

    async def get_all_po_proposals(self) -> List[PurchaseOrderProposal]:
        with self._lock:
            return list(self.po_proposals.values())

    async def update_po_status(
        self,
        po_id: str,
        status: POStatus,
        reviewed_by: str,
        override_reason: Optional[str] = None,
        overridden_items: Optional[Dict[str, int]] = None
    ) -> Optional[PurchaseOrderProposal]:
        with self._lock:
            po = self.po_proposals.get(po_id)
            if not po:
                return None
            po.status = status
            po.reviewed_by = reviewed_by
            po.reviewed_at = datetime.now()
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
            return po

    async def record_nudge_audit(self, audit: NudgeAuditRecord):
        with self._lock:
            self.nudge_audits.append(audit)

    async def get_nudge_audits(self, limit: int = 50) -> List[NudgeAuditRecord]:
        with self._lock:
            return self.nudge_audits[-limit:]

    async def record_delivery_event(
        self,
        event_id: str,
        household_id: str,
        sku_id: str,
        quantity: float,
        delivered_at: datetime,
        feedback_status: str = "NORMAL",
    ) -> CustomerOrder:
        with self._lock:
            order = CustomerOrder(
                order_id=event_id,
                household_id=household_id,
                sku_id=sku_id,
                order_date=delivered_at.date() if isinstance(delivered_at, datetime) else delivered_at,
                delivered_at=delivered_at,
                quantity=quantity,
                feedback_status=feedback_status,
            )
            self.orders.append(order)
            if household_id in self.households:
                self.households[household_id].is_active = True
                self.households[household_id].days_since_active = 0
                self.households[household_id].in_active_cart = False
            return order

    async def get_household_sku_feedback(self, household_id: str, sku_id: str) -> Optional[HouseholdSkuFeedback]:
        with self._lock:
            if not hasattr(self, "_feedbacks"):
                self._feedbacks = {}
            return self._feedbacks.get((household_id, sku_id))

    async def save_household_sku_feedback(self, feedback: HouseholdSkuFeedback):
        with self._lock:
            if not hasattr(self, "_feedbacks"):
                self._feedbacks = {}
            self._feedbacks[(feedback.household_id, feedback.sku_id)] = feedback

    async def reset_seed_data(self):
        with self._lock:
            self.po_proposals.clear()
            self.nudge_audits.clear()
            self.latest_bottom_up_Eh.clear()
