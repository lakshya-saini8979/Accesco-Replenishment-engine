# Accesco Hybrid Replenishment Calculation Service (v2.1.0)

An asynchronous, containerized **Python / FastAPI** microservice backed by **PostgreSQL** (`SQLAlchemy Async` + `asyncpg`) implementing Argha's mathematical replenishment models with enterprise CDAO governance:

1. **Daytime No-Expiry & Fresh Routing Engine (`app/core/no_expiry.py`):** Evaluates packaged lifespan fraction $L(\text{batch}, t)$ with FSSAI floor ($\ge 30\%$ life and $\ge 45$ days remaining), fresh produce harvest hours ($\le 48\text{h}$ sellable, $48\text{h}-72\text{h}$ Swadishtt kitchen, $> 72\text{h}$ compost), and FEFO picking priority.
2. **Evening Household Consumption Engine (`app/core/hce_engine.py`):** Computes household consumption rate $r(h, i)$ via Bayesian Shrinkage (1p/2+p catchment priors), low-stock day $T_{\text{low}}$ ($\theta = 0.20$), Gamma CDF reorder probability (paused when dormant $>7$ days), CDAO 8-Gate Safety checks, immutable audit logging, and bottom-up aggregate demand $E_h(i, t+1)$.
3. **Nightly Store Pull Replenishment Engine (`app/core/pull_theory.py`):** Applies PostgreSQL-backed weekday $c(w)$ and month-cycle $M(t)$ multipliers, reconciles with $E_h$ ($>15\%$ divergence), **decouples** doorstep rejection factor $R(i)$ into `ProcurementAlert`s without suppressing true demand $D(i, t+1)$, enforces category-level and aggregate Swadishtt kitchen capacity limits ($K_{\text{cap}}$) in strict priority order (`leafy_greens` $\to$ `dairy` $\to$ `base_veg` down to $0\%$), and rounds to supplier case packs and MOQs.
4. **Warehouse Staff Approval Workflow (`app/main.py`):** Persists PO proposals in `PENDING_APPROVAL` status with dedicated `approve`, `override` (with mandatory rationale), and `reject` endpoints.
5. **Observability & Governance:** `X-Correlation-ID` tracing middleware, RFC 7807 error payloads, UTC `TIMESTAMPTZ` alignment, emergency `/governance/kill-switch`, and immutable `/audit/nudges` ledger.

---

## Project Structure

```text
accesco-replenishment-engine/
├── app/
│   ├── main.py                         # FastAPI routes, CorrelationIdMiddleware & RFC 7807 errors
│   ├── database.py                     # PostgreSQL repository singleton wired from .env
│   ├── scheduler.py                    # Multi-loop safe APScheduler cron jobs
│   ├── repository/
│   │   ├── base.py                     # Abstract async BaseRepository interface
│   │   ├── postgres.py                 # SQLAlchemy Async + asyncpg PostgreSQL adapter
│   │   └── in_memory.py                # Thread-safe in-memory adapter (RLock fallback)
│   ├── core/
│   │   ├── no_expiry.py                # Job A: L(batch), FSSAI floor & kitchen routing
│   │   ├── hce_engine.py               # Job B: Bayesian shrinkage, Gamma CDF & 8-Gate safety
│   │   └── pull_theory.py              # Job C: Calendar factors, R(i) decoupling, K-cap & MOQ
│   └── models/
│       └── schemas.py                  # Pydantic v2 domain, PO & audit schemas
├── sql/
│   ├── 01_schema_ddl.sql               # PostgreSQL TIMESTAMPTZ schema (Phase 1 + operational tables)
│   ├── 02_seed_dml.sql                 # Rajajinagar ICP seed data, BOM & calendar factors
│   └── apply_schema.py                 # Idempotent asyncpg schema/seed runner
├── test_case/
│   ├── test_api.py                     # Pytest + httpx edge-case integration test suite
│   ├── captured_payloads.json          # Exact captured JSON request/response payloads
│   └── API_Test_Execution_Report.md    # Local PostgreSQL test & bug fix report
├── test_service.py                     # 10-Point end-to-end verification suite
├── docker-compose.yml                  # Local PostgreSQL 16 container definition
├── Dockerfile                          # Hardened Python 3.12-slim container
├── .env.example                        # Environment configuration template
├── pytest.ini                          # Pytest-asyncio configuration
└── requirements.txt                    # Python dependencies
```

---

## Local Setup & Quickstart

### 1. Configure Environment Variables
Copy `.env.example` to `.env` (already pre-configured for local Docker Compose):
```bash
cp .env.example .env
```

### 2. Start Local PostgreSQL 16
```bash
docker compose up -d
```

