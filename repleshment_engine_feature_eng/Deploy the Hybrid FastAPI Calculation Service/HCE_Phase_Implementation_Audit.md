# Accesco Household Consumption Engine (HCE) — 5-Phase Architecture Audit Report

**Document ID:** ACC-HCE-AUDIT-2026-V1  
**Project:** Accesco Quick Commerce Hybrid Replenishment Platform  
**Target Module:** Household Consumption Engine (HCE) & CDAO Safety Gates  
**Audit Scope:** Database Schemas, FastAPI Route Handlers, Mathematical Engines, Frontend Payloads, Self-Correction Loops  
**Database Backend:** Local Docker PostgreSQL 16 (`accesco_postgres_local` on `localhost:5432/accesco_local`)  
**Audit Outcome:** **100% COMPLIANT (All 5 Phases Validated & Passing Automated Tests)**  

---

## 1. Executive Summary

This code audit evaluates the implementation of the **Household Consumption Engine (HCE)** against Accesco's finalized **5-Phase Architecture**. 

The audit identified three critical architectural gaps in the pre-existing codebase:
1. **Phase 1 Missing Ingestion Webhook:** No endpoint existed to receive the "accepted at the door" trigger, and the database schema only recorded checkout `purchase_date`, lacking an explicit `delivered_at` timestamp to anchor the depletion clock ($d_n$).
2. **Phase 2 Missing Path B Branch:** No conditional branching existed for users with $\ge 5$ purchases to hand off from Bayesian Shrinkage to the Databricks XGBoost Survival Accelerated Failure Time (AFT) model.
3. **Phase 4 & 5 Missing Frontend Payloads & Feedback Loops:** The frontend "Wardrobe" UI endpoint (`/nudges/wardrobe/{household_id}`) did not exist, and there were no self-correction endpoints for "Reorder", "Bought Elsewhere", or "Still Have Plenty".

All identified gaps were resolved with production-grade Python/FastAPI code, PostgreSQL schema updates, and comprehensive automated test suites.

---

## 2. Five-Phase Architectural Checklist

| Phase | Architecture Requirement | Initial Status | Final Status | Implementation Enforced |
|---|---|:---:|:---:|---|
| **Phase 1: Data Ingestion** | Store purchase events; "accepted at door" webhook; depletion clock starts at delivery time ($d_n$), NOT checkout time | **INCOMPLETE** | **PASSED** | Added `delivered_at` column to `oms_customer_purchase_log`; created `POST /events/delivery-confirmed` (and `/events/doorstep-accepted`). |
| **Phase 2: Depletion Prediction** | Path A: Bayesian Shrinkage blending catchment $\rho(i)$ with user history for $<5$ orders, $T_{low}$, Gamma CDF; Path B: Branch for $\ge 5$ orders with Databricks XGBoost Survival AFT hook | **PARTIAL** | **PASSED** | Added `if n < 5:` (Path A) vs `else:` (Path B) in `hce_engine.py`; implemented `predict_ml_survival_rate` with covariate extraction and Databricks REST API client. |
| **Phase 3: CDAO Eligibility Gate** | Strict DB evaluation before `ELIGIBLE`: 1. In stock (`wms_inventory_batch`), 2. Absent from cart, 3. Active $\le 7$d, 4. Outside 3d cooldown, 5. Consented | **PASSED** | **PASSED** | Verified in `hce_engine.py` with 100% test coverage; candidate window aligned to 1–2 days prior to $T_{low}$ ($expected\_low\_stock\_days \le 2.0$). |
| **Phase 4: Expected Output** | Structured JSON payload for frontend "Wardrobe" UI 1–2 days prior to $T_{low}$ date; includes `sku_id`, `estimated_days_remaining`, and CTA triggers | **MISSING** | **PASSED** | Implemented `GET /nudges/wardrobe/{household_id}` returning `WardrobeResponse` with `sku_id`, `estimated_days_remaining`, `stock_level_percentage`, and full CTAs. |
| **Phase 5: Feedback Loop** | Dedicated endpoints: 1. "Reorder" logs successful prediction; 2. "Bought Elsewhere" resets virtual stock to 100% without shrinking $r$; 3. "Still have plenty" mathematically lowers $r(h,i)$ | **MISSING** | **PASSED** | Created `cdao_household_sku_feedback` table; implemented `/feedback/reorder`, `/feedback/bought-elsewhere`, `/feedback/still-have-plenty`, and `/feedback/dismiss`. |

