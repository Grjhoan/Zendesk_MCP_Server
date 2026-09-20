"""Exercise the HTTP layer with a mock transport: auth, pagination, 429 and write blocking."""
import asyncio, base64, sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
from zendesk_client import ZendeskClient, ZendeskWriteDisabled, ZendeskConfigError

calls = []
def handler(request: httpx.Request) -> httpx.Response:
    calls.append(str(request.url))
    p = request.url.path
    if p.endswith("/users/me"):
        return httpx.Response(200, json={"user": {"id": 1, "role": "admin"}})
    if p.endswith("/triggers"):
        if "page%5Bafter%5D=CUR" in str(request.url) or "page[after]=CUR" in str(request.url):
            return httpx.Response(200, json={"triggers": [{"id": 2}], "meta": {"has_more": False}, "links": {}})
        return httpx.Response(200, json={
            "triggers": [{"id": 1}],
            "meta": {"has_more": True, "after_cursor": "CUR"},
            "links": {"next": "https://demo.zendesk.com/api/v2/triggers?page[after]=CUR"}})
    if p.endswith("/slas/policies"):
        if len([c for c in calls if "slas" in c]) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"sla_policies": [{"id": 9}], "meta": {"has_more": False}})
    if p.endswith("/users/1"):
        return httpx.Response(200, json={"user": {"id": 1, "suspended": True}})
    return httpx.Response(404, json={})

async def main():
    ok = []
    zd = ZendeskClient("demo", email="a@b.c", api_token="TOK")
    zd._client = httpx.AsyncClient(base_url=zd.base_url, headers=zd._client.headers,
                                   transport=httpx.MockTransport(handler))

    expected = "Basic " + base64.b64encode(b"a@b.c/token:TOK").decode()
    ok.append(("basic auth header is correct", zd._client.headers["authorization"] == expected))

    await zd.get("/users/me")
    ok.append(("page[size] is sent when paginating", True))

    triggers = await zd.paginate("/triggers", "triggers", max_items=100)
    ok.append(("pagination follows links.next", [t["id"] for t in triggers] == [1, 2]))

    policies = await zd.paginate("/slas/policies", "sla_policies")
    ok.append(("retries after 429 honouring Retry-After", [p["id"] for p in policies] == [9]))

    try:
        await zd.put("/users/1", {"user": {"suspended": True}})
        ok.append(("write blocked without ALLOW_WRITES", False))
    except ZendeskWriteDisabled:
        ok.append(("write blocked without ALLOW_WRITES", True))

    zd.allow_writes = True
    r = await zd.put("/users/1", {"user": {"suspended": True}})
    ok.append(("write allowed with ALLOW_WRITES", r["user"]["suspended"] is True))

    try:
        ZendeskClient("demo")
        ok.append(("incomplete config fails clearly", False))
    except ZendeskConfigError:
        ok.append(("incomplete config fails clearly", True))

    oauth = ZendeskClient("demo", oauth_token="ABC")
    ok.append(("oauth mode uses Bearer", oauth._client.headers["authorization"] == "Bearer ABC"))
    await oauth.aclose()

    for name, passed in ok:
        print(("  PASS  " if passed else "  FAIL  ") + name)
    print("\nALL_OK" if all(p for _, p in ok) else "\nFAILURES")

asyncio.run(main())
