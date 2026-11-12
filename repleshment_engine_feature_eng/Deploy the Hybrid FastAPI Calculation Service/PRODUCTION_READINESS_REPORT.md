# Accesco Hybrid Replenishment Engine: Production-Readiness & Delta Architecture Report

**Document ID:** ACC-REPL-PROD-REP-01  
**Project:** Accesco Quick Commerce Hybrid Replenishment Service (Phase 2 — v2.1.0)  
**Updated:** November 11, 2026  
**Status:** Validated & Production-Ready (Local PostgreSQL 16 Wired + Edge-Case Verified)  

---

## Executive Summary

Accesco is establishing a Quick Commerce replenishment engine bridging **Argha's mathematical models** (*Pull Theory*, *Household Consumption Engine*, and *No-Expiry Engine*) with the enterprise **CDAO Architecture** (*PostgreSQL Flexible Server* operational source of truth + *Azure Databricks Delta Lakehouse*).

To resolve the **Day-1 cold start problem** (where SKU/inventory tables exist without historical linked customer orders), Argha's parametric formulas provide deterministic replenishment calculations immediately from Day 1 while building clean historical datasets for Month-2 ML handoff (*XGBoost Survival AFT* and *TimesFM 3.0*).

This updated report documents the complete evolution from the initial skeleton codebase through the v2.0 in-memory prototype to the **v2.1 PostgreSQL-backed, edge-case-verified service**.

---

## 1. What Was There vs. What Was Added (Delta Breakdown)

| File / Component | Initial Baseline State | Current Production-Ready State (v2.1.0) |
|---|---|---|
| **`app/models/schemas.py`** | Empty file (0 bytes). | Pydantic v2 schemas: `ProductCategory` (`leafy_greens`, `dairy`, `base_veg`, `fmcg`), `SKU`, `Batch`, `RecipeBOMItem`, `Household`, `CustomerOrder`, `CatchmentPrior`, `ProcurementAlert`, `NudgeAuditRecord`, and `PurchaseOrderProposal`. |
| **`app/repository/` (DAL)** | Did not exist. | **Pluggable Async Repository Pattern**: `base.py` (`BaseRepository` async interface), `postgres.py` (`PostgresRepository` using `SQLAlchemy Async` + `asyncpg`), and `in_memory.py` (`InMemoryRepository` with `RLock` fallback). |
| **`app/database.py`** | Empty file (0 bytes). | Loads `.env` (`DATABASE_URL`) and exports the active `PostgresRepository` singleton `db`. |
| **`sql/` (Phase 1 DDL/DML)** | Did not exist. | `01_schema_ddl.sql` (10 `TIMESTAMPTZ` tables), `02_seed_dml.sql` (Rajajinagar ICP priors, BOM, calendar factors, batches, households), and `apply_schema.py` idempotent setup script. |
| **`app/core/no_expiry.py`** | Empty file (0 bytes). | Async **Job A**: packaged life fraction $L(\text{batch}, t)$ with FSSAI floor ($\ge 30\%$ life and $\ge 45\text{d}$ remaining), 48h/72h fresh harvest limits, UTC `TIMESTAMPTZ` accuracy, and FEFO sorting. |
| **`app/core/hce_engine.py`** | Empty file (0 bytes). | Async **Job B**: Bayesian shrinkage $r(h, i)$ with 1p/2+p priors, low-stock day $T_{\text{low}}$ ($\theta=0.20$), Gamma CDF reorder probability (paused when dormant $>7\text{d}$), 8-Gate CDAO safety check, immutable audit logging (`cdao_nudge_audit_log`), and bottom-up $E_h$ persisted to PostgreSQL. |
| **`app/core/pull_theory.py`** | Empty file (0 bytes). | Async **Job C**: PostgreSQL calendar constants $c(w)$ & $M(t)$, HCE cross-check ($>15\%$ divergence with uninitialized-$E_h$ guard), **decoupled** doorstep rejection $R(i)$ emitting `ProcurementAlert`s, category & total kitchen caps $K$ (`leafy_greens` $\to$ `dairy` $\to$ `base_veg` down to $0\%$), case pack/MOQ rounding, and PO persistence in `PENDING_APPROVAL`. |
| **`app/scheduler.py`** | Empty file (0 bytes). | Multi-loop safe `APScheduler` (`AsyncIOScheduler` instantiated dynamically inside `start_scheduler()`). |
| **`app/main.py`** | 55 lines of basic stubs. | Async FastAPI v2.1.0 with `CorrelationIdMiddleware`, RFC 7807 error handling, UTC ISO-8601 health/error timestamps, warehouse PO approval/override/rejection endpoints, kill-switch toggle, and PostgreSQL `/data/*` inspection & reset routes. |
| **`docker-compose.yml`, `.env`, `.env.example`** | Did not exist. | Local PostgreSQL 16 Docker Compose service + environment configuration template. |
| **`Dockerfile`, `.dockerignore`, `.gitignore`** | Empty `Dockerfile` (0 bytes). | Hardened `python:3.12-slim` container running as non-root `appuser` with `/health` `HEALTHCHECK`. |
| **`test_case/` & `test_service.py`** | Did not exist. | `test_case/test_api.py` (`pytest` + `httpx` edge-case suite), `test_case/API_Test_Execution_Report.md`, `test_case/captured_payloads.json`, `pytest.ini`, and `test_service.py` (10-point live audit). |

