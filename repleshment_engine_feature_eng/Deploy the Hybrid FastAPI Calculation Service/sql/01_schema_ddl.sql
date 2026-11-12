-- ============================================================================
-- ACCESCO HYBRID REPLENISHMENT ENGINE - PHASE 1 & OPERATIONAL DDL (PostgreSQL)
-- ============================================================================

-- 1A. Replenishment Priors Configuration (CDAO)
-- Stores Argha's mathematical starting points for the Day 1 Cold Start
CREATE TABLE IF NOT EXISTS cdao_replenishment_priors (
    sku_id VARCHAR(50) PRIMARY KEY,
    sku_name VARCHAR(150) NOT NULL,
    category VARCHAR(50) NOT NULL,          -- leafy_greens, dairy, base_veg, fmcg
    is_fresh BOOLEAN NOT NULL DEFAULT TRUE,
    unit VARCHAR(20) NOT NULL DEFAULT 'units',
    buffer_b DECIMAL(4,3) NOT NULL,
    catchment_rho_1p DECIMAL(10,3) NOT NULL,
    catchment_rho_2p DECIMAL(10,3) NOT NULL,
    seed_daily_mean DECIMAL(10,3) NOT NULL,
    moq DECIMAL(10,3) NOT NULL DEFAULT 1.0,
    case_pack DECIMAL(10,3) NOT NULL DEFAULT 1.0,
    typical_order_qty DECIMAL(10,3) NOT NULL DEFAULT 1.0,
    prior_k_days DECIMAL(10,3) NOT NULL DEFAULT 14.0,
    prior_interval_mean DECIMAL(10,3) NOT NULL DEFAULT 4.0,
    prior_interval_std DECIMAL(10,3) NOT NULL DEFAULT 1.2,
    doorstep_rejection_factor_r DECIMAL(5,3) NOT NULL DEFAULT 1.000
);

-- 1B. Batch-Level Inventory Tracking (WMS)
-- Required for the No-Expiry Engine to calculate L(batch, t) and Fresh Routing
CREATE TABLE IF NOT EXISTS wms_inventory_batch (
    batch_id VARCHAR(50) PRIMARY KEY,
    sku_id VARCHAR(50) NOT NULL REFERENCES cdao_replenishment_priors(sku_id) ON DELETE CASCADE,
    received_qty DECIMAL(10,3) NOT NULL,
    current_qty DECIMAL(10,3) NOT NULL,
    is_fresh_produce BOOLEAN NOT NULL,
    manufacture_date TIMESTAMPTZ,
    harvest_date TIMESTAMPTZ,
    expiry_date TIMESTAMPTZ,
    location_id VARCHAR(50) DEFAULT 'STORE-RAJ-01',
    status VARCHAR(30) NOT NULL DEFAULT 'SELLABLE',
    hours_since_harvest DECIMAL(10,2),
    life_fraction_l DECIMAL(6,4)
);

-- 1C. Recipe Bill of Materials (OMS)
-- Required to convert Swadishtt meals, kits, and cook bookings into ingredient weights
CREATE TABLE IF NOT EXISTS oms_recipe_bom (
    dish_sku_id VARCHAR(50) NOT NULL,
    ingredient_sku_id VARCHAR(50) NOT NULL REFERENCES cdao_replenishment_priors(sku_id) ON DELETE CASCADE,
    quantity_base_unit DECIMAL(10,3) NOT NULL,
    unit_of_measure VARCHAR(10) NOT NULL,
    PRIMARY KEY (dish_sku_id, ingredient_sku_id)
);

-- 1D. Calendar Factors (CDAO)
CREATE TABLE IF NOT EXISTS cdao_calendar_factors (
    factor_type VARCHAR(20) NOT NULL,       -- WEEKDAY, MONTH_CYCLE, FESTIVAL
    factor_key VARCHAR(20) NOT NULL,
    multiplier DECIMAL(5,3) NOT NULL,
    PRIMARY KEY (factor_type, factor_key)
);

-- ============================================================================
-- COMPLEMENTARY OPERATIONAL & GOVERNANCE TABLES
-- ============================================================================