---

## 3. Detailed Phase-by-Phase Verification & Code Additions

### Phase 1: Data Ingestion (The User Action)

#### Requirements Verified:
1. Does `oms_customer_purchase_log` correctly store purchase events?
2. Is there an endpoint/webhook for "accepted at the door" trigger? Does the depletion clock start at delivery time ($d_n$), not checkout time?

#### Findings:
* `oms_customer_purchase_log` previously stored `purchase_date`, which conflated checkout time with delivery time.
* No webhook existed to receive delivery acceptance events from last-mile rider apps.

#### Code Updates Applied:
* **`sql/01_schema_ddl.sql` (Lines 78–90):**
  Added `delivered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP` and migration command:
  ```sql
  ALTER TABLE oms_customer_purchase_log 
  ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
  ```
* **`app/models/schemas.py`:**
  Added `DeliveryConfirmationEvent` model and updated `CustomerOrder` to carry `delivered_at`.
* **`app/repository/postgres.py`:**
  Implemented `record_delivery_event` which inserts the order with `delivered_at`, sets `oms_households.days_since_active = 0`, and ensures `is_active = True`.
* **`app/main.py`:**
  Implemented webhook endpoints `POST /events/delivery-confirmed` and `POST /events/doorstep-accepted`:
  ```python
  @app.post("/events/delivery-confirmed", status_code=status.HTTP_201_CREATED, tags=["Phase 1: Ingestion"])
  @app.post("/events/doorstep-accepted", status_code=status.HTTP_201_CREATED, tags=["Phase 1: Ingestion"])
  async def handle_doorstep_delivery_event(event: DeliveryConfirmationEvent):
      ...
  ```
* **`app/core/hce_engine.py`:**
  Updated depletion anchor calculation to strictly prioritize delivery time:
  ```python
  last_order = orders[-1]
  if last_order.delivered_at:
      d_n = last_order.delivered_at.date() if isinstance(last_order.delivered_at, datetime) else last_order.delivered_at
  else:
      d_n = last_order.order_date
  ```

---

### Phase 2: The Depletion Prediction (Calculation Layer)

#### Requirements Verified:
1. **Path A (Cold-Start / <5 purchases):** Bayesian Shrinkage formula blending catchment $\rho(i)$ with user history; expected low-stock day $T_{low}$ ($\theta = 0.20$); Gamma CDF reorder probability.
2. **Path B (ML Handoff / 5+ purchases):** Conditional branch checking `n >= 5` with hook/placeholder to call future Databricks XGBoost Survival AFT model endpoint.

#### Findings:
* Path A was implemented correctly mathematically, but executed indiscriminately for all order counts.
* Path B was completely absent.

#### Code Updates Applied:
* **`app/core/hce_engine.py`:**
  Implemented the branching logic and Databricks ML Hook:
  ```python
  # --- Phase 2: The Depletion Prediction (Calculation Layer) ---
  if n < 5:
      # Path A: Cold-Start / Bayesian Shrinkage (<5 purchases)
      prediction_engine = "PATH_A_BAYESIAN_SHRINKAGE"
      if n == 1:
          r_hi = base_rho
      else:
          sum_prev_q = sum(o.quantity for o in orders[:-1])
          r_hi = (k * base_rho + sum_prev_q) / (k + total_span_days)
  else:
      # Path B: ML Handoff / Databricks XGBoost Survival AFT (>=5 purchases)
      prediction_engine = "PATH_B_DATABRICKS_XGBOOST_AFT"
      r_hi = await predict_ml_survival_rate(household, sku, orders, base_rho=base_rho)
  ```
* **`predict_ml_survival_rate` Hook Function:**
  Extracts 7 survival covariates (`mean_interval`, `std_interval`, `median_interval`, `empirical_rate`, `serves_multiplier`, etc.), connects to `DATABRICKS_AFT_ENDPOINT_URL` with bearer token auth, and includes an empirical AFT hazard rate fallback.

---

