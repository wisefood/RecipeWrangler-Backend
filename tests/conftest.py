import os

# Weight tests must be deterministic: never let the live LLM fallback answer (an OpenRouter key is configured).
os.environ.setdefault("LIVE_WEIGHT_LLM_ENABLED", "false")
os.environ.setdefault("RECIPE1M_LLM_FALLBACK_ENABLED", "false")
"""Test-process defaults for modules that construct database clients on import."""

import os


os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USERNAME", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-only")
