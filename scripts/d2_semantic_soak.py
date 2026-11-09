"""Spike D2: does a free-tier LLM survive 10 demo runs in a row?

    uv run python scripts/d2_semantic_soak.py            # runs live and rewrites the D2 row
    uv run python scripts/d2_semantic_soak.py --no-write # print only

Runs the Guardian's semantic check 10 times in a row on 3 inputs (30 live calls, cache off,
cooldown off) against the Groq key in GROQ_API_KEY, records latency, HTTP 429s, other errors and
fallbacks, and writes the result to the D2 row of spikes/spikes.md. Without a key it says so and
exits 0 without touching the file.
"""

import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import httpx
from gawah_engine.config import settings
from gawah_engine.semantic import ModelNotFound, SemanticChecker, groq_llm, model_list

SPIKES = Path(__file__).resolve().parents[1] / "spikes" / "spikes.md"
RUNS = 10
MANDATE = ("footwear", "Black running shoes, size 9, under $80")
INPUTS = [
    ("matching shoe", [("SHOE-BLK-RUN", "Black Runner", "size 9")], "match"),
    ("watch (wrong category)", [("WATCH-STEEL-1", "Steel Chronograph Watch", None)], "mismatch"),
    ("shoe, size 10", [("SHOE-BLK-RUN", "Black Runner", "size 10")], "match"),
]


def main() -> int:
    if not settings.groq_api_key:
        print("GROQ_API_KEY is not set; skipping the D2 soak (exit 0, spikes.md untouched).")
        return 0
    inner = groq_llm(settings.groq_api_key)
    stats = {"http_429": 0, "http_other": 0, "timeouts": 0, "not_found": 0}

    def client(model, messages):
        try:
            return inner(model, messages)
        except ModelNotFound:
            stats["not_found"] += 1
            raise
        except httpx.HTTPStatusError as e:
            stats["http_429" if e.response.status_code == 429 else "http_other"] += 1
            raise
        except httpx.TimeoutException:
            stats["timeouts"] += 1
            raise

    checker = SemanticChecker(":unused:", client=client, cooldown_s=0,
                              models=model_list(settings.groq_model, settings.groq_models))
    checker._cache_get = lambda key: None  # type: ignore[method-assign]  # every call is live
    checker._cache_put = lambda key, result: None  # type: ignore[method-assign]
    print(f"models (in order): {checker.models}")

    latencies, fallbacks, wrong, used = [], 0, 0, {}
    for run in range(1, RUNS + 1):
        row = []
        for label, lines, expected in INPUTS:
            t = time.perf_counter()
            res = checker.check_inputs(*MANDATE, lines)
            ms = (time.perf_counter() - t) * 1000
            latencies.append(ms)
            if res.source == "fallback":
                fallbacks += 1
            else:
                m = checker.status()["model"]
                used[m] = used.get(m, 0) + 1
            if res.category_match != expected:
                wrong += 1
            row.append(f"{res.category_match}/{res.confidence:.2f}/{res.source[0]}/{ms:.0f}ms")
        print(f"run {run:2}: " + "  ".join(row))

    n = len(latencies)
    lat = sorted(latencies)
    p95 = lat[min(n - 1, int(n * 0.95))]
    summary = (
        f"{n} live calls (10 runs x 3 inputs, cache off) on {datetime.now(UTC):%Y-%m-%d} with models "
        f"{used or 'none answered'}: {n - fallbacks} model answers, {fallbacks} keyword fallbacks, "
        f"{stats['http_429']} HTTP 429, {stats['http_other']} other HTTP errors, "
        f"{stats['timeouts']} timeouts, {stats['not_found']} model-not-found; "
        f"{wrong} answers differed from the expected category verdict. "
        f"Latency ms: median {statistics.median(lat):.0f}, p95 {p95:.0f}, max {lat[-1]:.0f}."
    )
    verdict = "pass" if fallbacks == 0 and stats["http_429"] == 0 and wrong == 0 else "partial"
    print("\n" + summary + f"\nD2 status: {verdict}")
    if "--no-write" not in sys.argv:
        lines_ = SPIKES.read_text().splitlines()
        for i, line in enumerate(lines_):
            if line.startswith("| D2 "):
                lines_[i] = (
                    f"| D2 | Does a free-tier LLM survive 10 demo runs? | {verdict} | {summary} "
                    "Free-tier limits can change; this is one soak from one key, not a guarantee. "
                    "| Keyword fallback is automatic, and /health shows which path answered. |"
                )
        SPIKES.write_text("\n".join(lines_) + "\n")
        print(f"wrote {SPIKES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