---

## 2. Mathematical Formulations & Edge-Case Refinements

### Job A: No-Expiry & Fresh Routing (`app/core/no_expiry.py`)
1. **Packaged Goods Lifespan Fraction:**
   $$L(\text{batch}, t) = \frac{\text{expiry} - t}{\text{expiry} - \text{manufacture}}$$
   - **$L \ge 0.50$ and $\text{days\_remaining} \ge 45$:** `SELLABLE` (FEFO sorted).
   - **$0.30 \le L < 0.50$ or $\text{days\_remaining} < 45$ (if $L \ge 0.30$):** `ROUTE_TO_KITCHEN`.
   - **$L < 0.30$:** `COMPOST`.
2. **Fresh Produce Harvest Elapsed Time ($h$):**
   - **$h \le 48\text{h}$:** `SELLABLE`. **$48\text{h} < h \le 72\text{h}$:** `ROUTE_TO_KITCHEN`. **$h > 72\text{h}$:** `COMPOST`.
   - Uses UTC `TIMESTAMPTZ` comparison (`datetime.now(timezone.utc)`) to prevent timezone skew between container DB and host runtime.

### Job B: Household Consumption Engine (`app/core/hce_engine.py`)
1. **Bayesian Shrinkage Consumption Rate ($r(h, i)$):**
   $$r(h, i) = \frac{k \cdot \rho_{\text{1p/2p}}(i) + \sum_{j=1}^{n-1} q_j}{k + (d_n - d_1)}$$
   ($k = 14\text{ days}$ for fresh produce, $k = 30\text{ days}$ for staples).
2. **Expected Low-Stock Day ($T_{\text{low}}$):**
   $$T_{\text{low}}(h, i) = d_n + \frac{q_n \times (1 - \theta)}{r(h, i)}, \quad \theta = 0.20$$
3. **Gamma CDF Reorder Probability:**
   $$P_{\text{reorder}}(h, i, t) = \text{Gamma\_cdf}\left(t - d_n;\; \alpha = \frac{\mu^2}{\sigma^2},\; \beta = \frac{\sigma^2}{\mu}\right)$$
   Paused (`p_reorder = 0.0`) and blocked (`BLOCKED_DORMANT`) if household inactivity exceeds 7 days.
4. **Bottom-Up Demand Aggregation:**
   $$E_h(i, t+1) = \sum_{h} P_{\text{reorder}}(h, i, t+1) \times q_{\text{typical}}(h, i)$$

### Job C: Store Pull Replenishment (`app/core/pull_theory.py`)
1. **Multiplicative Demand Forecast (Doorstep Rejection Decoupled):**
   $$D(i, t+1) = D_{\text{base}} \times c(w) \times F(t+1) \times M(t+1)$$
   - **$R(i)$ Decoupling:** High doorstep rejections ($R(i) < 0.95$) do **not** scale down $D(i, t+1)$; instead, they emit a `ProcurementAlert` (`MEDIUM` or `HIGH`) instructing procurement to change the farm/supplier lot.
2. **HCE Cross-Check:**
   - When `E_h` has been computed by a prior HCE run and diverges from $A(i, w)$ by $> 15\%$: uses $\max(A, E_h)$ for fresh items and $\text{mean}(A, E_h)$ for `fmcg`.
3. **Swadishtt Kitchen Capacity Constraint ($K_{\text{cap}}$):**
   $$\sum_{i \in \text{Fresh}} \text{Overflow}_i \le K_{\text{total}}, \quad \text{and} \quad \forall c,\; \text{Overflow}_c \le K_c$$
   Under overflow, buffers trim by $1\%$ per step strictly in order **`leafy_greens` $\to$ `dairy` $\to$ `base_veg`** down to **$0\%$**, guaranteeing both category limits (`8 kg`, `12 kg`, `20 kg`) and total limit (`35 kg`) hold.
