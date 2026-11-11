"""The keyword fallback (no model available): honest footwear titles match, everything it does not
recognise still fails closed."""

import pytest
from gawah_engine.semantic import keyword_check

MANDATE = ("footwear", "Black Nike running shoes, size 12, under $80")


def check(title: str, sku: str = "X-1"):
    return keyword_check(*MANDATE, [(sku, title, None)])


@pytest.mark.parametrize("title", ["Nike SwiftRun 12", "Stride Black Runner", "Acme FastRun Pro"])
def test_honest_footwear_titles_match(title):
    res = check(title)
    assert res.category_match == "match" and not res.blocks and res.source == "fallback"


def test_the_demo_merchants_own_sku_and_title_match():
    assert check("Nike SwiftRun 12", "NIKE-SWIFTRUN-12-BLK").category_match == "match"


@pytest.mark.parametrize("title", [
    "Harvest Gold Atta Bread", "Green Valley Apples", "Nike Casual Black",  # unknown: fail closed
    "Orbit Wireless Earbuds", "PixelPad 10 Tablet",  # known other category
])
def test_everything_else_still_fails_closed(title):
    res = check(title)
    assert res.category_match == "mismatch" and res.blocks


def test_run_alone_is_not_footwear_and_keyword_stuffing_still_blocks():
    assert check("Run Club Tee").category_match == "mismatch"
    assert check("Steel Watch SwiftRun").category_match == "mismatch"  # names two categories
    assert check("Rerun").category_match == "mismatch"  # needs a 3+ letter product-line prefix


def test_running_shorts_stay_apparel_under_an_apparel_mandate():
    res = keyword_check("apparel", "shorts", [("S-2", "Running Shorts", None)])
    assert res.category_match == "match"
