"""Load the JSON fixtures in contract/fixtures/."""

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())
