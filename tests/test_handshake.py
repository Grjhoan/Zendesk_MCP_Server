"""Real MCP handshake: start the server over stdio, negotiate and list tools."""
import asyncio, os, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["server.py"],
        cwd=str(ROOT),
        env={**os.environ, "ZENDESK_SUBDOMAIN": "demo",
             "ZENDESK_EMAIL": "a@b.c", "ZENDESK_API_TOKEN": "x"},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            print("server:", init.server_info.name, init.server_info.version)
            tools = await session.list_tools()
            print(f"tools registered: {len(tools.tools)}\n")
            for t in tools.tools:
                required = t.input_schema.get("required", [])
                props = list(t.input_schema.get("properties", {}).keys())
                print(f"  {t.name}")
                print(f"     params: {props or '-'}  required: {required or '-'}")
            print("\nSCHEMA_OK")

asyncio.run(main())
