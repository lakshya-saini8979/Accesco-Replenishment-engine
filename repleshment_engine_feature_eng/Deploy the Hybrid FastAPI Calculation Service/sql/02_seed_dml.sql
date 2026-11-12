-- ============================================================================
-- ACCESCO HYBRID REPLENISHMENT ENGINE - PHASE 1 & OPERATIONAL SEED DML
-- ============================================================================

TRUNCATE TABLE
    cdao_nudge_audit_log,
    cdao_po_proposals,
    cdao_household_sku_feedback,
    oms_customer_purchase_log,
    oms_households,
    oms_recipe_bom,
    wms_inventory_batch,
    cdao_replenishment_priors,
    cdao_calendar_factors,
    cdao_kitchen_capacity,
    cdao_system_config
RESTART IDENTITY CASCADE;

-- 2A. Weekday Constants c(w) from Pull Theory Page 5
INSERT INTO cdao_calendar_factors (factor_type, factor_key, multiplier) VALUES
('WEEKDAY', 'Mon', 0.950),
('WEEKDAY', 'Tue', 0.950),
('WEEKDAY', 'Wed', 0.970),
('WEEKDAY', 'Thu', 0.980),
('WEEKDAY', 'Fri', 1.030),
('WEEKDAY', 'Sat', 1.100),
('WEEKDAY', 'Sun', 1.020);

-- 2B. Month-Cycle Factors M(t) from Pull Theory Page 5
INSERT INTO cdao_calendar_factors (factor_type, factor_key, multiplier) VALUES
('MONTH_CYCLE', 'Days_1_7', 1.080),
('MONTH_CYCLE', 'Days_8_22', 1.000),
('MONTH_CYCLE', 'Days_23_31', 0.920);

-- 2C. Category & Aggregate Kitchen Absorption Limits K (in kg)
INSERT INTO cdao_kitchen_capacity (category, daily_cap_kg) VALUES
('leafy_greens', 8.000),
('dairy', 12.000),
('base_veg', 20.000),
('total_fresh', 35.000);

-- 2D. System Configuration & Guardrails
INSERT INTO cdao_system_config (config_key, config_value) VALUES
('emergency_kill_switch', 'false'),
('quiet_hours_start', '21'),
('quiet_hours_end', '8'),
('cooldown_days', '3'),
('daily_frequency_cap', '1'),
('latest_bottom_up_eh', '{}');

-- 2E. Seed the Rajajinagar ICP Replenishment Priors & SKU Catalog
INSERT INTO cdao_replenishment_priors
(sku_id, sku_name, category, is_fresh, unit, buffer_b, catchment_rho_1p, catchment_rho_2p, seed_daily_mean, moq, case_pack, typical_order_qty, prior_k_days, prior_interval_mean, prior_interval_std, doorstep_rejection_factor_r)
VALUES
('SKU-SPINACH-BUNCH', 'Farm Fresh Palak / Spinach (250g)', 'leafy_greens', TRUE, 'bunches', 0.100, 0.250, 0.400, 32.000, 10.000, 5.000, 1.000, 14.000, 4.000, 1.200, 1.000),
('SKU-CORIANDER-100G', 'Fresh Coriander Leaves (100g)', 'leafy_greens', TRUE, 'bunches', 0.100, 0.330, 0.528, 45.000, 20.000, 10.000, 1.000, 14.000, 3.000, 1.000, 1.000),
('SKU-PANEER-FRESH', 'Artisanal Fresh Paneer (200g)', 'dairy', TRUE, 'packs', 0.100, 0.150, 0.240, 18.000, 5.000, 1.000, 1.000, 14.000, 4.000, 1.200, 1.000),
('SKU-MILK-1L', 'Fresh Farm Milk (1 Litre)', 'dairy', TRUE, 'pouches', 0.100, 1.000, 1.600, 70.000, 24.000, 12.000, 2.000, 14.000, 1.500, 0.500, 1.000),
('SKU-TOMATO-HYBRID', 'Country Tomatoes (1kg)', 'base_veg', TRUE, 'kg', 0.150, 0.250, 0.400, 55.000, 30.000, 10.000, 1.000, 14.000, 4.000, 1.200, 1.000),
('SKU-POTATO-1KG', 'Agra Potatoes (1kg)', 'base_veg', TRUE, 'kg', 0.150, 0.300, 0.480, 80.000, 40.000, 10.000, 2.000, 14.000, 6.000, 1.800, 1.000),
('SKU-ATTA-5KG', 'Chakki Fresh Sharbati Atta (5kg)', 'fmcg', FALSE, 'bags', 0.250, 0.250, 0.400, 18.000, 8.000, 4.000, 1.000, 30.000, 20.000, 4.000, 1.000),
('SKU-TOOR-DAL-1KG', 'Unpolished Toor Dal (1kg)', 'fmcg', FALSE, 'packs', 0.250, 0.070, 0.112, 22.000, 12.000, 6.000, 1.000, 30.000, 14.000, 3.000, 1.000);

-- 2F. Recipe BOM Mapping (Exploding Swadishtt Meal into Ingredients)
INSERT INTO oms_recipe_bom (dish_sku_id, ingredient_sku_id, quantity_base_unit, unit_of_measure) VALUES
('MEAL-PALAK-PANEER-1', 'SKU-SPINACH-BUNCH', 0.150, 'kg'),
('MEAL-PALAK-PANEER-1', 'SKU-PANEER-FRESH', 0.100, 'kg'),
('MEAL-PALAK-PANEER-1', 'SKU-TOMATO-HYBRID', 0.050, 'kg');