### 3. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 4. Apply Schema & Seed Data (Phase 1 DDL/DML)
Wait until `accesco_postgres_local` reports `healthy`, then run:
```bash
python sql/apply_schema.py
```

### 5. Run the FastAPI Server
```bash
uvicorn app.main:app --reload --port 8000
```
Open interactive Swagger API documentation at: **[http://localhost:8000/docs](http://localhost:8000/docs)**

---

## Testing & Verification

### Edge-Case Integration Suite (`pytest` + `httpx` + PostgreSQL)
Validates the 7-day dormancy rule, doorstep rejection decoupling, category-level kitchen capacity buffer trimming (`leafy_greens` $\to$ `dairy` $\to$ `base_veg`), and end-to-end aging exclusion:
```bash
pytest -v test_case/test_api.py
```
To test against a live running `uvicorn` instance instead of ASGI transport:
```powershell
$env:API_BASE_URL="http://127.0.0.1:8000"
pytest -v test_case/test_api.py
```

### 10-Point End-to-End Audit Script
```bash
python test_service.py
```

See full recorded JSON request/response traces and bug fixes in [`test_case/API_Test_Execution_Report.md`](test_case/API_Test_Execution_Report.md).

---

## API Endpoints

### Core Replenishment Jobs
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/jobs/fresh-routing?run_sync=true` | Evaluates shelf batches ($\le 48\text{h}$ sellable, $48-72\text{h}$ kitchen, $>72\text{h}$ compost; $L \ge 0.50$ & $\ge 45\text{d}$ FSSAI floor) and updates `wms_inventory_batch` |
| `POST` | `/jobs/hce-nudge?run_sync=true` | Runs Bayesian shrinkage $r(h, i)$, evaluates 8 CDAO gates, logs to `cdao_nudge_audit_log`, computes bottom-up $E_h$ |
| `POST` | `/jobs/store-pull?run_sync=true` | Reconciles demand with $E_h$, decouples $R(i)$ into `ProcurementAlert`s, caps overflow at category/total $K_{\text{cap}}$, applies case pack & MOQ, saves PO in `cdao_po_proposals` |
| `POST` | `/jobs/run-all-e2e` | Executes the complete daily cycle (Job A $\to$ Job B $\to$ Job C) in sequence |

### Warehouse Staff Approval Workflow (CDAO Mandate)
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/po/proposals` | List all PO proposals and approval statuses |
| `GET` | `/po/proposals/{po_id}` | Retrieve specific PO proposal |
| `POST` | `/po/proposals/{po_id}/approve` | Warehouse staff marks PO as `APPROVED` |
| `POST` | `/po/proposals/{po_id}/override` | Warehouse staff overrides item quantities with mandatory reason |
| `POST` | `/po/proposals/{po_id}/reject` | Warehouse staff rejects PO proposal with explanation |

### Phase 1 Ingestion & Phase 4 Frontend Endpoints
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events/delivery-confirmed` | Webhook for rider delivery acceptance; depletion clock begins at delivery time $d_n$ |
| `POST` | `/events/doorstep-accepted` | Alias webhook for doorstep delivery confirmation |
| `GET` | `/nudges/wardrobe/{household_id}` | Frontend Wardrobe UI structured JSON payload (1–2 days prior to $T_{low}$) |
| `GET` | `/frontend/wardrobe/{household_id}` | Alias endpoint for frontend Wardrobe UI |

### Phase 5 Customer Feedback Loop (Self-Correction)
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/feedback/reorder` | Logs successful prediction, records reorder order event |
| `POST` | `/feedback/bought-elsewhere` | Resets virtual stock to 100% full; strictly preserves consumption rate $r$ without shrinkage |
| `POST` | `/feedback/still-have-plenty` | Mathematically lowers daily consumption rate $r(h,i)$ to delay next nudge cycle |
| `POST` | `/feedback/dismiss` | Dismisses nudge and enters 3-day cooldown period |

### Governance & Data Inspection
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | PostgreSQL connectivity, kill-switch status, and active capacity parameters |
| `GET` | `/audit/nudges` | Immutable audit ledger of customer reminder evaluations |
| `POST` | `/governance/kill-switch` | Instant stop-send toggle (persisted in `cdao_system_config`) |
| `GET` | `/data/inventory` | Live `wms_inventory_batch` rows and `SELLABLE` sound on-hand stock |
| `GET` | `/data/households` | Household profiles, dormancy, opt-outs, and cart flags |
| `GET` | `/data/skus` | SKU catalog, catchment priors, and `oms_recipe_bom` |
| `POST` | `/data/reset` | Re-applies `sql/02_seed_dml.sql` against PostgreSQL |
