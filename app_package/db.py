import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_db_connection():
    """Establishes and returns a connection to the Neon PostgreSQL database."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is missing.")
    return psycopg2.connect(db_url, cursor_factory=RealDictCursor)