-- 2G. Warehouse Inventory Batches (WMS)
INSERT INTO wms_inventory_batch
(batch_id, sku_id, received_qty, current_qty, is_fresh_produce, manufacture_date, harvest_date, expiry_date, location_id, status)
VALUES
('BATCH_SPN_01', 'SKU-SPINACH-BUNCH', 20.000, 18.000, TRUE, NULL, NOW() - INTERVAL '22 hours', NOW() + INTERVAL '50 hours', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_SPN_02', 'SKU-SPINACH-BUNCH', 10.000, 8.000, TRUE, NULL, NOW() - INTERVAL '54 hours', NOW() + INTERVAL '18 hours', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_PAN_01', 'SKU-PANEER-FRESH', 10.000, 6.000, TRUE, NULL, NOW() - INTERVAL '18 hours', NOW() + INTERVAL '54 hours', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_MLK_01', 'SKU-MILK-1L', 40.000, 35.000, TRUE, NULL, NOW() - INTERVAL '14 hours', NOW() + INTERVAL '58 hours', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_MLK_02', 'SKU-MILK-1L', 5.000, 4.000, TRUE, NULL, NOW() - INTERVAL '76 hours', NOW() - INTERVAL '4 hours', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_TOM_01', 'SKU-TOMATO-HYBRID', 30.000, 25.000, TRUE, NULL, NOW() - INTERVAL '40 hours', NOW() + INTERVAL '32 hours', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_POT_01', 'SKU-POTATO-1KG', 45.000, 40.000, TRUE, NULL, NOW() - INTERVAL '36 hours', NOW() + INTERVAL '36 hours', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_ATT_01', 'SKU-ATTA-5KG', 15.000, 12.000, FALSE, NOW() - INTERVAL '60 days', NULL, NOW() + INTERVAL '120 days', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_ATT_02', 'SKU-ATTA-5KG', 8.000, 5.000, FALSE, NOW() - INTERVAL '120 days', NULL, NOW() + INTERVAL '65 days', 'STORE-RAJ-01', 'SELLABLE'),
('BATCH_DAL_01', 'SKU-TOOR-DAL-1KG', 20.000, 15.000, FALSE, NOW() - INTERVAL '30 days', NULL, NOW() + INTERVAL '150 days', 'STORE-RAJ-01', 'SELLABLE');

-- 2H. Household Profiles
INSERT INTO oms_households
(household_id, cluster_id, serves_multiplier, is_active, days_since_active, is_opted_out, in_active_cart, last_nudge_timestamp, daily_nudge_count)
VALUES
('H101', 'CLUSTER_CENTRAL_01', 1.60, TRUE, 1, FALSE, FALSE, NOW() - INTERVAL '5 days', 0),
('H102', 'CLUSTER_CENTRAL_01', 1.00, TRUE, 0, FALSE, FALSE, NULL, 0),
('H103', 'CLUSTER_CENTRAL_01', 1.00, FALSE, 12, FALSE, FALSE, NULL, 0),
('H104', 'CLUSTER_CENTRAL_01', 1.00, TRUE, 1, TRUE, FALSE, NULL, 0),
('H105', 'CLUSTER_CENTRAL_01', 1.00, TRUE, 0, FALSE, TRUE, NULL, 0),
('H106', 'CLUSTER_CENTRAL_01', 1.00, TRUE, 0, FALSE, FALSE, NOW() - INTERVAL '1 day', 1);

-- 2I. Customer Purchase Orders (Depletion clock starts at delivered_at d_n)
INSERT INTO oms_customer_purchase_log
(event_id, household_id, sku_id, quantity, purchase_date, delivered_at, feedback_status)
VALUES
('ORD_101_1', 'H101', 'SKU-MILK-1L', 2.000, NOW() - INTERVAL '7 days', NOW() - INTERVAL '7 days', 'NORMAL'),
('ORD_101_2', 'H101', 'SKU-MILK-1L', 2.000, NOW() - INTERVAL '5 days', NOW() - INTERVAL '5 days', 'NORMAL'),
('ORD_101_3', 'H101', 'SKU-MILK-1L', 2.000, NOW() - INTERVAL '3 days', NOW() - INTERVAL '3 days', 'NORMAL'),
('ORD_101_4', 'H101', 'SKU-MILK-1L', 2.000, NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day', 'NORMAL'),
('ORD_101_5', 'H101', 'SKU-SPINACH-BUNCH', 1.000, NOW() - INTERVAL '8 days', NOW() - INTERVAL '8 days', 'NORMAL'),
('ORD_101_6', 'H101', 'SKU-SPINACH-BUNCH', 1.000, NOW() - INTERVAL '4 days', NOW() - INTERVAL '4 days', 'NORMAL'),
('ORD_102_1', 'H102', 'SKU-MILK-1L', 1.000, NOW() - INTERVAL '2 days', NOW() - INTERVAL '2 days', 'NORMAL'),
('ORD_103_1', 'H103', 'SKU-MILK-1L', 1.000, NOW() - INTERVAL '15 days', NOW() - INTERVAL '15 days', 'NORMAL'),
('ORD_104_1', 'H104', 'SKU-SPINACH-BUNCH', 1.000, NOW() - INTERVAL '4 days', NOW() - INTERVAL '4 days', 'NORMAL'),
('ORD_105_1', 'H105', 'SKU-TOMATO-HYBRID', 1.000, NOW() - INTERVAL '5 days', NOW() - INTERVAL '5 days', 'NORMAL'),
('ORD_106_1', 'H106', 'SKU-MILK-1L', 1.000, NOW() - INTERVAL '3 days', NOW() - INTERVAL '3 days', 'NORMAL');
