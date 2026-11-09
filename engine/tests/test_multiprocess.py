"""Two real engine processes, one SQLite file, 40 concurrent proposals.

Only loopback connections between the test and its own two child processes; no external network.
PayPal is SIMULATED inside each child (GAWAH_FAKE_PAYPAL=1, which the engine refuses unless
GAWAH_ENV=test). The children run in a temp directory so they never read the repo's .env.
"""

import itertools
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from gawah_contract.carts import generate_keypair, public_key_pem, sign_cart

ENGINE_DIR = Path(__file__).resolve().parents[1]
S1, S3 = "mp-key-system1", "mp-key-system3"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def iso(delta=timedelta(0)):
    return (datetime.now(UTC) + delta).isoformat().replace("+00:00", "Z")


def child_env(tmp: Path, **extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")}
    env.update(
        PYTHONPATH=os.pathsep.join([str(ENGINE_DIR), str(ENGINE_DIR.parent / "contract")]),
        GAWAH_MOCK="0", GAWAH_ENV="test", GAWAH_FAKE_PAYPAL="1",
        DATABASE_URL=f"sqlite:///{tmp / 'shared.db'}",
        LEDGER_SIGNING_KEY_PATH=str(tmp / "keys"), MANDATE_SIGNING_KEY_PATH=str(tmp / "keys"),
        SYSTEM1_KEY=S1, SYSTEM3_KEY=S3, RATE_LIMIT_PER_MINUTE="0", GROQ_API_KEY="x-never-used",
    )
    env.update(extra)
    return env


def start(tmp: Path, port: int, **extra) -> subprocess.Popen:
    log = open(tmp / f"engine-{port}.log", "wb")  # noqa: SIM115  (closed with the process)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gawah_engine.main:app", "--port", str(port),
         "--host", "127.0.0.1", "--log-level", "warning"],
        cwd=tmp, env=child_env(tmp, **extra), stdout=log, stderr=subprocess.STDOUT,
    )


def wait_healthy(port: int, proc: subprocess.Popen, tmp: Path, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            pytest.fail(f"engine on {port} exited:\n{(tmp / f'engine-{port}.log').read_text()[-2000:]}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    pytest.fail(f"engine on {port} never became healthy:\n{(tmp / f'engine-{port}.log').read_text()[-2000:]}")


@pytest.fixture
def two_engines(tmp_path):
    ports = [free_port(), free_port()]
    # both start at the same moment on an empty database and empty key directory: this also
    # exercises migration and first-key creation races
    procs = [start(tmp_path, p) for p in ports]
    try:
        for p, proc in zip(ports, procs, strict=True):
            wait_healthy(p, proc, tmp_path)
        yield [f"http://127.0.0.1:{p}" for p in ports]
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_fake_paypal_is_refused_outside_test_env(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", "from gawah_engine.services import check_fake_paypal_allowed as c; c()"],
        cwd=tmp_path, env=child_env(tmp_path, GAWAH_ENV="production"),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode != 0 and "refused unless GAWAH_ENV=test" in proc.stderr
    no_env = subprocess.run(
        [sys.executable, "-c", "from gawah_engine.services import check_fake_paypal_allowed as c; c()"],
        cwd=tmp_path, env={k: v for k, v in child_env(tmp_path).items() if k != "GAWAH_ENV"},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert no_env.returncode != 0


def test_forty_concurrent_proposals_across_two_processes_pay_exactly_once(two_engines, tmp_path):
    a, b = two_engines
    key = generate_keypair()
    h1, h3 = {"X-Gawah-Key": S1}, {"X-Gawah-Key": S3}
    with httpx.Client(timeout=60) as c:
        assert c.post(f"{a}/v1/merchants", headers=h1, json={
            "merchant_id": "demo-shoe-store", "key_id": "demo-shoe-store-key-1", "alg": "ES256",
            "public_key_pem": public_key_pem(key)}).status_code == 201
        mandate = c.post(f"{b}/v1/mandates", headers=h3, json={  # created on B, visible on A
            "user_id": "u1", "category": "footwear", "item_description": "Black running shoes, size 9",
            "max_amount_minor": 8000, "currency": "USD", "allowed_merchants": ["demo-shoe-store"],
            "expires_at": iso(timedelta(days=7))}).json()

    def proposal(i: int) -> dict:
        cart = sign_cart({
            "cart_id": f"cart_mp_{i}", "merchant_id": "demo-shoe-store",
            "line_items": [{"sku": "SHOE-BLK-RUN", "title": "Black Runner", "variant": "size 9",
                            "quantity": 1, "unit_price_minor": 6500}],
            "total_minor": 6500, "currency": "USD", "expires_at": iso(timedelta(hours=1)),
            "signature": "", "key_id": ""}, key, "demo-shoe-store-key-1").model_dump(mode="json")
        return {
            "id": f"prop_mp_{i}", "mandate_id": mandate["id"], "agent_id": "agent-1",
            "merchant_id": "demo-shoe-store",
            "product": {"id": "SHOE-BLK-RUN", "title": "Black Runner", "url": "https://x.example/p"},
            "quantity": 1, "unit_price_minor": 6500, "currency": "USD", "rationale": "shoes",
            "signed_cart": cart, "created_at": iso()}

    results: list = [None] * 40
    barrier = threading.Barrier(40)

    def go(i: int) -> None:
        base = a if i % 2 == 0 else b  # alternate between the two processes
        with httpx.Client(timeout=120) as c:
            barrier.wait()
            r = c.post(f"{base}/v1/proposals", headers=h1, json=proposal(i))
            results[i] = (r.status_code, r.json())

    threads = [threading.Thread(target=go, args=(i,)) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert all(r and r[0] == 200 for r in results), [r for r in results if not r or r[0] != 200][:3]
    bodies = [r[1] for r in results]
    paid = [x for x in bodies if x["payment"] and x["payment"]["status"] == "PAID"]
    blocked = [x for x in bodies if x["decision"]["verdict"] == "BLOCK"]
    assert len(paid) == 1, f"expected exactly one PAID, got {len(paid)}"
    assert len(blocked) == 39 and all(x["decision"]["reasons"] == ["OVER_BUDGET"] for x in blocked)
    assert all(x["payment"] is None for x in blocked)

    with httpx.Client(timeout=60) as c:
        for base in (a, b):  # either process sees the same truth
            budget = c.get(f"{base}/v1/mandates/{mandate['id']}/budget", headers=h3).json()
            assert (budget["spent_minor"], budget["reserved_minor"], budget["remaining_minor"]) == (6500, 0, 1500)
            pays = c.get(f"{base}/v1/payments", headers=h3).json()
            assert [p["status"] for p in pays] == ["PAID"]
            ver = c.get(f"{base}/v1/ledger/verify", headers=h3).json()
            assert ver["ok"] is True and ver["reason"] is None
        entries = c.get(f"{a}/v1/ledger", headers=h3).json()
        assert ver["checked"] == len(entries) == 1 + 40 * 2 + 2  # created + 40x(received,decision) + paid
        assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))  # no gaps, no fork
        assert all(e["prev_hash"] == p["hash"] for p, e in itertools.pairwise(entries))
        types = [e["type"] for e in entries]
        assert types.count("PAYMENT_STARTED") == 1 and types.count("PAYMENT_COMPLETED") == 1
        assert c.get(f"{a}/v1/ledger/head", headers=h3).json()["seq"] == len(entries)
