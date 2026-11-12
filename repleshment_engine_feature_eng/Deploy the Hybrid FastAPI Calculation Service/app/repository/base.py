"""
Abstract Asynchronous Repository Interface for Accesco Replenishment Engine.
Decouples domain business logic from the underlying storage engine (PostgreSQL vs In-Memory).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional
from app.models.schemas import (
    SKU, Batch, BatchStatus, Household, CustomerOrder, CatchmentPrior,
    PurchaseOrderProposal, POStatus, NudgeAuditRecord, RecipeBOMItem, SystemConfig,
    HouseholdSkuFeedback
)

class BaseRepository(ABC):
    @abstractmethod
    async def check_connection(self) -> bool:
        pass

    @abstractmethod
    async def get_all_batches(self) -> List[Batch]:
        pass

    @abstractmethod
    async def update_batch_status(self, batch_id: str, new_status: BatchStatus, metric: Optional[float] = None):
        pass

    @abstractmethod
    async def get_sound_on_hand(self, sku_id: str) -> float:
        pass

    @abstractmethod
    async def get_sku(self, sku_id: str) -> Optional[SKU]:
        pass

    @abstractmethod
    async def get_all_skus(self) -> List[SKU]:
        pass

    @abstractmethod
    async def get_recipe_bom(self) -> List[RecipeBOMItem]:
        pass

    @abstractmethod
    async def get_households(self) -> List[Household]:
        pass

    @abstractmethod
    async def get_household(self, household_id: str) -> Optional[Household]:
        pass

    @abstractmethod
    async def update_household_nudge(self, household_id: str):
        pass

    @abstractmethod
    async def get_prior(self, sku_id: str) -> Optional[CatchmentPrior]:
        pass

    @abstractmethod
    async def get_all_priors(self) -> Dict[str, CatchmentPrior]:
        pass

    @abstractmethod
    async def get_household_orders_for_sku(self, household_id: str, sku_id: str) -> List[CustomerOrder]:
        pass

    @abstractmethod
    async def get_all_household_sku_pairs(self) -> List[tuple]:
        pass

    @abstractmethod
    async def get_calendar_factors(self) -> Dict[str, Dict[str, float]]:
        pass

    @abstractmethod
    async def get_kitchen_capacities(self) -> Dict[str, float]:
        pass

    @abstractmethod
    async def get_system_config(self) -> SystemConfig:
        pass

    @abstractmethod
    async def set_kill_switch(self, active: bool):
        pass

    @abstractmethod
    async def set_bottom_up_demand(self, eh_map: Dict[str, float]):
        pass

    @abstractmethod
    async def get_bottom_up_demand(self) -> Dict[str, float]:
        pass

    @abstractmethod
    async def save_po_proposal(self, proposal: PurchaseOrderProposal):
        pass

    @abstractmethod
    async def get_po_proposal(self, po_id: str) -> Optional[PurchaseOrderProposal]:
        pass

    @abstractmethod
    async def get_all_po_proposals(self) -> List[PurchaseOrderProposal]:
        pass

    @abstractmethod
    async def update_po_status(
        self,
        po_id: str,
        status: POStatus,
        reviewed_by: str,
        override_reason: Optional[str] = None,
        overridden_items: Optional[Dict[str, int]] = None
    ) -> Optional[PurchaseOrderProposal]:
        pass

    @abstractmethod
    async def record_nudge_audit(self, audit: NudgeAuditRecord):
        pass

    @abstractmethod
    async def get_nudge_audits(self, limit: int = 50) -> List[NudgeAuditRecord]:
        pass

    @abstractmethod
    async def record_delivery_event(
        self,
        event_id: str,
        household_id: str,
        sku_id: str,
        quantity: float,
        delivered_at: datetime,
        feedback_status: str = "NORMAL",
    ) -> CustomerOrder:
        pass

    @abstractmethod
    async def get_household_sku_feedback(self, household_id: str, sku_id: str) -> Optional[HouseholdSkuFeedback]:
        pass

    @abstractmethod
    async def save_household_sku_feedback(self, feedback: HouseholdSkuFeedback):
        pass

    @abstractmethod
    async def reset_seed_data(self):
        pass
