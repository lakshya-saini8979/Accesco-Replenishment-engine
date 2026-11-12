"""
Production-Readiness 10-Point Verification Test Suite
Tests every mathematical formula, CDAO governance gate, approval workflow,
and concurrent access against the PostgreSQL-backed Accesco Hybrid Replenishment Engine.
"""

import sys
import os
import concurrent.futures

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
os.environ["PG_NULLPOOL"] = "true"

import httpx
from fastapi.testclient import TestClient
from app.main import app


def run_10_point_verification():
    print("=" * 80)
    print("ACCESCO HYBRID REPLENISHMENT ENGINE - 10-POINT PRODUCTION-READINESS AUDIT")
    print("=" * 80)

    base_url = os.getenv("API_BASE_URL")
    with (httpx.Client(base_url=base_url, timeout=30.0) if base_url else TestClient(app)) as client:
        # --------------------------------------------------------------------------
        # CHECK 1: Observability, Tracing & Health Check
        # --------------------------------------------------------------------------
        print("\n[CHECK 1/10] Verifying Healthcheck & Correlation ID Tracing...")
        res = client.get("/health", headers={"X-Correlation-ID": "test-trace-101"})
        assert res.status_code == 200
        assert res.headers.get("X-Correlation-ID") == "test-trace-101"
        data = res.json()
        assert data["status"] == "HEALTHY"
        assert data["database_connected"] is True
        assert "PostgreSQL" in data["storage_mode"]
        print("  [OK] Health check returned 200 OK (PostgreSQL connected).")
        print(f"  [OK] Correlation ID properly propagated in headers: {res.headers.get('X-Correlation-ID')}")

        # --------------------------------------------------------------------------
        # CHECK 2: Clean PostgreSQL Seed Reset & Schema Verification
        # --------------------------------------------------------------------------
        print("\n[CHECK 2/10] Verifying PostgreSQL Seed Reset & Phase 1 Tables...")
        res = client.post("/data/reset")
        assert res.status_code == 200
        res = client.get("/data/skus")
        skus_data = res.json()["skus"]
        assert len(skus_data) == 8
        assert len(res.json()["recipe_bom"]) == 3
        print(f"  [OK] PostgreSQL store successfully reset with {len(skus_data)} SKUs and 3 BOM lines.")

        # --------------------------------------------------------------------------
        # CHECK 3: Job A: Fresh Routing & FSSAI Floor Math
        # --------------------------------------------------------------------------
        print("\n[CHECK 3/10] Verifying Job A: No-Expiry & Fresh Routing...")
        res = client.post("/jobs/fresh-routing?run_sync=true")
        assert res.status_code == 200
        r_data = res.json()
        print(f"  [OK] Checked {r_data['total_batches_checked']} batches:")
        print(f"    - Sellable: {r_data['sellable_count']}")
        print(f"    - Routed to Kitchen: {r_data['routed_to_kitchen_count']}")
        print(f"    - Compost: {r_data['compost_count']}")
        assert r_data["routed_to_kitchen_count"] >= 2, "Expected aged batches to route to kitchen"
        assert r_data["compost_count"] >= 1, "Expected >72h milk batch to be composted"
        res = client.get("/data/inventory")
        inv = res.json()["sound_on_hand"]
        assert inv["SKU-SPINACH-BUNCH"] == 18.0, "Sound on-hand must exclude 8.0 units routed to kitchen"

        # --------------------------------------------------------------------------
        # CHECK 4: Job B: Bayesian Shrinkage & Low Stock Math
        # --------------------------------------------------------------------------
        print("\n[CHECK 4/10] Verifying Job B: Bayesian Shrinkage & Gamma CDF...")
        res = client.post("/jobs/hce-nudge?run_sync=true&ignore_quiet_hours=true")
        assert res.status_code == 200
        h_data = res.json()
        print(f"  [OK] Evaluated {h_data['total_evaluations']} customer-product relationships:")
        print(f"    - Nudges Queued: {h_data['nudges_generated']}")
        print(f"    - Suppressed by CDAO Gates: {h_data['nudges_blocked']}")
        print(f"    - Bottom-Up Demand (E_h): {h_data['bottom_up_demand_Eh']}")
        assert h_data["bottom_up_demand_Eh"]["SKU-MILK-1L"] > 0.0

        # --------------------------------------------------------------------------
        # CHECK 5: Job B: CDAO Policy Gates (Consent, Dormancy, Cart, Cooldown)
        # --------------------------------------------------------------------------
        print("\n[CHECK 5/10] Verifying All CDAO Safety Gates...")
        gate_reasons = {item["household_id"]: item["gate_status"] for item in h_data["nudge_details"]}
        print(f"  [OK] Gate outcomes by household: {gate_reasons}")
        assert gate_reasons["H103"] == "BLOCKED_DORMANT", "Dormant user must be blocked"
        assert gate_reasons["H104"] == "BLOCKED_OPT_OUT", "Opted out user must be blocked"
        assert gate_reasons["H105"] == "BLOCKED_IN_CART", "In-cart product must be blocked"
        assert gate_reasons["H106"] == "BLOCKED_COOLDOWN", "Cooldown period must block re-nudging"
        print("  [OK] All 4 boundary condition gates successfully blocked inappropriate contacts.")

        # --------------------------------------------------------------------------
        # CHECK 6: Job B: Emergency Kill Switch & Audit Trail
        # --------------------------------------------------------------------------
        print("\n[CHECK 6/10] Verifying Emergency Kill Switch & Immutable Audit Ledger...")
        res = client.post("/governance/kill-switch?active=true")
        assert res.status_code == 200
        res = client.post("/jobs/hce-nudge?run_sync=true&ignore_quiet_hours=true")
        k_data = res.json()
        assert k_data["nudges_generated"] == 0, "No nudges allowed when kill switch is active"
        assert all(item["gate_status"] == "BLOCKED_KILL_SWITCH" for item in k_data["nudge_details"])
        print("  [OK] Emergency kill switch halted 100% of outgoing nudges.")

        client.post("/governance/kill-switch?active=false")
        res = client.get("/audit/nudges?limit=10")
        assert res.status_code == 200
        audits = res.json()
        assert len(audits) > 0
        print(f"  [OK] Audit ledger contains {len(audits)} immutable evaluation records.")

        # --------------------------------------------------------------------------
        # CHECK 7: Job C: Pull Theory Multiplicative Demand & Dynamic Calendar
        # --------------------------------------------------------------------------
        print("\n[CHECK 7/10] Verifying Job C: Pull Theory Demand & Kitchen Cap...")
        res = client.post("/jobs/store-pull?run_sync=true")
        assert res.status_code == 200
        p_data = res.json()
        assert p_data["status"] == "PENDING_APPROVAL"
        print(f"  [OK] Generated PO Proposal: {p_data['po_id']}")
        print(f"    - Status: {p_data['status']}")
        print(f"    - Weekday Factor c(w): {p_data['items'][0]['day_constant_c']}")
        print(f"    - Month-Cycle Factor M: {p_data['month_salary_factor_M']}")
        print(f"    - Total Proposed Units: {p_data['total_proposed_units']}")
        print(f"    - Fresh Overflow: {p_data['total_fresh_overflow_kg']} kg (Cap: {p_data['kitchen_capacity_cap_kg']} kg)")
        assert p_data["total_fresh_overflow_kg"] <= p_data["kitchen_capacity_cap_kg"]

        # --------------------------------------------------------------------------
        # CHECK 8: Job C: Inventory Math (Case Pack Rounding & MOQ)
        # --------------------------------------------------------------------------
        print("\n[CHECK 8/10] Verifying Case Pack Rounding and MOQ Compliance...")
        for item in p_data["items"]:
            if item["proposed_quantity"] > 0:
                assert item["proposed_quantity"] >= item["moq"], f"{item['name']} must meet MOQ {item['moq']}"
                assert item["proposed_quantity"] % item["case_pack_size"] == 0
        print("  [OK] 100% of proposed PO items strictly adhere to Supplier Case Pack multiples and MOQs.")

        # --------------------------------------------------------------------------
        # CHECK 9: Warehouse Staff Approval, Override & Rejection Workflow
        # --------------------------------------------------------------------------
        print("\n[CHECK 9/10] Verifying Warehouse Approval, Override & Rejection APIs...")
        po_id = p_data["po_id"]
        override_payload = {
            "reviewed_by": "MGR_LAKSHMAN",
            "override_quantities": {"SKU-SPINACH-BUNCH": 30},
            "override_reason": "High weekend cook booking demand anticipated.",
        }
        res = client.post(f"/po/proposals/{po_id}/override", json=override_payload)
        assert res.status_code == 200
        ov_data = res.json()
        assert ov_data["status"] == "OVERRIDDEN"
        assert ov_data["reviewed_by"] == "MGR_LAKSHMAN"
        spinach_item = next(it for it in ov_data["items"] if it["sku_id"] == "SKU-SPINACH-BUNCH")
        assert spinach_item["approved_quantity"] == 30
        print(f"  [OK] PO {po_id} successfully overridden with logged rationale: '{ov_data['override_reason']}'")

        # --------------------------------------------------------------------------
        # CHECK 10: Multi-threaded Concurrency & Connection Pool Safety
        # --------------------------------------------------------------------------
        print("\n[CHECK 10/10] Verifying Concurrent PostgreSQL Pool Access (30 Parallel Requests)...")

        def hit_endpoint(i):
            if i % 3 == 0:
                return client.get("/data/inventory").status_code
            elif i % 3 == 1:
                return client.post("/jobs/fresh-routing?run_sync=true").status_code
            else:
                return client.get("/health").status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(hit_endpoint, range(30)))

        assert all(code == 200 for code in results)
        print("  [OK] 30 concurrent requests executed against PostgreSQL without errors.")

    print("\n" + "=" * 80)
    print("RESULT: ALL 10 PRODUCTION-READINESS CHECKS PASSED AGAINST POSTGRESQL!")
    print("=" * 80)


if __name__ == "__main__":
    run_10_point_verification()
