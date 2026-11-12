"""
Edge-Case & Integration Test Suite for Accesco Hybrid Replenishment Engine.
Executes against the live PostgreSQL database via FastAPI + httpx, records
exact JSON request/response payloads for reporting, and cleans up fixtures
for 100% repeatability.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv

# Ensure project root is importable
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

PAYLOAD_LOG_PATH = Path(__file__).resolve().parent / "captured_payloads.json"


def save_captured_payload(test_name: str, request_info: dict, response_status: int, response_body: dict):
    """Persists exact request/response payloads for Markdown report generation."""
    existing = {}
    if PAYLOAD_LOG_PATH.exists():
        existing = json.loads(PAYLOAD_LOG_PATH.read_text(encoding="utf-8"))
    existing[test_name] = {
        "request": request_info,
        "response_status": response_status,
        "response_body": response_body,
    }
    PAYLOAD_LOG_PATH.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")


@pytest_asyncio.fixture(scope="function")
async def pg_conn():
    """Direct asyncpg connection for deterministic test data setup and cleanup."""
    conn = await asyncpg.connect(RAW_DB_URL)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="function")
async def api_client():
    """Async HTTP client wired through ASGI or live server with clean PostgreSQL seed reset."""
    await db.reset_seed_data()
    base_url = os.getenv("API_BASE_URL")
    if base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            yield client
    else:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0) as client:
            yield client
    await db.dispose()


@pytest.mark.asyncio
async def test_00_postgres_healthcheck(api_client: httpx.AsyncClient):
    """Verify FastAPI is connected to local PostgreSQL via asyncpg."""
    resp = await api_client.get("/health")
    body = resp.json()
    save_captured_payload(
        "test_00_postgres_healthcheck",
        {"method": "GET", "url": "/health", "query": None, "body": None},
        resp.status_code,
        body,
    )
    assert resp.status_code == 200
    assert body["status"] == "HEALTHY"
    assert body["database_connected"] is True
    assert "PostgreSQL" in body["storage_mode"]


@pytest.mark.asyncio
async def test_01_seven_day_dormancy_rule(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Edge Case 1: The 7-Day Dormancy Rule (HCE Engine)
    Insert a household that has not opened the app in 8 days (days_since_active = 8).
    Trigger POST /jobs/hce-nudge and assert gate_status == 'BLOCKED_DORMANT' and nudge_eligible == False.
    """
    test_hh = "TEST-HH-DORMANT-8D"
    test_evt = "TEST-EVT-DORMANT-1"
    try:
        await pg_conn.execute(
            """
            INSERT INTO oms_households
            (household_id, cluster_id, serves_multiplier, is_active, days_since_active, is_opted_out, in_active_cart, daily_nudge_count)
            VALUES ($1, 'CLUSTER_CENTRAL_01', 1.00, TRUE, 8, FALSE, FALSE, 0)
            ON CONFLICT (household_id) DO UPDATE SET days_since_active = 8, is_active = TRUE
            """,
            test_hh,
        )
        await pg_conn.execute(
            """
            INSERT INTO oms_customer_purchase_log (event_id, household_id, sku_id, quantity, purchase_date, feedback_status)
            VALUES ($1, $2, 'SKU-MILK-1L', 2.000, NOW() - INTERVAL '10 days', 'NORMAL')
            ON CONFLICT (event_id) DO NOTHING
            """,
            test_evt,
            test_hh,
        )

        url = "/jobs/hce-nudge?run_sync=true&ignore_quiet_hours=true"
        resp = await api_client.post(url)
        body = resp.json()
        save_captured_payload(
            "test_01_seven_day_dormancy_rule",
            {"method": "POST", "url": url, "query": {"run_sync": True, "ignore_quiet_hours": True}, "body": None},
            resp.status_code,
            body,
        )

        assert resp.status_code == 200
        matches = [d for d in body["nudge_details"] if d["household_id"] == test_hh]
        assert len(matches) == 1
        dormant_eval = matches[0]
        assert dormant_eval["nudge_eligible"] is False
        assert dormant_eval["gate_status"] == "BLOCKED_DORMANT"
        assert "8 days inactive" in dormant_eval["reason"]

        # Verify audit log persisted in PostgreSQL
        audit_row = await pg_conn.fetchrow(
            "SELECT gate_status, action_taken FROM cdao_nudge_audit_log WHERE household_id = $1 ORDER BY evaluated_at DESC LIMIT 1",
            test_hh,
        )
        assert audit_row is not None
        assert audit_row["gate_status"] == "BLOCKED_DORMANT"
        assert audit_row["action_taken"] == "SUPPRESSED"
    finally:
        await pg_conn.execute("DELETE FROM cdao_nudge_audit_log WHERE household_id = $1", test_hh)
        await pg_conn.execute("DELETE FROM oms_customer_purchase_log WHERE household_id = $1", test_hh)
        await pg_conn.execute("DELETE FROM oms_households WHERE household_id = $1", test_hh)