4. **Inventory Math:**
   - Case pack ceiling $\lceil \frac{\text{Raw\_Order}}{\text{case\_pack}} \rceil \times \text{case\_pack}$ and $\text{MOQ}$ enforcement.

---

## 3. End-to-End Architecture

```mermaid
flowchart TD
    subgraph DB["PostgreSQL 16 Operational & CDAO Store"]
        PG[(wms_inventory_batch, oms_recipe_bom,<br/>oms_households, oms_customer_purchase_log,<br/>cdao_replenishment_priors, cdao_calendar_factors,<br/>cdao_kitchen_capacity, cdao_system_config,<br/>cdao_po_proposals, cdao_nudge_audit_log)]
    end

    subgraph S1["1. Daytime Shelf Routing (Job A)"]
        PG --> NOEX["No-Expiry Evaluation (UTC TIMESTAMPTZ)"]
        NOEX -->|L >= 0.50 & >= 45d / <= 48h| SELL["SELLABLE (FEFO Sound Stock)"]
        NOEX -->|0.30 <= L < 0.50 or 48h-72h| KIT["ROUTE_TO_KITCHEN (Swadishtt)"]
        NOEX -->|L < 0.30 or > 72h| COMP["COMPOST"]
        SELL & KIT & COMP -->|Persist Status| PG
    end

    subgraph S2["2. Evening Customer Engine (Job B)"]
        PG --> HCE["Bayesian Shrinkage & Gamma CDF"]
        HCE --> GATE{"CDAO 8-Gate Safety Check"}
        GATE -->|Kill-Switch / Dormant >7d / Opt-Out / Stockout / Cart / Quiet / Cooldown / Cap| SUPP["SUPPRESSED -> cdao_nudge_audit_log"]
        GATE -->|Passed & Candidate| NUDGE["NUDGE_QUEUED -> cdao_nudge_audit_log"]
        HCE --> AGG["Persist E_h(i, t+1) -> cdao_system_config"]
    end

    subgraph S3["3. Nightly Store Pull (Job C)"]
        PG --> PULL["Nightly Pull (9:15 PM)"]
        PULL -->|R(i) < 0.95| ALERT["Emit ProcurementAlert (Demand Unpenalized)"]
        PULL --> KC{"Kitchen Caps (Total + Category)"}
        KC -->|Trim leafy_greens -> dairy -> base_veg to 0%| MOQ["Case Pack & MOQ Rounding"]
        KC -->|Within Caps| MOQ
        MOQ --> DRAFT["Save PO Proposal (PENDING_APPROVAL)"]
    end

    subgraph S4["4. Warehouse Staff Governance"]
        DRAFT --> ADMIN{"Warehouse Review APIs"}
        ADMIN -->|POST /approve| APPR["APPROVED"]
        ADMIN -->|POST /override| OVER["OVERRIDDEN (With Rationale)"]
        ADMIN -->|POST /reject| REJ["REJECTED (With Reason)"]
    end
```

---

## 4. Verification Results

* **PostgreSQL Edge-Case Integration Suite (`pytest -v test_case/test_api.py`):** **5 / 5 PASSED** (verified both via ASGI transport and live `uvicorn` HTTP server). Full JSON payloads and bug-fix audit recorded in [`test_case/API_Test_Execution_Report.md`](test_case/API_Test_Execution_Report.md).
* **10-Point Production Audit (`python test_service.py`):** **10 / 10 PASSED** against live `uvicorn` + PostgreSQL connection pool (including 30 parallel concurrent requests).

---

## 5. Month 2+ Azure & Databricks Roadmap

1. **Azure Deployment:** Swap local `.env` `DATABASE_URL` to Azure Database for PostgreSQL Flexible Server (`ssl=require`) and deploy [`Dockerfile`](Dockerfile) to Azure Container Apps.
2. **Databricks Lakehouse & MLflow Integration:** Ingest `oms_customer_purchase_log`, `cdao_nudge_audit_log`, and `cdao_po_proposals` incrementally into ADLS Gen2 / Delta Lake (Bronze $\to$ Silver $\to$ Gold) to train and shadow-benchmark `XGBoost Survival AFT` and `TimesFM 3.0` against Argha's Day-1 parametric engines.
