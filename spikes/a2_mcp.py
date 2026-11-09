"""Spike A2: does the remote PayPal MCP accept our sandbox token?

Run: uv run python spikes/a2_mcp.py [order_id]
Tries streamable HTTP at /http, then SSE at /sse. Never prints the token.
"""

import asyncio
import sys

from gawah_engine.config import settings
from gawah_engine.paypal import PayPalClient
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

BASE = "https://mcp.sandbox.paypal.com"


def leaves(e):
    subs = getattr(e, "exceptions", None)
    return [x for sub in subs for x in leaves(sub)] if subs else [e]


async def probe(session: ClientSession, order_id: str | None):
    await session.initialize()
    tools = (await session.list_tools()).tools
    print("tools:", [t.name for t in tools])
    if order_id:
        read_only = [t.name for t in tools if "get" in t.name or "list" in t.name]
        print("read-only candidates:", read_only)
        for name in read_only:
            if "order" in name:
                res = await session.call_tool(name, {"id": order_id})
                print(name, "->", str(res.content)[:400])
                break


async def main(order_id: str | None):
    pp = PayPalClient(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_env)
    headers = {"Authorization": f"Bearer {pp.get_token()}"}
    for label, url, opener in (
        ("streamable-http", f"{BASE}/http", streamable_http_client),
        ("sse", f"{BASE}/sse", sse_client),
    ):
        print(f"--- {label} {url}")
        try:
            async with asyncio.timeout(60):
                if opener is streamable_http_client:
                    http = create_mcp_http_client(headers=headers)
                    async with (
                        opener(url, http_client=http) as (r, w, *_),
                        ClientSession(r, w) as s,
                    ):
                        await probe(s, order_id)
                else:
                    async with (
                        opener(url, headers=headers) as (r, w),
                        ClientSession(r, w) as s,
                    ):
                        await probe(s, order_id)
            print(f"{label}: OK")
            return 0
        except BaseException as e:  # noqa: BLE001 - spike: report whatever happened
            print(f"{label}: FAILED {type(e).__name__}: {str(e)[:400]}")
            for leaf in leaves(e):
                print("   leaf:", type(leaf).__name__, str(leaf)[:300])
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