@pytest.mark.asyncio
async def test_02_doorstep_rejection_decoupling(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Edge Case 2: Doorstep Rejection Decoupling (Pull Engine)
    Feed a high rejection factor (R = 0.65) into PostgreSQL for SKU-TOMATO-HYBRID.
    Trigger POST /jobs/store-pull and assert:
      1) A procurement alert is generated for SKU-TOMATO-HYBRID.
      2) Base demand D(i, t+1) is IDENTICAL to baseline (NOT lowered by R).
    """
    sku_id = "SKU-TOMATO-HYBRID"
    target_date = "2026-11-11"
    url = f"/jobs/store-pull?run_sync=true&delivery_date={target_date}"
    try:
        # Step A: Baseline pull with R = 1.000
        await pg_conn.execute(
            "UPDATE cdao_replenishment_priors SET doorstep_rejection_factor_r = 1.000 WHERE sku_id = $1",
            sku_id,
        )
        base_resp = await api_client.post(url)
        assert base_resp.status_code == 200
        base_body = base_resp.json()
        base_item = next(it for it in base_body["items"] if it["sku_id"] == sku_id)
        baseline_demand_d = base_item["reconciled_demand_D"]

        # Step B: Inject high doorstep rejection (R = 0.650)
        await pg_conn.execute(
            "UPDATE cdao_replenishment_priors SET doorstep_rejection_factor_r = 0.650 WHERE sku_id = $1",
            sku_id,
        )
        rej_resp = await api_client.post(url)
        assert rej_resp.status_code == 200
        rej_body = rej_resp.json()

        save_captured_payload(
            "test_02_doorstep_rejection_decoupling",
            {
                "method": "POST",
                "url": url,
                "precondition": {"sku_id": sku_id, "doorstep_rejection_factor_r": 0.650, "baseline_D": baseline_demand_d},
                "body": None,
            },
            rej_resp.status_code,
            rej_body,
        )

        rej_item = next(it for it in rej_body["items"] if it["sku_id"] == sku_id)
        assert rej_item["rejection_factor_R"] == 0.65
        # Demand D(i, t+1) must NOT be lowered by R
        assert rej_item["reconciled_demand_D"] == baseline_demand_d

        # Procurement alert must be triggered
        alerts = [a for a in rej_body["procurement_alerts"] if a["sku_id"] == sku_id]
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "HIGH"
        assert alerts[0]["rejection_factor_r"] == 0.65
        assert "PROCUREMENT_ALERT" in alerts[0]["recommended_action"]
    finally:
        await pg_conn.execute(
            "UPDATE cdao_replenishment_priors SET doorstep_rejection_factor_r = 1.000 WHERE sku_id = $1",
            sku_id,
        )


@pytest.mark.asyncio
async def test_03_kitchen_capacity_category_splits(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Edge Case 3: Kitchen Capacity (K) Category Splits (Pull Engine)
    Force massive fresh overflow across leafy_greens, dairy, and base_veg.
    Assert the engine trims buffers iteratively starting strictly with leafy_greens -> dairy -> base_veg
    and guarantees both category caps and total K cap are satisfied.
    """
    url = "/jobs/store-pull?run_sync=true&delivery_date=2026-11-11"
    try:
        # Set high fresh demand & clear sound stock to induce ~110 kg overflow vs 35 kg cap
        await pg_conn.execute("UPDATE wms_inventory_batch SET current_qty = 0.000")
        await pg_conn.execute(
            "UPDATE cdao_replenishment_priors SET seed_daily_mean = 200.000, buffer_b = 0.100 WHERE sku_id = 'SKU-SPINACH-BUNCH'"
        )
        await pg_conn.execute(
            "UPDATE cdao_replenishment_priors SET seed_daily_mean = 250.000, buffer_b = 0.100 WHERE sku_id = 'SKU-MILK-1L'"
        )
        await pg_conn.execute(
            "UPDATE cdao_replenishment_priors SET seed_daily_mean = 300.000, buffer_b = 0.150 WHERE sku_id = 'SKU-TOMATO-HYBRID'"
        )
        # Keep other SKUs smaller so we isolate exact tiered shedding
        await pg_conn.execute(
            "UPDATE cdao_replenishment_priors SET seed_daily_mean = 10.000 WHERE sku_id IN ('SKU-CORIANDER-100G', 'SKU-PANEER-FRESH', 'SKU-POTATO-1KG')"
        )
        # Set strict category and total caps
        await pg_conn.execute("UPDATE cdao_kitchen_capacity SET daily_cap_kg = 8.000 WHERE category = 'leafy_greens'")
        await pg_conn.execute("UPDATE cdao_kitchen_capacity SET daily_cap_kg = 12.000 WHERE category = 'dairy'")
        await pg_conn.execute("UPDATE cdao_kitchen_capacity SET daily_cap_kg = 20.000 WHERE category = 'base_veg'")
        await pg_conn.execute("UPDATE cdao_kitchen_capacity SET daily_cap_kg = 35.000 WHERE category = 'total_fresh'")

        resp = await api_client.post(url)
        body = resp.json()

        save_captured_payload(
            "test_03_kitchen_capacity_category_splits",
            {
                "method": "POST",
                "url": url,
                "precondition": {
                    "SKU-SPINACH-BUNCH": "demand~200, buffer=10%",
                    "SKU-MILK-1L": "demand~250, buffer=10%",
                    "SKU-TOMATO-HYBRID": "demand~300, buffer=15%",
                    "caps": {"leafy_greens": 8.0, "dairy": 12.0, "base_veg": 20.0, "total_fresh": 35.0},
                },
                "body": None,
            },
            resp.status_code,
            body,
        )

        assert resp.status_code == 200
        assert body["overflow_cap_exceeded"] is True
        assert body["buffer_reduction_iterations"] > 0

        items_by_sku = {it["sku_id"]: it for it in body["items"]}
        # Leafy greens and dairy must be trimmed completely to 0% before base_veg finishes trimming
        assert items_by_sku["SKU-SPINACH-BUNCH"]["buffer_percentage_applied"] == 0.0
        assert items_by_sku["SKU-MILK-1L"]["buffer_percentage_applied"] == 0.0
        assert items_by_sku["SKU-TOMATO-HYBRID"]["buffer_percentage_applied"] < items_by_sku["SKU-TOMATO-HYBRID"]["initial_buffer_percentage"]

        # Verify all caps hold
        assert body["total_fresh_overflow_kg"] <= body["kitchen_capacity_cap_kg"]
        assert body["category_overflow_kg"]["leafy_greens"] <= body["category_kitchen_caps_kg"]["leafy_greens"]
        assert body["category_overflow_kg"]["dairy"] <= body["category_kitchen_caps_kg"]["dairy"]
        assert body["category_overflow_kg"]["base_veg"] <= body["category_kitchen_caps_kg"]["base_veg"]
    finally:
        await db.reset_seed_data()


