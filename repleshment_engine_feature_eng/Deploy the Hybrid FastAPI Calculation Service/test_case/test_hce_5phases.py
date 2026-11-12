"""
Comprehensive 5-Phase Architecture Test Suite for Household Consumption Engine (HCE)
Tests Phase 1 through Phase 5 against PostgreSQL via FastAPI + httpx.
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["PG_NULLPOOL"] = "true"

load_dotenv(PROJECT_ROOT / ".env")

from app.main import app  # noqa: E402
from app.database import db  # noqa: E402

RAW_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://accesco:accesco_local_pass@localhost:5432/accesco_local",
).replace("postgresql+asyncpg://", "postgresql://")


@pytest_asyncio.fixture(scope="function")
async def pg_conn():
    conn = await asyncpg.connect(RAW_DB_URL)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="function")
async def api_client():
    await db.reset_seed_data()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0) as client:
        yield client
    await db.dispose()


# ==============================================================================
# PHASE 1: DATA INGESTION (DOORSTEP ACCEPTANCE WEBHOOK)
# ==============================================================================
@pytest.mark.asyncio
async def test_phase1_doorstep_acceptance_webhook(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Phase 1: Ingestion
    Verify that POST /events/delivery-confirmed stores purchase events and anchors
    the depletion clock at delivery time d_n, NOT checkout time.
    """
    delivery_time = datetime.now(timezone.utc) - timedelta(hours=2)
    payload = {
        "household_id": "H102",
        "sku_id": "SKU-MILK-1L",
        "quantity": 3.0,
        "delivered_at": delivery_time.isoformat(),
        "event_id": "EVT-TEST-DELIV-P1",
        "order_id": "ORD-TEST-001",
    }
    resp = await api_client.post("/events/delivery-confirmed", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert data["event"] == "DOORSTEP_DELIVERY_ACCEPTED"
    assert data["household_id"] == "H102"
    assert data["sku_id"] == "SKU-MILK-1L"
    assert data["quantity"] == 3.0
    assert "depletion_clock_started_at" in data

    # Verify directly in PostgreSQL
    row = await pg_conn.fetchrow(
        "SELECT event_id, quantity, delivered_at, feedback_status FROM oms_customer_purchase_log WHERE event_id = $1",
        "EVT-TEST-DELIV-P1",
    )
    assert row is not None
    assert float(row["quantity"]) == 3.0
    assert row["delivered_at"] is not None


# ==============================================================================
# PHASE 2: DEPLETION PREDICTION (PATH A & PATH B)
# ==============================================================================
@pytest.mark.asyncio
async def test_phase2_path_a_and_path_b_depletion_prediction(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Phase 2: Depletion Prediction
    - Path A: Cold-start (< 5 purchases) uses Bayesian Shrinkage blending catchment rho(i).
    - Path B: Mature user (>= 5 purchases) uses the Databricks XGBoost Survival AFT hook.
    """
    # Household H102 has 1 purchase (<5) -> Path A
    resp_hce = await api_client.post("/jobs/hce-nudge?run_sync=true&ignore_quiet_hours=true")
    assert resp_hce.status_code == 200
    hce_items = {item["household_id"]: item for item in resp_hce.json()["nudge_details"]}

    item_h102 = hce_items["H102"]
    assert item_h102["purchases_count"] < 5
    assert item_h102["prediction_engine"] == "PATH_A_BAYESIAN_SHRINKAGE"
    assert item_h102["shrunk_rate_r_hi"] > 0.0

    # Create a user with >= 5 purchases to trigger Path B
    hh_mature = "TEST-HH-MATURE-5P"
    await pg_conn.execute(
        """
        INSERT INTO oms_households (household_id, cluster_id, serves_multiplier, is_active, days_since_active, is_opted_out, in_active_cart, last_nudge_timestamp, daily_nudge_count)
        VALUES ($1, 'CLUSTER_CENTRAL_01', 1.0, TRUE, 0, FALSE, FALSE, NULL, 0)
        """,
        hh_mature,
    )
    now = datetime.now(timezone.utc)
    for i in range(5):
        await pg_conn.execute(
            """
            INSERT INTO oms_customer_purchase_log (event_id, household_id, sku_id, quantity, purchase_date, delivered_at, feedback_status)
            VALUES ($1, $2, 'SKU-MILK-1L', 2.0, $3, $3, 'NORMAL')
            """,
            f"EVT-MATURE-{i}",
            hh_mature,
            now - timedelta(days=(12 - i * 2)),
        )

    resp_hce2 = await api_client.post("/jobs/hce-nudge?run_sync=true&ignore_quiet_hours=true")
    assert resp_hce2.status_code == 200
    hce_items2 = {item["household_id"]: item for item in resp_hce2.json()["nudge_details"] if item["sku_id"] == "SKU-MILK-1L"}

    item_mature = hce_items2[hh_mature]
    assert item_mature["purchases_count"] >= 5
    assert item_mature["prediction_engine"] == "PATH_B_DATABRICKS_XGBOOST_AFT"
    assert item_mature["shrunk_rate_r_hi"] > 0.0


# ==============================================================================
# PHASE 3: CDAO ELIGIBILITY GATES
# ==============================================================================
@pytest.mark.asyncio
async def test_phase3_cdao_eligibility_gates(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Phase 3: The CDAO Eligibility Gate
    Verifies that before a nudge candidate is marked as ELIGIBLE, the code strictly checks DB:
    1. In-stock in live inventory
    2. Absent from active shopping cart
    3. Active in last 7 days (dormancy)
    4. Outside 3-day cooldown
    5. Not opted out
    """
    resp = await api_client.post("/jobs/hce-nudge?run_sync=true&ignore_quiet_hours=true")
    assert resp.status_code == 200
    details = {item["household_id"]: item for item in resp.json()["nudge_details"]}

    # Gate 1: Out of stock
    # Zero out stock for SKU-MILK-1L
    await pg_conn.execute("UPDATE wms_inventory_batch SET current_qty = 0 WHERE sku_id = 'SKU-MILK-1L'")
    resp_oos = await api_client.post("/jobs/hce-nudge?run_sync=true&ignore_quiet_hours=true")
    oos_item = next(item for item in resp_oos.json()["nudge_details"] if item["household_id"] == "H102" and item["sku_id"] == "SKU-MILK-1L")
    assert oos_item["gate_status"] == "BLOCKED_OUT_OF_STOCK"
    assert oos_item["nudge_eligible"] is False

    # Restore stock
    await pg_conn.execute("UPDATE wms_inventory_batch SET current_qty = 35 WHERE batch_id = 'BATCH_MLK_01'")

    # Gate 2: In Cart
    assert details["H105"]["gate_status"] == "BLOCKED_IN_CART"
    assert details["H105"]["nudge_eligible"] is False

    # Gate 3: Dormancy (>7 days)
    assert details["H103"]["gate_status"] == "BLOCKED_DORMANT"
    assert details["H103"]["nudge_eligible"] is False

    # Gate 4: Cooldown (<3 days)
    assert details["H106"]["gate_status"] == "BLOCKED_COOLDOWN"
    assert details["H106"]["nudge_eligible"] is False

    # Gate 5: Opted Out
    assert details["H104"]["gate_status"] == "BLOCKED_OPT_OUT"
    assert details["H104"]["nudge_eligible"] is False


# ==============================================================================
# PHASE 4: FRONTEND WARDROBE PAYLOAD
# ==============================================================================
@pytest.mark.asyncio
async def test_phase4_frontend_wardrobe_payload(api_client: httpx.AsyncClient):
    """
    Phase 4: The Expected Output (Frontend Payload)
    Verifies that GET /nudges/wardrobe/{household_id} generates the structured JSON payload
    including sku_id, estimated_days_remaining, stock level, and CTA triggers (Reorder, Dismiss, etc.)
    """
    resp = await api_client.get("/nudges/wardrobe/H102")
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["household_id"] == "H102"
    assert payload["total_tracked_items"] >= 1
    item = payload["wardrobe_items"][0]

    # Verify required frontend schema keys
    assert "sku_id" in item
    assert "estimated_days_remaining" in item
    assert "stock_level_percentage" in item
    assert "depletion_rate_daily" in item
    assert "reorder_probability" in item
    assert "cta_triggers" in item

    ctas = item["cta_triggers"]
    assert ctas["reorder"]["action"] == "REORDER"
    assert ctas["reorder"]["endpoint"] == "/feedback/reorder"
    assert ctas["dismiss"]["action"] == "DISMISS"
    assert ctas["dismiss"]["endpoint"] == "/feedback/dismiss"
    assert ctas["bought_elsewhere"]["action"] == "BOUGHT_ELSEWHERE"
    assert ctas["bought_elsewhere"]["endpoint"] == "/feedback/bought-elsewhere"
    assert ctas["still_have_plenty"]["action"] == "STILL_HAVE_PLENTY"
    assert ctas["still_have_plenty"]["endpoint"] == "/feedback/still-have-plenty"


# ==============================================================================
# PHASE 5: THE FEEDBACK LOOP (SELF-CORRECTION)
# ==============================================================================
@pytest.mark.asyncio
async def test_phase5_feedback_reorder(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Phase 5.1: 'Reorder' Feedback
    Logs a successful prediction in audit ledger and ingests reorder event.
    """
    resp = await api_client.post(
        "/feedback/reorder",
        json={"household_id": "H102", "sku_id": "SKU-MILK-1L", "quantity": 2.0, "notes": "Test Reorder CTA"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert data["action"] == "REORDER"
    assert data["prediction_outcome"] == "ACCURATE"

    # Verify audit log entry
    row = await pg_conn.fetchrow(
        "SELECT action_taken, gate_status FROM cdao_nudge_audit_log WHERE household_id = $1 AND sku_id = $2 ORDER BY evaluated_at DESC LIMIT 1",
        "H102",
        "SKU-MILK-1L",
    )
    assert row["action_taken"] == "FEEDBACK_REORDER_CONFIRMED"
    assert row["gate_status"] == "PASSED"


@pytest.mark.asyncio
async def test_phase5_feedback_bought_elsewhere(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Phase 5.2: 'Bought Elsewhere' Feedback
    Resets the virtual stock to 100% full, but MUST NOT shrink the consumption rate r.
    """
    resp = await api_client.post(
        "/feedback/bought-elsewhere",
        json={"household_id": "H102", "sku_id": "SKU-MILK-1L", "notes": "Bought at nearby store"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert data["action"] == "BOUGHT_ELSEWHERE"
    assert data["virtual_stock_percentage"] == 100.0
    assert data["rate_r_shrunk"] is False
    assert data["rate_multiplier"] == 1.0

    # Verify virtual stock in Wardrobe UI is now 100% full
    wardrobe = await api_client.get("/nudges/wardrobe/H102")
    item = next(i for i in wardrobe.json()["wardrobe_items"] if i["sku_id"] == "SKU-MILK-1L")
    assert item["stock_level_percentage"] == 100.0


@pytest.mark.asyncio
async def test_phase5_feedback_still_have_plenty(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Phase 5.3: 'Still Have Plenty' Feedback
    Mathematically lowers the daily consumption rate r(h,i) so the next prediction cycle arrives later.
    """
    # 1. Get baseline rate from wardrobe
    w_initial = await api_client.get("/nudges/wardrobe/H102")
    item_initial = next(i for i in w_initial.json()["wardrobe_items"] if i["sku_id"] == "SKU-MILK-1L")
    r_initial = item_initial["depletion_rate_daily"]
    days_initial = item_initial["estimated_days_remaining"]

    # 2. Submit 'Still Have Plenty' feedback with 30% reduction (factor 0.70)
    resp = await api_client.post(
        "/feedback/still-have-plenty",
        json={"household_id": "H102", "sku_id": "SKU-MILK-1L", "reduction_factor": 0.70},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert data["action"] == "STILL_HAVE_PLENTY"
    assert data["self_correction"] == "CONSUMPTION_RATE_LOWERED"
    assert data["rate_multiplier_applied"] == 0.70

    # 3. Verify that r(h,i) was mathematically lowered and days remaining increased
    w_after = await api_client.get("/nudges/wardrobe/H102")
    item_after = next(i for i in w_after.json()["wardrobe_items"] if i["sku_id"] == "SKU-MILK-1L")
    r_after = item_after["depletion_rate_daily"]
    days_after = item_after["estimated_days_remaining"]

    assert r_after < r_initial, f"Expected lowered rate: {r_after} < {r_initial}"
    assert days_after > days_initial, f"Expected delayed T_low: {days_after} > {days_initial}"
