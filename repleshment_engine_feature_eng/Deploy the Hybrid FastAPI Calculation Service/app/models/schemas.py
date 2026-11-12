"""
Data models and schemas for Accesco Hybrid Replenishment Engine.
Includes CDAO governance, PO lifecycles, procurement alerts, and customer eligibility gates.
"""

from datetime import datetime, date
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

# --- Enums ---
class BatchStatus(str, Enum):
    SELLABLE = "SELLABLE"
    ROUTE_TO_KITCHEN = "ROUTE_TO_KITCHEN"
    COMPOST = "COMPOST"

class ProductCategory(str, Enum):
    LEAFY_GREENS = "leafy_greens"
    DAIRY = "dairy"
    BASE_VEG = "base_veg"
    FMCG = "fmcg"

class POStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    OVERRIDDEN = "OVERRIDDEN"

class NudgeGateStatus(str, Enum):
    PASSED = "PASSED"
    BLOCKED_OUT_OF_STOCK = "BLOCKED_OUT_OF_STOCK"
    BLOCKED_OPT_OUT = "BLOCKED_OPT_OUT"
    BLOCKED_DORMANT = "BLOCKED_DORMANT"
    BLOCKED_IN_CART = "BLOCKED_IN_CART"
    BLOCKED_QUIET_HOURS = "BLOCKED_QUIET_HOURS"
    BLOCKED_COOLDOWN = "BLOCKED_COOLDOWN"
    BLOCKED_FREQ_CAP = "BLOCKED_FREQ_CAP"
    BLOCKED_KILL_SWITCH = "BLOCKED_KILL_SWITCH"

# --- Domain Entities ---
class SKU(BaseModel):
    sku_id: str
    name: str
    category: ProductCategory
    is_fresh: bool
    unit: str
    buffer_percentage: float
    case_pack_size: int = 1
    moq: int = 1
    trailing_weekly_avg: float = 0.0 # A(i, w) / seed_daily_mean
    typical_order_qty: float = 1.0
    doorstep_rejection_factor_R: float = 1.00 # Doorstep Check R(i)

class Batch(BaseModel):
    batch_id: str
    sku_id: str
    quantity: float
    received_qty: float = 0.0
    is_fresh_produce: bool = True
    manufacture_or_harvest_date: datetime
    expiry_date: Optional[datetime] = None
    location_id: str = "STORE-RAJ-01"
    status: BatchStatus = BatchStatus.SELLABLE
    hours_since_harvest: Optional[float] = None
    life_fraction_L: Optional[float] = None
    fssai_days_remaining: Optional[int] = None

class RecipeBOMItem(BaseModel):
    dish_sku_id: str
    ingredient_sku_id: str
    quantity_base_unit: float
    unit_of_measure: str

class Household(BaseModel):
    household_id: str
    cluster_id: str = "CLUSTER_CENTRAL_01"
    serves_multiplier: float = 1.0 # 1.0 for Serves 1, 1.6 for Serves 2
    is_active: bool = True
    days_since_active: int = 0 # Dormancy > 7 days blocks nudge
    is_opted_out: bool = False
    in_active_cart: bool = False
    last_nudge_timestamp: Optional[datetime] = None
    daily_nudge_count: int = 0

class CustomerOrder(BaseModel):
    order_id: str
    household_id: str
    sku_id: str
    order_date: date
    delivered_at: Optional[datetime] = None  # Authoritative d_n start of depletion clock
    quantity: float
    feedback_status: str = "NORMAL"

class HouseholdSkuFeedback(BaseModel):
    household_id: str
    sku_id: str
    feedback_type: str  # REORDER, BOUGHT_ELSEWHERE, STILL_HAVE_PLENTY, DISMISS
    rate_multiplier: float = 1.0000
    virtual_stock_reset_at: Optional[datetime] = None
    last_feedback_at: datetime
    notes: Optional[str] = None

class DeliveryConfirmationEvent(BaseModel):
    household_id: str = Field(..., description="Target customer household identifier")
    sku_id: str = Field(..., description="Replenished product identifier")
    quantity: float = Field(..., gt=0.0, description="Delivered quantity")
    delivered_at: Optional[datetime] = Field(None, description="Doorstep delivery timestamp d_n (defaults to UTC now)")
    event_id: Optional[str] = Field(None, description="Optional idempotency/tracking event ID")
    order_id: Optional[str] = Field(None, description="Optional upstream OMS order ID")

# --- Phase 5: Feedback Request Models ---
class ReorderFeedbackRequest(BaseModel):
    household_id: str
    sku_id: str
    quantity: Optional[float] = Field(None, description="Quantity reordered, defaults to typical order qty")
    notes: Optional[str] = Field(None, description="Optional customer/audit notes")

class BoughtElsewhereFeedbackRequest(BaseModel):
    household_id: str
    sku_id: str
    quantity: Optional[float] = Field(None, description="Quantity replenished from external retailer")
    notes: Optional[str] = Field(None, description="Optional feedback context")

class StillHavePlentyFeedbackRequest(BaseModel):
    household_id: str
    sku_id: str
    reduction_factor: float = Field(0.70, ge=0.10, le=0.95, description="Rate multiplier dampener (default 0.70 = 30% reduction)")
    notes: Optional[str] = Field(None, description="Optional feedback context")

