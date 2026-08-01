# SerenLodestar

The cluster head. The thing that knows which box has the GPU.

One process on port **6361** that keeps a live picture of every node you're
running, routes service control to whichever one can actually do the job,
runs the chat tool loop, fires scheduled tasks, and puts the whole thing
behind one operator dashboard.

Lodestar doesn't run models. It knows *where* the models are, and it's the
only address anything else has to remember.

Part of the [Seren](https://github.com/ChadRoesler) stack. It talks to
[SerenObservatory](https://github.com/ChadRoesler/SerenObservatory) on each
node — that's the only hard dependency, and everything else is optional.

---

## Install

```bash
pip install seren-lodestar
python -m seren_lodestar
```

That starts, finds no nodes, and tells you so on stderr rather than pretending:

```
[lodestar] config: 0.0.0.0:6361
[lodestar] inbound auth: DISABLED (no token)
[lodestar] cluster: 0 node(s) configured
```

Copy `seren-lodestar.yaml.sample` to `seren-lodestar.yaml` and list your nodes.
Unlike the rest of the family, **configuration isn't optional here** — a cluster
head with no cluster orchestrates nothing, and it will warn you about exactly
that at startup.

```bash
python -m seren_lodestar --config /etc/seren/lodestar.yaml --port 6361
```

`--config`, `--port` and `--host` override the file. `SEREN_LODESTAR_CONFIG`
does the same for the config path.

## Wire up your nodes

```yaml
cluster:
  refresh_interval: "00:30:00"    # how often to re-probe
  discovery_timeout: "00:00:02"   # per-node patience

  nodes:
    - name: "orin-nano"
      agent_url: "http://192.168.1.100:7777"
      preferred_for: ["whisper", "kokoro"]
      is_host: false

    - name: "xavier"
      agent_url: "http://192.168.1.101:7777"
      preferred_for: ["llama", "comfy"]
      is_host: true
```

Port **7777** is where an Observatory listens. Check one before you trust it:

```bash
curl http://192.168.1.100:7777/api/v1/system/ping
```

`preferred_for` is **advisory, not binding**. It's where Lodestar looks first;
if that node is down or doesn't have the service installed, the request goes to
whoever actually answers. A preference is a hint about your hardware, not a
promise you have to keep making true by hand.

`is_host` marks the node running primary inference — exactly one. `nickname` is
free text for the dashboard. `agent_update_path` is where a pushed Observatory
package lands *on that node*, which differs per box (a Jetson image is usually
`/home/jetson`, a NUC or Spark is whatever account runs the service), so it's
per-node rather than global.

The node type isn't special to Lodestar. A Jetson, a NUC, a Spark and a spare
laptop all go in the same way. Only the Observatory on the far end matters.

---

## What it actually does

**Discovery runs on a loop.** Every `refresh_interval`, every node gets probed
with a `discovery_timeout` of patience. Nodes go offline and come back without
anyone restarting anything, and the capability map — which services exist on
which boxes — updates itself.

**Service control is routed, not addressed.** `POST /api/v1/service/llama/start`
doesn't need to know which machine that is. If you *do* want to be specific,
`/api/v1/node/xavier/service/llama/start` bypasses routing entirely.

**Chat carries a tool loop.** `POST /api/v1/chat` runs inference with tools
attached; `/chat/stream` does it streaming. The dialect that formats tool calls
for the model lives behind `IToolDialect` — `QwenHermesDialect` is the shipped
one, and swapping model families means writing a new dialect, not editing the
loop. `POST /api/v1/chat/inspect` shows you exactly what got injected when the
answer looks wrong.

**The scheduler fires tools on a clock.** Cron expressions or relative offsets
(`2h`, `30m`, `90s`). State persists to `scheduler.persistence_dir`, defaulting
to a `scheduler/` directory next to your config file. Tasks survive a restart;
one-shots delete themselves after firing, recurring ones re-arm.

**Agent updates push outward.** `POST /api/v1/system/agent-update` ships a
packaged Observatory to every node at its configured `agent_update_path`.

---

## Connect a model to it

The MCP endpoint is at `/mcp/` — trailing slash, a bare `/mcp` gets a 307.

```jsonc
{
  "mcpServers": {
    "seren-lodestar": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:6361/mcp/",
               "--transport", "http-only"]
    }
  }
}
```

Six tools, all cluster-shaped:

| tool | |
|---|---|
| `cluster_refresh` | re-probe everything, or one named node |
| `cluster_capabilities` | which services are on which nodes |
| `service_control` | start / stop / restart / status / health, routed or pinned |
| `scheduler_list` | what's scheduled |
| `scheduler_add` | cron or relative |
| `scheduler_remove` | by name |

DNS-rebinding protection defaults **off**, because the normal deployment is a
trusted LAN and the check breaks cross-host access. Turn it on with
`SEREN_LODESTAR_ALLOWED_HOSTS` / `SEREN_LODESTAR_ALLOWED_ORIGINS` when the
network stops being trusted.

---

## Endpoints

| | |
|---|---|
| `GET /` | service info |
| `GET /health` | liveness |
| `GET /viewer` | the operator dashboard |
| `GET /api/v1/system/ping` | public — no token |
| `GET /api/v1/system/version` | public — no token |
| `GET /api/v1/system/status` | per-node status |
| `GET /api/v1/system/health` | cluster health |
| `POST /api/v1/system/reclaim` | stop services to free memory |
| `POST /api/v1/system/reboot/{node}` | reboot a node |
| `POST /api/v1/system/reboot/{node}/cancel` | change your mind |
| `POST /api/v1/system/agent-update` | push Observatory to every node |
| `POST /api/v1/cluster/refresh` | re-probe all nodes |
| `POST /api/v1/cluster/refresh/{node}` | re-probe one |
| `GET /api/v1/cluster/capabilities` | the capability map |
| `GET`/`POST` `/api/v1/service/{name}/*` | routed service lifecycle |
| `GET`/`POST` `/api/v1/node/{node}/service/{svc}/*` | pinned to one node |
| `GET`/`POST` `/api/v1/scheduler/tasks` | list / add |
| `DELETE /api/v1/scheduler/tasks/{name}` | remove |
| `POST /api/v1/scheduler/tasks/{name}/pause` | pause |
| `POST /api/v1/scheduler/tasks/{name}/resume` | resume |
| `POST /api/v1/chat` | inference with tools |
| `POST /api/v1/chat/stream` | the same, streamed |
| `POST /api/v1/chat/inspect` | what got injected, for debugging |
| `GET /api/v1/chat/health` | is the backend up |
| `GET /api/v1/chat/last_user_at` | last human activity |
| `/mcp/` | MCP streamable-HTTP transport |

## Auth

Set `server.bearer_token`, or point at an env var or the OS keyring:

```yaml
server:
  bearer_token_env: "SEREN_LODESTAR_BEARER_TOKEN"
  # bearer_token_keyring: "seren-lodestar"
```

Everything requires it except `/`, `/health`, `/viewer`, `system/ping` and
`system/version` — liveness and identity probes that a monitoring box shouldn't
need a secret to reach.

`runtime.inject_bearer_token` makes Lodestar forward its own token when calling
Observatories, so you can configure one secret instead of one per node.

**With no token set, auth is off entirely and it says so on startup.** That's
fine on a bench and wrong on anything routable.

---

## What it won't do

It won't install services — that's the Observatory's job on each node. It won't
invent a node that isn't in the config; discovery probes what you listed, it
doesn't scan your subnet. A `preferred_for` entry can't force work onto a box
that isn't answering. And with zero nodes configured it comes up healthy and
warns loudly rather than failing silently, because "I am running and I have
nothing to do" is a real state worth being able to see.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## License

GPL-3.0-or-later.