@pytest.mark.asyncio
async def test_04_end_to_end_aging_exclusion_flow(api_client: httpx.AsyncClient, pg_conn: asyncpg.Connection):
    """
    Edge Case 4: End-to-End Data Flow (/jobs/fresh-routing -> /jobs/store-pull)
    Verify that aging produce (>48h) processed by /jobs/fresh-routing routes to
    ROUTE_TO_KITCHEN in PostgreSQL and is explicitly excluded from Sound_OnHand in /jobs/store-pull.
    """
    sku_id = "SKU-PANEER-FRESH"
    aged_batch_id = "TEST-BATCH-PANEER-52H"
    fresh_batch_id = "TEST-BATCH-PANEER-10H"
    try:
        # Remove existing paneer batches and insert controlled fresh (6 units, 10h) + aging (10 units, 52h)
        await pg_conn.execute("DELETE FROM wms_inventory_batch WHERE sku_id = $1", sku_id)
        await pg_conn.execute(
            """
            INSERT INTO wms_inventory_batch
            (batch_id, sku_id, received_qty, current_qty, is_fresh_produce, harvest_date, expiry_date, status)
            VALUES
            ($1, $3, 6.000, 6.000, TRUE, NOW() - INTERVAL '10 hours', NOW() + INTERVAL '62 hours', 'SELLABLE'),
            ($2, $3, 10.000, 10.000, TRUE, NOW() - INTERVAL '52 hours', NOW() + INTERVAL '20 hours', 'SELLABLE')
            """,
            fresh_batch_id,
            aged_batch_id,
            sku_id,
        )

        # Step 1: Trigger Fresh Routing
        route_url = "/jobs/fresh-routing?run_sync=true"
        route_resp = await api_client.post(route_url)
        route_body = route_resp.json()
        assert route_resp.status_code == 200

        routed_ids = [b["batch_id"] for b in route_body["routed_to_kitchen_items"]]
        sellable_ids = [b["batch_id"] for b in route_body["sellable_items"]]
        assert aged_batch_id in routed_ids
        assert fresh_batch_id in sellable_ids

        # Step 2: Confirm DB status updated in PostgreSQL
        db_status = await pg_conn.fetchval(
            "SELECT status FROM wms_inventory_batch WHERE batch_id = $1", aged_batch_id
        )
        assert db_status == "ROUTE_TO_KITCHEN"

        # Step 3: Trigger Store Pull and assert Sound_OnHand excludes the 10 aging units
        pull_url = "/jobs/store-pull?run_sync=true&delivery_date=2026-11-11"
        pull_resp = await api_client.post(pull_url)
        pull_body = pull_resp.json()
        assert pull_resp.status_code == 200

        paneer_item = next(it for it in pull_body["items"] if it["sku_id"] == sku_id)
        # Must strictly equal 6.0 (excluding the 10.0 aging units routed to kitchen)
        assert paneer_item["sound_on_hand"] == 6.0

        save_captured_payload(
            "test_04_end_to_end_aging_exclusion_flow",
            {
                "step_1": {"method": "POST", "url": route_url},
                "step_2": {"method": "POST", "url": pull_url},
                "precondition": {fresh_batch_id: "6 units @ 10h", aged_batch_id: "10 units @ 52h"},
            },
            pull_resp.status_code,
            {
                "fresh_routing_routed_to_kitchen": [
                    b for b in route_body["routed_to_kitchen_items"] if b["batch_id"] == aged_batch_id
                ],
                "store_pull_paneer_item": paneer_item,
            },
        )
    finally:
        await db.reset_seed_data()
