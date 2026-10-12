"""Failures must come back as a readable result, never as a raised exception."""
import asyncio, sys, os
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.update(ZENDESK_SUBDOMAIN="demo", ZENDESK_EMAIL="a@b.c", ZENDESK_API_TOKEN="x")
import httpx, server
from zendesk_client import ZendeskClient, ZendeskRateLimited

TOKEN = "tok12345"


def make(handler, allow_writes=False):
    zd = ZendeskClient("demo", email="a@b.c", api_token=TOKEN, allow_writes=allow_writes)
    zd._client = httpx.AsyncClient(base_url=zd.base_url, headers=zd._client.headers,
                                   transport=httpx.MockTransport(handler))
    return zd


def status(code):
    def handler(request):
        return httpx.Response(code, json={"error": "x"}, headers={"Retry-After": "1"})
    return make(handler)


def unreachable(request):
    raise httpx.ConnectError("name resolution failed", request=request)


def readable(r):
    return isinstance(r, dict) and "error" in r and "hint" in r


async def call(tool, **kwargs):
    """Run a tool; a raised exception counts as a failed check."""
    try:
        return await tool(**kwargs)
    except Exception as exc:
        return {"raised": repr(exc)}


async def main():
    ok = []

    # A 401 on whoami must say what the connection was attempted with.
    server._client = status(401)
    r = await call(server.whoami)
    configured = r.get("configured", {})
    ok.append(("whoami on 401 reports subdomain, auth mode and email presence",
               configured.get("subdomain") == "demo"
               and configured.get("auth_mode") == "api_token"
               and configured.get("email_set") is True))
    ok.append(("whoami reports the token length",
               configured.get("token_length") == len(TOKEN)))
    ok.append(("whoami never exposes the token", TOKEN not in str(r)))

    # Every HTTP status family gets a message and a hint instead of raising.
    for code in (401, 403, 404, 422, 500):
        server._client = status(code)
        r = await call(server.find_tickets, subject="something")
        ok.append((f"find_tickets on {code} returns a readable result",
                   readable(r) and str(code) in r["error"]))

    server._client = status(403)
    r = await call(server.backlog_health)
    ok.append(("backlog_health on 403 returns a readable result", readable(r)))

    server._client = status(500)
    r = await call(server.find_user, query="ana")
    ok.append(("unknown status gets the generic hint",
               readable(r) and r["hint"] == "Unexpected response from Zendesk."))

    # Exhausting the 429 retries raises a dedicated type, which is handled.
    zd = status(429)
    try:
        await zd.get("/users/me")
        ok.append(("429 exhaustion raises ZendeskRateLimited", False))
    except ZendeskRateLimited:
        ok.append(("429 exhaustion raises ZendeskRateLimited", True))
    server._client = status(429)
    r = await call(server.find_tickets, subject="something")
    ok.append(("find_tickets on 429 says to wait a minute",
               readable(r) and "wait a minute" in r["error"].lower()))

    # Network failure.
    server._client = make(unreachable)
    r = await call(server.find_tickets, email="ana@acme.com")
    ok.append(("network error returns a readable result",
               readable(r) and "ConnectError" in r["error"]))

    # Write attempted with writes disabled.
    def user_ok(request):
        return httpx.Response(200, json={"user": {"id": 1, "role": "agent", "suspended": False}})
    server._client = make(user_ok)
    r = await call(server.set_user_status, user_id=1, suspended=True, dry_run=False)
    ok.append(("write-disabled returns a readable result",
               readable(r) and "ZENDESK_ALLOW_WRITES" in r["error"]))

    # Missing configuration.
    server._client = None
    saved = {k: os.environ.pop(k, None) for k in ("ZENDESK_SUBDOMAIN",)}
    r = await call(server.agent_workload)
    ok.append(("config error returns a readable result",
               readable(r) and "ZENDESK_SUBDOMAIN" in r["error"]))
    os.environ.update({k: v for k, v in saved.items() if v is not None})

    # An unsubstituted bundle placeholder must be treated as empty.
    os.environ["ZENDESK_OAUTH_TOKEN"] = "${user_config.oauth_token}"
    os.environ["ZENDESK_EMAIL"] = "real@example.com"
    os.environ["ZENDESK_API_TOKEN"] = "realToken"
    zd = ZendeskClient.from_env()
    ok.append(("unsubstituted placeholder is not taken as an OAuth token",
               zd.auth_mode == "api_token"))
    os.environ["ZENDESK_SUBDOMAIN"] = "${user_config.subdomain}"
    try:
        ZendeskClient.from_env()
        ok.append(("placeholder subdomain counts as missing", False))
    except Exception as exc:
        ok.append(("placeholder subdomain counts as missing",
                   "ZENDESK_SUBDOMAIN" in str(exc)))

    for name, passed in ok:
        print(("  PASS  " if passed else "  FAIL  ") + name)
    print("\nALL_OK" if all(p for _, p in ok) else "\nFAILURES")
    sys.exit(0 if all(p for _, p in ok) else 1)

asyncio.run(main())