class DismissFeedbackRequest(BaseModel):
    household_id: str
    sku_id: str
    reason: Optional[str] = Field(None, description="Optional dismissal explanation")

class CatchmentPrior(BaseModel):
    sku_id: str
    cluster_id: str = "CLUSTER_CENTRAL_01"
    rho_daily_rate: float # 1p neighborhood rate
    rho_2p_daily_rate: float # 2+p neighborhood rate
    prior_k_days: float # 14 for fresh, 30 for staples
    prior_interval_mean: float
    prior_interval_std: float

class SystemConfig(BaseModel):
    emergency_kill_switch: bool = False
    quiet_hours_start: int = 21
    quiet_hours_end: int = 8
    cooldown_days: int = 3
    daily_frequency_cap: int = 1

# --- Audit & Governance Models ---
class NudgeAuditRecord(BaseModel):
    audit_id: str
    household_id: str
    sku_id: str
    sku_name: str
    evaluated_at: datetime
    reorder_probability: float
    gate_status: NudgeGateStatus
    action_taken: str # "NUDGE_QUEUED", "SUPPRESSED"
    reason: str

class ProcurementAlert(BaseModel):
    sku_id: str
    sku_name: str
    rejection_factor_r: float
    severity: str
    recommended_action: str

class PurchaseOrderItem(BaseModel):
    sku_id: str
    name: str
    category: ProductCategory
    trailing_avg_Aiw: float
    bottom_up_Eh: float
    divergence_pct: float
    base_demand: float
    day_constant_c: float
    month_factor_M: float
    rejection_factor_R: float
    reconciled_demand_D: float
    initial_buffer_percentage: float
    buffer_percentage_applied: float
    order_up_to_S: float
    sound_on_hand: float
    raw_order: float
    case_pack_size: int
    moq: int
    proposed_quantity: int
    approved_quantity: Optional[int] = None
    kitchen_overflow_kg: float

class PurchaseOrderProposal(BaseModel):
    po_id: str
    created_at: datetime
    status: POStatus
    target_delivery_date: date
    festival_factor_F: float
    month_salary_factor_M: float
    doorstep_rejection_factor_R: float
    kitchen_capacity_cap_kg: float
    category_kitchen_caps_kg: Dict[str, float] = {}
    category_overflow_kg: Dict[str, float] = {}
    total_fresh_overflow_kg: float
    overflow_cap_exceeded: bool
    buffer_reduction_iterations: int
    total_proposed_units: int
    procurement_alerts: List[ProcurementAlert] = []
    items: List[PurchaseOrderItem]
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    override_reason: Optional[str] = None

# --- API Response Models ---
class FreshRoutingItem(BaseModel):
    batch_id: str
    sku_id: str
    name: str
    category: str
    quantity: float
    age_metric: str
    status: BatchStatus
    action_notes: str

class FreshRoutingReport(BaseModel):
    timestamp: datetime
    total_batches_checked: int
    sellable_count: int
    routed_to_kitchen_count: int
    compost_count: int
    routed_to_kitchen_items: List[FreshRoutingItem]
    compost_items: List[FreshRoutingItem]
    sellable_items: List[FreshRoutingItem]

class HceNudgeItem(BaseModel):
    household_id: str
    sku_id: str
    sku_name: str
    purchases_count: int
    shrunk_rate_r_hi: float
    days_since_last_purchase: int
    expected_low_stock_days: float
    reorder_probability: float
    prediction_engine: str = "PATH_A_BAYESIAN_SHRINKAGE"  # PATH_A_BAYESIAN_SHRINKAGE vs PATH_B_DATABRICKS_XGBOOST_AFT
    stock_level_percentage: float = 0.0
    nudge_eligible: bool
    gate_status: NudgeGateStatus
    reason: str

# --- Phase 4: Frontend Wardrobe UI Payload Models ---
class WardrobeAction(BaseModel):
    action: str
    label: str
    endpoint: str
    method: str = "POST"

class WardrobeCTAs(BaseModel):
    reorder: WardrobeAction
    dismiss: WardrobeAction
    bought_elsewhere: WardrobeAction
    still_have_plenty: WardrobeAction

class WardrobeItem(BaseModel):
    sku_id: str
    sku_name: str
    category: str
    last_delivery_date: date
    last_quantity: float
    estimated_days_remaining: float
    stock_level_percentage: float
    depletion_rate_daily: float
    reorder_probability: float
    prediction_engine: str
    is_nudge_eligible: bool
    gate_status: NudgeGateStatus
    gate_reason: str
    cta_triggers: WardrobeCTAs

class WardrobeResponse(BaseModel):
    household_id: str
    timestamp: datetime
    total_tracked_items: int
    eligible_nudges_count: int
    wardrobe_items: List[WardrobeItem]

class HceReport(BaseModel):
    timestamp: datetime
    kill_switch_active: bool
    total_evaluations: int
    nudges_generated: int
    nudges_blocked: int
    nudge_details: List[HceNudgeItem]
    bottom_up_demand_Eh: Dict[str, float]

class POReviewRequest(BaseModel):
    reviewed_by: str = Field(..., description="Staff/Manager identifier")
    override_quantities: Optional[Dict[str, int]] = Field(None, description="Mapping of sku_id -> overridden quantity")
    override_reason: Optional[str] = Field(None, description="Required explanation when overriding or rejecting")
