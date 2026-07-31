#!/usr/bin/env python3
"""supabase_client.py: Shared Supabase client bootstrap.

fetch_offers.py, offer_parser.py, and migrate_to_supabase.py all write to the
same Supabase project at runtime using SUPABASE_URL + SUPABASE_KEY (the
service_role secret key -- RLS is enabled on all three tables with no
policies, so the anon/publishable key cannot write to them; these are
trusted backend scripts, not browser-facing code, so service_role is the
right key here and must never be exposed client-side).
"""

import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set (see .env.example). "
            "SUPABASE_KEY should be the service_role secret key from "
            "Project Settings -> API in the Supabase dashboard -- not the "
            "anon/publishable key, which RLS blocks from writing."
        )
    return create_client(url, key)