### Phase 3: The CDAO Eligibility Gate (The Guardrail)

#### Requirements Verified:
Before marking a candidate as `ELIGIBLE`, strictly check DB for:
1. Item in stock in live inventory (`wms_inventory_batch`).
2. Item absent from active shopping cart (`in_active_cart`).
3. User active in last 7 days (`days_since_active <= 7`).
4. Outside 3-day reminder cooldown (`now - last_nudge_timestamp >= 3 days`).
5. User consented / not opted out (`is_opted_out == False`).

#### Findings:
* All 5 gates were implemented in `hce_engine.py` alongside system kill switch, quiet hours, and frequency caps.
* Updated candidate timing threshold to ensure candidates qualify **1 to 2 days prior to $T_{low}$**:
  `is_candidate = (p_reorder >= 0.50) or (expected_low_stock_days_from_today <= 2.0)`

#### Audit Matrix:
| Gate | Database Source Table & Column | Condition Checked | Action on Failure |
|---|---|---|---|
| **1. Stock** | `wms_inventory_batch.current_qty` (via `get_sound_on_hand`) | `sound_stock <= 0` | Set `BLOCKED_OUT_OF_STOCK`, suppress nudge |
| **2. Cart** | `oms_households.in_active_cart` | `household.in_active_cart == True` | Set `BLOCKED_IN_CART`, suppress nudge |
| **3. Dormancy** | `oms_households.days_since_active`, `is_active` | `days_since_active > 7 or not is_active` | Set `BLOCKED_DORMANT`, pause Gamma CDF |
| **4. Cooldown** | `oms_households.last_nudge_timestamp` | `now - last_nudge < 3 days` | Set `BLOCKED_COOLDOWN`, suppress nudge |
| **5. Consent** | `oms_households.is_opted_out` | `household.is_opted_out == True` | Set `BLOCKED_OPT_OUT`, suppress nudge |

---

### Phase 4: The Expected Output (Frontend Payload)

#### Requirements Verified:
1. API generates structured JSON payload for frontend 1 to 2 days prior to $T_{low}$.
2. JSON includes data needed by the "Wardrobe" UI: `sku_id`, `estimated_days_remaining`, and CTA triggers (Reorder, Dismiss).

#### Findings:
* Missing endpoint. No dedicated API route existed for the frontend mobile/web client.

#### Code Updates Applied:
* Added endpoints in `app/main.py`:
  * `GET /nudges/wardrobe/{household_id}`
  * `GET /frontend/wardrobe/{household_id}`
* Implemented `generate_wardrobe_payload(household_id: str)` in `app/core/hce_engine.py` returning `WardrobeResponse`.

---

### Phase 5: The Feedback Loop (Self-Correction)

#### Requirements Verified:
Dedicated endpoints to handle user feedback:
1. **"Reorder":** Logs a successful prediction.
2. **"Bought Elsewhere":** Resets virtual stock to 100% full, but **MUST NOT shrink consumption rate $r$**.
3. **"Still have plenty":** Mathematically lowers daily consumption rate $r(h,i)$ so the next cycle arrives later.

#### Findings:
* Completely missing in previous version.

#### Code Updates Applied:
* **Database State (`sql/01_schema_ddl.sql`):**
  Added `cdao_household_sku_feedback` table:
  ```sql
  CREATE TABLE IF NOT EXISTS cdao_household_sku_feedback (
      household_id VARCHAR(50) NOT NULL REFERENCES oms_households(household_id) ON DELETE CASCADE,
      sku_id VARCHAR(50) NOT NULL REFERENCES cdao_replenishment_priors(sku_id) ON DELETE CASCADE,
      feedback_type VARCHAR(30) NOT NULL,
      rate_multiplier DECIMAL(6,4) NOT NULL DEFAULT 1.0000,
      virtual_stock_reset_at TIMESTAMPTZ,
      last_feedback_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      notes TEXT,
      PRIMARY KEY (household_id, sku_id)
  );
  ```
