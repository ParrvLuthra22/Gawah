"""Warm the Guardian's semantic cache so a recording never depends on a live Groq call.

    uv run python scripts/warm_semantic_cache.py                  # warm the engine's database
    uv run python scripts/warm_semantic_cache.py --database-url sqlite:///./gawah.db
    uv run python scripts/warm_semantic_cache.py --attacks my_titles.json   # JSON list of titles

Runs the real semantic check once for every demo input so the engine later finds the answers in
`semantic_cache` (the cache key is a hash of the exact prompt: category, description, sku, title,
variant). Two sets are warmed:

  * the inputs scripts/demo_run.py sends (mandate "Black running shoes, size 9, under $80"), and
  * the inputs `make e2e` / integration/e2e.py send (mandate "Black Nike running shoes, size 12,
    under $80"): the shopper's real cart, the four attack fixtures in
    system1/shopper/security/attack_simulation.py, and the electronics and grocery titles from
    system1's demo catalog. They come from integration/scenarios.py, which reads System 1's files,
    so a title changed there is picked up here.

Needs GROQ_API_KEY; without it, says so and exits 0. Answers that came from the keyword fallback
are NOT cached (by design), so they are listed: re-run when the models are available again.
`--attacks FILE` (a JSON list of titles) adds more footwear-mandate titles.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gawah_engine.config import settings
from gawah_engine.db import db_path, init_db
from gawah_engine.semantic import SemanticChecker, model_list

CATEGORY = "footwear"
DESCRIPTION = "Black running shoes, size 9, under $80"
SKU = "SHOE-BLK-RUN"

DEMO_INPUTS: list[tuple[str, str, str, str | None]] = [
    # label, sku, title, variant  (titles/variants as scripts/demo_run.py sends them)
    ("matching shoes, size 9", SKU, "Black Runner", "size 9"),
    ("matching shoes, size 10", SKU, "Black Runner Lite", "size 10"),
    ("$120 shoes", SKU, "Black Runner Pro", "size 9"),
    ("a watch", "WATCH-STEEL-1", "Steel Chronograph Watch", None),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--database-url", default=settings.database_url)
    ap.add_argument("--attacks", help="JSON file with a list of attack titles")
    args = ap.parse_args()
    if not settings.groq_api_key:
        print("GROQ_API_KEY is not set; nothing to warm (exit 0).")
        return 0
    titles = json.loads(Path(args.attacks).read_text()) if args.attacks else []
    inputs = [(label, CATEGORY, DESCRIPTION, sku, title, variant)
              for label, sku, title, variant in DEMO_INPUTS]
    inputs += [(f"extra title {i}", CATEGORY, DESCRIPTION, SKU, t, "size 9")
               for i, t in enumerate(titles, 1)]
    try:
        from integration.scenarios import semantic_inputs

        inputs += [(f"e2e: {i.label}", i.category, i.description, i.sku, i.title, i.variant)
                   for i in semantic_inputs()]
    except ImportError as exc:  # System 1's shopper is not installed: `uv sync` installs it
        print(f"skipping the e2e inputs ({exc}); run `uv sync` to install the shopper")

    path = db_path(args.database_url)
    init_db(path)
    checker = SemanticChecker(path, api_key=settings.groq_api_key,
                              models=model_list(settings.groq_model, settings.groq_models))
    print(f"database: {args.database_url}\nmodels (in order): {checker.models}")
    counts = {"model": 0, "cache": 0, "fallback": 0}
    uncached = []
    for label, category, description, sku, title, variant in inputs:
        t = time.perf_counter()
        res = checker.check_inputs(category, description, [(sku, title, variant)])
        ms = (time.perf_counter() - t) * 1000
        counts[res.source] += 1
        if res.source == "fallback":
            uncached.append(label)
        print(f"  {label[:44]:44} {title[:26]:26} -> {res.category_match:8} {res.confidence:.2f}  "
              f"{res.source:8} {res.model or '-':22} {ms:5.0f} ms  {res.reason}")
    print(f"\nwarmed {len(inputs)} inputs: {counts['model']} from the model, "
          f"{counts['cache']} already cached, {counts['fallback']} from the keyword fallback")
    if uncached:
        print("NOT cached (fallback answers are never cached): " + ", ".join(uncached))
    return 0 if not uncached else 1


if __name__ == "__main__":
    raise SystemExit(main())
