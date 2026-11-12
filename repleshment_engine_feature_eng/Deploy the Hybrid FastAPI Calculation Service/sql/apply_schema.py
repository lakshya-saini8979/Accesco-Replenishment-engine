"""
Schema & Seed Applier Script
Reads DATABASE_URL from .env and executes Phase 1 SQL DDL and DML scripts
idempotently against the local PostgreSQL database.
"""

import asyncio
import os
from pathlib import Path
import asyncpg
from dotenv import load_dotenv


async def apply():
    load_dotenv()
    raw_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://accesco:accesco_local_pass@localhost:5432/accesco_local",
    )
    dsn = raw_url.replace("postgresql+asyncpg://", "postgresql://")
    sql_dir = Path(__file__).resolve().parent

    ddl_sql = (sql_dir / "01_schema_ddl.sql").read_text(encoding="utf-8")
    dml_sql = (sql_dir / "02_seed_dml.sql").read_text(encoding="utf-8")

    conn = await asyncpg.connect(dsn)
    try:
        print("Connected to PostgreSQL:", dsn.split("@")[-1])
        await conn.execute(ddl_sql)
        print("Applied 01_schema_ddl.sql successfully.")
        await conn.execute(dml_sql)
        print("Applied 02_seed_dml.sql successfully.")

        tables = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )
        print("Active tables:", [r["table_name"] for r in tables])
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(apply())
