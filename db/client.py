"""
db/client.py

Supabase client singleton.
Always use the service key on the backend — never the publishable/anon key.
"""

import os
from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    """
    Return a cached Supabase client.
    Reads SUPABASE_URL and SUPABASE_SERVICE_KEY from environment.
    """
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
            )
        _client = create_client(url, key)
    return _client