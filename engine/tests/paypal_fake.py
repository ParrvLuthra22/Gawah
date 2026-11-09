"""Test import path for the simulated PayPal (it lives in the package so a test engine can use it)."""

from gawah_engine.fake_paypal import Crash, FakePayPal

__all__ = ["Crash", "FakePayPal"]
