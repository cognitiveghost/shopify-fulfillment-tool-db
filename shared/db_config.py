"""Where the trial Supabase project lives. The publishable key is public by design (RLS guards the data)."""
import os

SUPABASE_URL = os.environ.get("FULFILLMENT_DB_URL", "https://ntzqarcksthplcdzivep.supabase.co")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("FULFILLMENT_DB_KEY", "sb_publishable_rVn_-LK-kpNkYCHNz4ilOA_a_W8SPP2")
