# zendesk-admin-mcp

A local [MCP](https://modelcontextprotocol.io) server for Zendesk administration
work. Zendesk adopted MCP on the client side — its agentic AI consumes external
servers — but never shipped a server of its own that a host application could
use. This fills that gap, with a deliberately narrow scope.

It does not wrap the whole API. It exposes **12 tools** covering four concrete
admin jobs, because a server with 200 tools floods the context window and makes
the model choose worse.

```
You: "Which triggers haven't fired in the last 30 days?"
     "Who has the heaviest open backlog right now?"
     "Maria is leaving on Friday — what's still assigned to her?"
```

- [Purpose](#purpose)
- [Features](#features)
- [Security model](#security-model)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Connecting it](#connecting-it)
- [Verification](#verification)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Implementation notes](#implementation-notes)
- [Contributing](#contributing)
- [License](#license)
- [Author](#author)

## Purpose

Zendesk admin work involves a lot of questions that are tedious to answer
through the UI: which business rules are dead weight, where the backlog is
piling up, who still holds admin privileges, and what is left hanging when an
agent leaves. Each of those means clicking through several admin screens and
cross-referencing by hand.

This server turns those questions into tool calls an AI assistant can make
directly, and returns aggregated summaries rather than raw record dumps, so the
model can reason about the answer instead of drowning in it.

## Features

### Connection

| Tool | What it does |
|---|---|
| `whoami` | Verifies credentials and shows which account and role the server operates as. |

### Business-rule auditing

| Tool | What it does |
|---|---|
| `audit_business_rules` | Triggers, automations and macros: finds inactive rules, rules unused in 30 days, rules not modified in months, and duplicate titles. |
| `list_ticket_fields` | Inventory of ticket fields, flagging inactive and custom ones. |

### Backlog and SLAs

| Tool | What it does |
|---|---|
| `list_sla_policies` | SLA policies with their targets by priority and metric. |
| `backlog_health` | Counts by status and priority, unassigned tickets, and the most stalled ones. |
| `agent_workload` | Distribution of open tickets per agent, heaviest load first. |

### Security

| Tool | What it does |
|---|---|
| `security_overview` | Who holds the admin role, unused OAuth tokens, installed apps. |
| `recent_admin_changes` | Configuration changes from the audit log. Requires the Enterprise plan. |

### Users and offboarding

| Tool | What it does |
|---|---|
| `find_user` | Finds users by name, email or Zendesk search syntax. |
| `offboarding_report` | Everything left hanging on a departing agent: open tickets, groups, role, last login. Read-only. |
| `reassign_tickets` | **Write.** Reassigns an agent's open tickets to another agent. Dry run by default. |
| `set_user_status` | **Write.** Suspends, reactivates or changes a user's role. Dry run by default. |

## Security model

Three layers, in order of importance:

1. **The token's Zendesk role is the real boundary.** The server can never do
   more than that user could already do in the UI. If you want a read-only
   server, give it a token belonging to a user with read-only permissions.
2. **Writes are off by default.** `reassign_tickets` and `set_user_status` fail
   with a clear message while `ZENDESK_ALLOW_WRITES` is not `true`.
3. **Dry run by default.** Even with writes enabled, both tools report what they
   *would* change without touching anything, until you call them with
   `dry_run=false`.

Each tool is also advertised to the MCP client with `readOnlyHint` or
`destructiveHint`, so the host can prompt you for confirmation before the ones
that modify data.

### Credential handling

No credential is ever written into the source tree. The server reads everything
from environment variables, and authentication travels in an HTTP header rather
than a URL, so it cannot leak into a logged request line.

Depending on how you install it, your token lives in one of three places:

| Install method | Where the token is stored |
|---|---|
| Claude Desktop extension (`.mcpb`) | Your OS keychain |
| Claude Desktop / Claude Code JSON config | That config file, in plain text |
| Local `.env` | That file, in plain text — git-ignored by this repo |

If you fork or clone this repo, keep `.env` out of version control. The shipped
`.gitignore` already excludes `.env`, `dist/`, and `*.mcpb`.

### A note on API tokens

Zendesk has marked API tokens as **deprecated** and recommends OAuth. Their own
documentation warns that an API token allows impersonating any member of the
account, admins included. For a local prototype they are fine; if this reaches
production or more than one person, move to OAuth. The server accepts both.

## Requirements

- Python 3.10 or newer
- A Zendesk account and a token (API token or OAuth)
- For `recent_admin_changes`: the Zendesk Enterprise plan
- For `list_sla_policies`: a plan that includes SLAs

## Installation

```bash
git clone https://github.com/Grjhoan/Zendesk_MCP_Server.git
cd Zendesk_MCP_Server
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

Copy `.env.example` to `.env` and fill it in, or define the variables directly
in your MCP client's configuration.

To generate an API token: **Admin Center → Apps and integrations → APIs →
Zendesk API → Settings**, enable *Token access* and create one. Save it right
then; Zendesk will not show it again.

| Variable | Required | Description |
|---|---|---|
| `ZENDESK_SUBDOMAIN` | Yes | If you sign in at `https://acme.zendesk.com`, it is `acme`. |
| `ZENDESK_OAUTH_TOKEN` | One of the two | OAuth token. The recommended option. |
| `ZENDESK_EMAIL` + `ZENDESK_API_TOKEN` | One of the two | Email of the token owner, and the token. |
| `ZENDESK_ALLOW_WRITES` | No | `true` enables the two write tools. Off by default. |

If both are set, `ZENDESK_OAUTH_TOKEN` wins.

## Connecting it

### Claude Desktop — as an extension (recommended)

This bundles the dependencies, so the extension does not depend on your
virtualenv, and stores the token in your OS keychain instead of a plain-text
file.

```bash
./build_mcpb.sh
```

Then open **Settings → Extensions → Advanced settings** and either:

- **Install extension…** → pick the generated `zendesk-admin.mcpb`, or
- **Install unpacked extension…** → pick the `dist/` folder (not the project
  root — only `dist/` has the rewritten manifest).

Claude will prompt you for the subdomain, credentials and the write toggle.

> Build the bundle on the machine you will install it on. It vendors
> platform-compiled wheels and pins the absolute path of the interpreter that
> built it, so a bundle built on Linux will not start on macOS. This is also why
> `dist/` and `*.mcpb` are git-ignored — they are never portable.

### Claude Desktop — manual configuration

In `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "zendesk-admin": {
      "command": "/absolute/path/to/zendesk-admin-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/zendesk-admin-mcp/server.py"],
      "env": {
        "ZENDESK_SUBDOMAIN": "acme",
        "ZENDESK_EMAIL": "you@company.com",
        "ZENDESK_API_TOKEN": "...",
        "ZENDESK_ALLOW_WRITES": "false"
      }
    }
  }
}
```

Use absolute paths: the client does not launch the server from this folder.
Restart Claude Desktop after editing.

### Claude Code

```bash
claude mcp add zendesk-admin \
  --env ZENDESK_SUBDOMAIN=acme \
  --env ZENDESK_EMAIL=you@company.com \
  --env ZENDESK_API_TOKEN=... \
  --env ZENDESK_ALLOW_WRITES=false \
  -- /absolute/path/.venv/bin/python /absolute/path/server.py
```

Then confirm it is connected:

```bash
claude mcp list
```

### Any other MCP client

The server speaks MCP over **stdio**. Point your client at
`.venv/bin/python server.py` with the environment variables above.

## Verification

```bash
python tests/test_client.py      # HTTP layer against a mock transport
python tests/test_handshake.py   # real MCP handshake and tool listing
```

Both run without credentials or network access. After that, with credentials in
place:

```bash
python check_connection.py       # probes every endpoint the tools rely on
```

This reports which endpoints answer for *your* account and plan, which is the
fastest way to find out what will work before connecting the server to a client.
Finally, ask the model to call `whoami` — the quickest confirmation that the
connection and role are what you expect.

## Limitations

- **Read-heavy by design.** Only two tools write. This is not a general-purpose
  Zendesk automation server.
- **Plan-gated features.** `recent_admin_changes` needs Enterprise;
  `list_sla_policies` needs a plan with SLAs. Both degrade to an explanatory
  message rather than an error.
- **Results are bounded.** Every tool caps how much it fetches and returns a
  `truncated` flag when it hits the cap. On very large accounts, treat the
  numbers as a well-sampled picture rather than a full census.
- **Single account per server instance.** Credentials are read once from the
  environment at startup.
- **Local only.** stdio transport, one operator, one set of credentials. There
  is no multi-user HTTP transport yet.
- **No caching.** Repeated audits spend fresh rate-limit quota each run.
- **`usage_30d` is not universal.** Zendesk reports it for triggers and macros
  but not automations, where it comes back null.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401` from `whoami` | Wrong subdomain, email or token — or *Token access* is disabled in Admin Center. The email must be the token owner's. |
| `403` on a specific tool | The token's role lacks permission for that endpoint. `security_overview` needs admin. |
| `404` on SLA or audit-log tools | Your plan does not include that feature. |
| Write tool refuses to run | `ZENDESK_ALLOW_WRITES` is not `true`. Once it is, you still need `dry_run=false` to actually apply. |
| Server does not appear in the client | Non-absolute paths in the config, or the client was not restarted. |
| Extension installs but will not start | The `.mcpb` was built on a different machine or with a different interpreter. Rebuild with `./build_mcpb.sh`. |
| Repeated `429` | Rate limited. The client retries up to 4 times honouring `Retry-After`; lower `max_items` on the heavy tools. |

## Implementation notes

Verified against the official Zendesk documentation:

- **Authentication**: basic auth with the `{email}/token:{api_token}` form in
  base64, or `Bearer` for OAuth.
- **Pagination**: cursor-based (`page[size]`, `links.next`, `meta.has_more`).
  Offset pagination is capped at 100 pages and 10,000 records, so it is unused.
- **Rate limits**: on a 429 the `Retry-After` header is honoured, with up to 4
  retries and a 60-second ceiling per wait.
- **Search**: `/search/export` rather than `/search`, because the latter caps at
  1,000 total results. The export cursor expires after one hour.
- **Bulk writes**: `update_many` accepts 100 ids per call and processes
  asynchronously via `job_status`.

The Python MCP SDK is on 2.x, where `FastMCP` was renamed `MCPServer`.
`pyproject.toml` pins `mcp>=2.0,<3`.

## Contributing

Issues and pull requests are welcome. Please run both test suites before
submitting, and never commit a `.env`, a built `dist/`, or a `.mcpb` bundle.

Ideas that would push this further:

- Streamable HTTP transport with OAuth 2.1, so several admins can connect from
  claude.ai or another host with their own credentials.
- A read cache, to avoid spending rate-limit quota on audits that run often.
- Tools for views, forms and groups, if the business-rule audit proves useful.

## License

MIT — see [LICENSE](LICENSE). You are free to use, modify and distribute this in
your own environment, commercial or otherwise.

## Author

**Jhoan Zabala**

- Web: [jhoanzabala.com](https://jhoanzabala.com)
- Email: [jzabalacx@gmail.com](mailto:jzabalacx@gmail.com)

---

*Not affiliated with or endorsed by Zendesk. "Zendesk" is a trademark of Zendesk, Inc.*