-- 1E. Household Profiles & Consent/Dormancy Flags (OMS)
CREATE TABLE IF NOT EXISTS oms_households (
    household_id VARCHAR(50) PRIMARY KEY,
    cluster_id VARCHAR(50) NOT NULL DEFAULT 'CLUSTER_CENTRAL_01',
    serves_multiplier DECIMAL(4,2) NOT NULL DEFAULT 1.0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    days_since_active INTEGER NOT NULL DEFAULT 0,
    is_opted_out BOOLEAN NOT NULL DEFAULT FALSE,
    in_active_cart BOOLEAN NOT NULL DEFAULT FALSE,
    last_nudge_timestamp TIMESTAMPTZ,
    daily_nudge_count INTEGER NOT NULL DEFAULT 0
);

-- 1F. Customer Purchase Log (OMS / CDAO)
-- Depletion clock starts at delivery time (d_n), NOT checkout time.
CREATE TABLE IF NOT EXISTS oms_customer_purchase_log (
    event_id VARCHAR(50) PRIMARY KEY,
    household_id VARCHAR(50) NOT NULL REFERENCES oms_households(household_id) ON DELETE CASCADE,
    sku_id VARCHAR(50) NOT NULL REFERENCES cdao_replenishment_priors(sku_id) ON DELETE CASCADE,
    quantity DECIMAL(10,3) NOT NULL,
    purchase_date TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    feedback_status VARCHAR(30) NOT NULL DEFAULT 'NORMAL'
);

ALTER TABLE oms_customer_purchase_log ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

-- 1F-2. Household-SKU Dynamic Feedback State (CDAO Self-Correction Loop)
-- Tracks virtual stock resets (Bought Elsewhere) and rate dampening (Still Have Plenty)
CREATE TABLE IF NOT EXISTS cdao_household_sku_feedback (
    household_id VARCHAR(50) NOT NULL REFERENCES oms_households(household_id) ON DELETE CASCADE,
    sku_id VARCHAR(50) NOT NULL REFERENCES cdao_replenishment_priors(sku_id) ON DELETE CASCADE,
    feedback_type VARCHAR(30) NOT NULL,       -- REORDER, BOUGHT_ELSEWHERE, STILL_HAVE_PLENTY, DISMISS
    rate_multiplier DECIMAL(6,4) NOT NULL DEFAULT 1.0000,
    virtual_stock_reset_at TIMESTAMPTZ,
    last_feedback_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    PRIMARY KEY (household_id, sku_id)
);

-- 1G. Category-Specific & Total Kitchen Capacity Limits (CDAO / Kitchen)
CREATE TABLE IF NOT EXISTS cdao_kitchen_capacity (
    category VARCHAR(50) PRIMARY KEY,       -- leafy_greens, dairy, base_veg, total_fresh
    daily_cap_kg DECIMAL(10,3) NOT NULL
);

-- 1H. System Configuration & Operational State
CREATE TABLE IF NOT EXISTS cdao_system_config (
    config_key VARCHAR(50) PRIMARY KEY,
    config_value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 1I. Purchase Order Proposals (Warehouse Staff Approval Workflow)
CREATE TABLE IF NOT EXISTS cdao_po_proposals (
    po_id VARCHAR(50) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING_APPROVAL',
    target_delivery_date DATE NOT NULL,
    festival_factor_f DECIMAL(6,3) NOT NULL,
    month_salary_factor_m DECIMAL(6,3) NOT NULL,
    doorstep_rejection_factor_r DECIMAL(6,3) NOT NULL,
    kitchen_capacity_cap_kg DECIMAL(10,3) NOT NULL,
    total_fresh_overflow_kg DECIMAL(10,3) NOT NULL,
    overflow_cap_exceeded BOOLEAN NOT NULL,
    buffer_reduction_iterations INTEGER NOT NULL,
    total_proposed_units INTEGER NOT NULL,
    procurement_alerts_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    items_json JSONB NOT NULL,
    reviewed_by VARCHAR(100),
    reviewed_at TIMESTAMPTZ,
    override_reason TEXT
);

-- 1J. Immutable Nudge Audit Log
CREATE TABLE IF NOT EXISTS cdao_nudge_audit_log (
    audit_id VARCHAR(50) PRIMARY KEY,
    household_id VARCHAR(50) NOT NULL,
    sku_id VARCHAR(50) NOT NULL,
    sku_name VARCHAR(150) NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    reorder_probability DECIMAL(6,4) NOT NULL,
    gate_status VARCHAR(50) NOT NULL,
    action_taken VARCHAR(30) NOT NULL,
    reason TEXT NOT NULL
);
