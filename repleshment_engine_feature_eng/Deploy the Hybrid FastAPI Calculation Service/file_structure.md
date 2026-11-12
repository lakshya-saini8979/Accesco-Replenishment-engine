accesco-replenishment-engine/
│
├── app/
│   ├── main.py                         # FastAPI app, routes, middleware & RFC 7807 errors
│   ├── database.py                     # PostgreSQL asyncpg repository binding
│   ├── scheduler.py                    # Multi-loop safe APScheduler cron configuration
│   │
│   ├── repository/                     # Pluggable Data Access Layer
│   │   ├── __init__.py
│   │   ├── base.py                     # Abstract BaseRepository interface
│   │   ├── postgres.py                 # SQLAlchemy Async + asyncpg PostgreSQL adapter
│   │   └── in_memory.py                # Thread-safe in-memory adapter (RLock fallback)
│   │
│   ├── core/
│   │   ├── no_expiry.py                # Job A: L(batch), FSSAI floor & FEFO kitchen routing
│   │   ├── hce_engine.py               # Job B: Bayesian shrinkage, Gamma CDF & 8-Gate CDAO safety
│   │   └── pull_theory.py              # Job C: Calendar factors, R(i) decoupling, K-cap & MOQ
│   │
│   └── models/
│       └── schemas.py                  # Pydantic v2 domain, PO approval & audit schemas
│
├── sql/
│   ├── 01_schema_ddl.sql               # Phase 1 + operational PostgreSQL TIMESTAMPTZ schema
│   ├── 02_seed_dml.sql                 # Day-1 Rajajinagar ICP seed data & calendar factors
│   └── apply_schema.py                 # Idempotent asyncpg schema & seed applier
│
├── test_case/
│   ├── test_api.py                     # Pytest + httpx edge-case integration test suite
│   ├── test_hce_5phases.py             # Strict 5-Phase HCE Architecture verification suite
│   ├── captured_payloads.json          # Exact JSON request/response traces from test runs
│   └── API_Test_Execution_Report.md    # Local DB status, payloads & bug fix audit report
│
├── test_service.py                     # 10-Point end-to-end production-readiness audit
├── HCE_Phase_Implementation_Audit.md   # Comprehensive 5-Phase HCE audit & verification report
├── docker-compose.yml                  # Local PostgreSQL 16 service definition
├── Dockerfile                          # Hardened Python 3.12-slim production container
├── .env                                # Local environment variables (git-ignored)
├── .env.example                        # Environment variable template
├── .gitignore                          # Git exclusions
├── .dockerignore                       # Docker build exclusions
├── pytest.ini                          # Pytest-asyncio strict configuration
├── requirements.txt                    # Python dependencies (FastAPI, asyncpg, scipy, pytest)
├── PRODUCTION_READINESS_REPORT.md      # Full architecture & delta report
└── README.md                           # Setup, formulas, endpoints & testing guide
