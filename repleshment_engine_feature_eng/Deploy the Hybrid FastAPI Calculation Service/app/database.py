"""
Database Module - Exports the active asynchronous PostgreSQL repository instance.
Reads DATABASE_URL from .env for local or Azure PostgreSQL Flexible Server connectivity.
"""

import os
from dotenv import load_dotenv
from app.repository.postgres import PostgresRepository

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://accesco:accesco_local_pass@localhost:5432/accesco_local"
)

# Global singleton repository wired to PostgreSQL
db = PostgresRepository(database_url=DATABASE_URL)