* **Endpoints in `app/main.py`:**
  1. `POST /feedback/reorder`: Logs `FEEDBACK_REORDER_CONFIRMED` in audit log, marks prediction as `ACCURATE`, and logs purchase event.
  2. `POST /feedback/bought-elsewhere`: Sets `virtual_stock_reset_at = NOW()` and `rate_multiplier = 1.0000`. In `hce_engine.py`, resets stock to 100% and explicitly preserves $r(h,i)$ without shrinkage.
  3. `POST /feedback/still-have-plenty`: Applies `rate_multiplier = current * reduction_factor` (default `0.70`), reducing daily consumption rate by 30%. This increases $days\_to\_low\_stock = \frac{q_n(1-\theta)}{r_{new}}$, delaying the next nudge.
  4. `POST /feedback/dismiss`: Suppresses reminder and triggers 3-day cooldown.

---

## 4. Frontend Team JSON Payload Specification (Phase 4)

Below is the verified, production JSON response returned by `GET /nudges/wardrobe/{household_id}`:

```json
{
  "household_id": "H102",
  "timestamp": "2026-11-11T15:34:22.863999Z",
  "total_tracked_items": 1,
  "eligible_nudges_count": 1,
  "wardrobe_items": [
    {
      "sku_id": "SKU-MILK-1L",
      "sku_name": "Fresh Farm Milk (1 Litre)",
      "category": "dairy",
      "last_delivery_date": "2026-11-09",
      "last_quantity": 1.0,
      "estimated_days_remaining": -0.9,
      "stock_level_percentage": 0.0,
      "depletion_rate_daily": 0.7000,
      "reorder_probability": 0.8450,
      "prediction_engine": "PATH_A_BAYESIAN_SHRINKAGE",
      "is_nudge_eligible": true,
      "gate_status": "PASSED",
      "gate_reason": "Active, in-stock, consented, outside cooldown.",
      "cta_triggers": {
        "reorder": {
          "action": "REORDER",
          "label": "Reorder Now",
          "endpoint": "/feedback/reorder",
          "method": "POST"
        },
        "dismiss": {
          "action": "DISMISS",
          "label": "Dismiss",
          "endpoint": "/feedback/dismiss",
          "method": "POST"
        },
        "bought_elsewhere": {
          "action": "BOUGHT_ELSEWHERE",
          "label": "Bought Elsewhere",
          "endpoint": "/feedback/bought-elsewhere",
          "method": "POST"
        },
        "still_have_plenty": {
          "action": "STILL_HAVE_PLENTY",
          "label": "Still Have Plenty",
          "endpoint": "/feedback/still-have-plenty",
          "method": "POST"
        }
      }
    }
  ]
}
```

---

## 5. Automated Test Execution Results

All test suites were executed against the active local PostgreSQL database.

### Test Suite 1: `test_case/test_hce_5phases.py` (7 / 7 PASSED)
* `test_phase1_doorstep_acceptance_webhook` — **PASSED** (Ingestion and depletion clock $d_n$ verified)
* `test_phase2_path_a_and_path_b_depletion_prediction` — **PASSED** (Path A Bayesian vs Path B ML Survival hook verified)
* `test_phase3_cdao_eligibility_gates` — **PASSED** (Stock, cart, dormancy, cooldown, and opt-out verified)
* `test_phase4_frontend_wardrobe_payload` — **PASSED** (Wardrobe UI schema, $T_{low}$, and CTAs verified)
* `test_phase5_feedback_reorder` — **PASSED** (Reorder success audit logging verified)
* `test_phase5_feedback_bought_elsewhere` — **PASSED** (100% stock reset without rate shrinkage verified)
* `test_phase5_feedback_still_have_plenty` — **PASSED** (Mathematical rate lowering and $T_{low}$ delay verified)

### Test Suite 2: `test_case/test_api.py` (5 / 5 PASSED)
* `test_00_postgres_healthcheck` — **PASSED**
* `test_01_seven_day_dormancy_rule` — **PASSED**
* `test_02_doorstep_rejection_decoupling` — **PASSED**
* `test_03_kitchen_capacity_category_splits` — **PASSED**
* `test_04_end_to_end_aging_exclusion_flow` — **PASSED**

### Test Suite 3: `test_service.py` (10 / 10 PASSED)
* All 10 end-to-end integration checks including 30 concurrent PostgreSQL connections passed cleanly.

**Grand Total: 22 / 22 Tests PASSED (100% Green).**
