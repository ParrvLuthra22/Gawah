"""Run the Guardian's semantic (Groq) category check once on a matching item and a watch.

    uv run python scripts/groq_check.py

Needs GROQ_API_KEY (env or .env). Without it, says so and exits 0.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from gawah_engine.config import settings
from gawah_engine.semantic import SemanticChecker, model_list

CASES = [
    ("matching item", [("SHOE-BLK-RUN", "Black Runner", "size 9")]),
    ("a watch", [("WATCH-STEEL-1", "Steel Chronograph Watch", None)]),
]


def main() -> int:
    if not settings.groq_api_key:
        print("GROQ_API_KEY is not set; skipping the live semantic check (exit 0).")
        return 0
    checker = SemanticChecker(":memory:-unused", api_key=settings.groq_api_key,
                              models=model_list(settings.groq_model, settings.groq_models))
    checker._cache_get = lambda key: None  # always hit the model
    checker._cache_put = lambda key, result: None
    print(f"models (in order): {checker.models}")
    for label, lines in CASES:
        t = time.perf_counter()
        res = checker.check_inputs("footwear", "Black running shoes, size 9, under $80", lines)
        ms = (time.perf_counter() - t) * 1000
        print(f"{label:14} -> {res.category_match:8} confidence={res.confidence:.2f} "
              f"source={res.source} used={checker.status().get('model')} "
              f"variant_issues={res.variant_issues} latency={ms:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
