import os

# Weight tests must be deterministic: never let the live LLM fallback answer (an OpenRouter key is configured).
os.environ.setdefault("LIVE_WEIGHT_LLM_ENABLED", "false")
os.environ.setdefault("RECIPE1M_LLM_FALLBACK_ENABLED", "false")
# Tests must not depend on the developer .env (src modules call load_dotenv, which never overrides set values).
os.environ.setdefault("NUTRITION_MATCHER_MODE", "legacy")
os.environ.setdefault("NUTRITION_TOTALS_MODE", "legacy")
"""Test-process defaults for modules that construct database clients on import."""

import os


os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USERNAME", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-only")
