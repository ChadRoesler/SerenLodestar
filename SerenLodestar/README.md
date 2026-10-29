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
      agent_url: "http://192.0.2.100:7777"
      preferred_for: ["whisper", "kokoro"]
      is_host: false

    - name: "xavier"
      agent_url: "http://192.0.2.101:7777"
      preferred_for: ["llama", "comfy"]
      is_host: true
```

Port **7777** is where an Observatory listens. Check one before you trust it:

```bash
curl http://192.0.2.100:7777/api/v1/system/ping
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

**The wire to a node is spelled once.** The node client, the discovery loop
and every shape an Observatory answers with live in `seren_sinew.cluster`
(since 28 Oct 2026); `seren_lodestar.agent_client`, `cluster` and `dtos` are
the same things under the names this service has always used. A contract
test on each side (`tests/test_wire_contract.py` here and in the
Observatory) parses the Observatory's real answers strictly with those
shapes, so the two cannot drift apart quietly again.

**Discovery runs on a loop.** Every `refresh_interval`, every node gets probed
with a `discovery_timeout` of patience. Nodes go offline and come back without
anyone restarting anything, and the capability map — which services exist on
which boxes — updates itself.

**Service control is routed, not addressed.** `POST /api/v1/service/llama/start`
doesn't need to know which machine that is. If you *do* want to be specific,
`/api/v1/node/xavier/service/llama/start` bypasses routing entirely.

**Chat carries a tool loop.** `POST /api/v1/chat` runs inference with tools
attached; `/chat/stream` does it streaming, one generation per round, with the
answer arriving as it is produced. Lodestar's own tools are always attached;
`tooling.remote_mcp` adds other MCP servers (Workbench, for memory and search). The dialect that formats tool calls
for the model lives behind `IToolDialect` — `QwenHermesDialect` is the shipped
one, and swapping model families means writing a new dialect, not editing the
loop. `POST /api/v1/chat/inspect` shows you exactly what got injected when the
answer looks wrong.

**The scheduler fires tools on a clock.** Cron expressions or relative offsets
(`2h`, `30m`, `90s`). State persists to `scheduler.persistence_dir`, defaulting
to a `scheduler/` directory next to your config file (or
`~/seren-lodestar/scheduler` when running on defaults). Tasks survive a
restart; one-shots delete themselves after firing, recurring ones re-arm. A
task fires through the same tool client the chat uses.

**Upgrades are driven, not hoped for.** Release what changed to PyPI, and
`POST /api/v1/upgrade` moves whatever is behind: every online node's seren
services — stores first, then what reads them, the Observatory last on its
node, Lodestar last of all — each to its own newest release. For each one it
pulls a fresh snapshot into the backup stash, then asks the node's
Observatory to upgrade it (see [Upgrades](#upgrades)). The apps move apart:
one that fails is rolled back and the rest carry on.

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
| `GET /` | service info + update status |
| `GET /health` | liveness |
| `GET /viewer` | the operator dashboard (on a short screen, a glance row: see below) |
| `GET /api/v1/system/ping` | public — no token |
| `GET /api/v1/system/version` | public — no token |
| `GET /api/v1/system/status` | per-node status |
| `GET /api/v1/system/health` | cluster health |
| `POST /api/v1/system/reclaim` | stop the GPU daemons (never the constellation, unless told) |
| `POST /api/v1/system/reboot/{node}` | reboot a node |
| `POST /api/v1/system/reboot/{node}/cancel` | change your mind |
| `POST /api/v1/system/agent-update` | push Observatory to every node |
| `POST /api/v1/node/{node}/agent-update` | push Observatory to one node |
| `POST /api/v1/service/{name}/ensure` | hold an app (or llama…) up; a short name finds its install |
| `POST /api/v1/service/{name}/release` | let go; its run policy decides when it stops |
| `GET /api/v1/cluster/leases` | who holds what, for how long |
| `GET /api/v1/orchestration` | run policies, leases, apps, what was started and stopped |
| `POST /api/v1/orchestration/check` | apply the run policies now |
| `POST /api/v1/upgrade` | upgrade the cluster's seren services (`dry_run: true` for the plan) |
| `GET /api/v1/upgrade` | the run going now, and the recent ones |
| `GET /api/v1/upgrade/behind` | every seren service, its version, and the newest on the index |
| `GET /api/v1/upgrade/{run_id}` | one run, step by step |
| `POST /api/v1/cluster/refresh` | re-probe all nodes |
| `POST /api/v1/cluster/refresh/{node}` | re-probe one |
| `GET /api/v1/cluster/capabilities` | the capability map |
| `GET`/`POST` `/api/v1/service/{name}/*` | routed service lifecycle |
| `GET`/`POST` `/api/v1/node/{node}/service/{svc}/*` | pinned to one node |
| `GET`/`POST` `/api/v1/scheduler/tasks` | list / add |
| `DELETE /api/v1/scheduler/tasks/{name}` | remove |
| `POST /api/v1/scheduler/tasks/{name}/pause` | pause |
| `POST /api/v1/scheduler/tasks/{name}/resume` | resume |
| `POST /api/v1/image` | draw a picture: wakes ComfyUI on a node that can, holds it, lets go |
| `POST /api/v1/hear` | words from a clip (multipart `file`, a WAV): wakes whisper, holds it, lets go |
| `POST /api/v1/listen` | words about a sound (multipart `file` + `prompt`): wakes the listener, an audio-language model, holds it, lets go |
| `POST /api/v1/listen/compare` | the same about two clips (`a`, `b`): how they differ |
| `POST /api/v1/speak` | words said in a voice (JSON `{text, voice}`): wakes the speaker on the node that has one, holds it, lets go; the WAV is the body, the node and timings are `X-Seren-*` headers. A voice card that `needs` another service on that box (a big model behind its own server) has it woken and let go alongside, `X-Seren-Needs` says which |
| `GET /api/v1/speak/voices` | the voices the speaker has |
| `POST /api/v1/chat` | inference with tools |
| `POST /api/v1/chat/stream` | the same, streamed |
| `POST /api/v1/chat/inspect` | what got injected, for debugging |
| `GET /api/v1/chat/health` | is the backend up |
| `GET /api/v1/chat/last_user_at` | last human activity |
| `/mcp/` | MCP streamable-HTTP transport |

### The glance screen

The viewer has a second layout for a short viewport (under 400px tall, the
1U touchscreen on a rack): thin header and tabs, and the node grid becomes one
horizontal row, as wide as it has nodes up to five across. Past five it pages,
one card per tap on the arrows at its ends (a resistive touchscreen is not
something you swipe). Nothing is squeezed to fit. The actions card goes and
settings/refresh move into the header strip. Which nodes sit on that row is
the screen's own choice: **settings → On this screen** ticks nodes on and off
for that browser only (localStorage, never the yaml), and a node ticked off
comes back by itself while it is offline or has a service down, so the glance
never hides something that needs you. The LED and the gaps strip count every
node regardless.

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

## Update checking

`GET /` reports whether a newer `seren-lodestar` has been published:

```jsonc
{
  "service": "SerenLodestar",
  "version": "1.4.2",
  "status": "ok",
  "updates": {
    "status": "ok",
    "installed": "1.4.2",
    "latest": "1.5.0",
    "update_available": true
  }
}
```

On by default: update checking is part of the core install (it lives in
`seren-meninges`). A box that never leaves the LAN has no business calling
PyPI; set `enabled: false` there. The knobs:

```yaml
updates:
  enabled: true
  check_interval_hours: 6      # cached; never checked per-request
  allow_prerelease: false
  # index_url: "https://pypi.org/pypi/{distribution}/json"
```

The result is cached and the check never happens in the request path, so `/`
stays fast. `updates.status` is always one of `ok`, `disabled`, `unavailable`
(a broken install: the checker's dependencies are missing) or `error`
— **never absent, and never a silent "you're fine" when it couldn't actually
check.** `SEREN_LODESTAR_UPDATES_ENABLED=false`
turns it off without editing config.

Note `/` is public, so this publishes "running 1.4.2, which is behind" to anyone
who asks. It already published the version; this adds the comparison. Set
`enabled: false` if that bothers you and read it off the dashboard instead.

**Lodestar never swaps its own code in place.** A running process can't
reliably replace itself, so it doesn't pretend to. What it does instead is
hand the job to its node's Observatory and pick the result up after the
restart; see [Upgrades](#upgrades).

---

## Running apps as needed

Not every Seren app needs to be up all the time. The `orchestration:` block
gives each app a run policy (`unmanaged`, `always`, `on_demand`, `scheduled`;
the rules are `seren_sinew.orchestration`), and every `check_seconds` Lodestar
applies them through each node's Observatory: an `on_demand` app nobody has
held for its `idle_minutes` is stopped, a `scheduled` one is started inside its
hours. An app is held by an ensure (`POST /api/v1/service/probe/ensure` — a
short name finds the one install of it) for `lease_ttl_minutes` unless it is
renewed, so a caller that dies lets go on its own. Callers that go through
Sinew's `OnDemandTransport` ensure on a refused connection and renew while
they keep calling. The lease book is kept beside the config, so a restart
doesn't forget what an ensure started. `GET /api/v1/orchestration` shows the
policies, the leases, every app and what was started and stopped lately;
`POST /api/v1/orchestration/check` applies the policies now. The default
policy is the old behaviour exactly. Lodestar and the Observatory are never
managed.

## Upgrades

Snap it, snap the config, upgrade, put it back — without remembering to.

```bash
# What is behind (cached; ?refresh=true asks the index now):
curl -H "Authorization: Bearer $TOKEN" localhost:6361/api/v1/upgrade/behind
# What would move, changing nothing:
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:6361/api/v1/upgrade   -d '{"dry_run": true}'
# Do it (all online nodes, every seren service that is behind; or name "nodes" / "services"):
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:6361/api/v1/upgrade   -d '{"reason": "memory 5.2"}'
# Pin an app (or a run of one app takes "version"):
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:6361/api/v1/upgrade   -d '{"versions": {"memory": "5.2.0"}}'
curl -H "Authorization: Bearer $TOKEN" localhost:6361/api/v1/upgrade
```

**Each app on its own.** The Seren apps are installed, versioned and released
apart: tag and publish the one that changed, and a run moves it (and leaves
what is already on its newest alone — `"only_behind": false` reinstalls
anyway). A run never puts one version on every app: `version` is for a run of
one app, `versions` pins per app, and anything else goes to its own newest.

**Peers.** Where the apps meet is over HTTP — the Callosum reads Memory and
Loci, the Hippocampus writes to Memory — so that contract is what's checked,
not the packages. A reader lists the apps it talks to in its pyproject, as an
extra nobody installs:

```toml
[project.optional-dependencies]
peers = ["seren-memory>=5.2", "seren-loci>=5.0"]
```

meaning "where there is a Memory, it is 5.2 or newer". An app that isn't in
the cluster isn't required, so you can still pick and choose. Lodestar sends
every node's versions with each upgrade; when the new version needs a newer
peer than the cluster runs, the Observatory ends the job **held**, nothing
touched, with the reason (`seren-corpus-callosum 5.3.0 needs
seren-memory>=5.2; seren-memory-wren on nuc is 5.1.0`). Stores go first in a
run, so when Memory and the Callosum both move, the Callosum sees the new
Memory. The stores' side of the deal: within a major version, only add. Raise
a reader's floor in the same commit that starts using what the newer peer
added.

Per service, in order:

0. **Behind?** An app already on the newest release is left alone
   (`current`). A pinned version is always installed.
1. **Backup.** A fresh snapshot is pulled into the stash (the `backup:` block)
   so a copy is off the box before the box changes. If the pull fails, that
   service is not touched, and the run goes on with the rest.
2. **Upgrade.** The node's Observatory runs it as a job: the config and the
   app folder's `.env` files are held, `pip install --upgrade` runs with the
   service's extras *and* `seren-sinew` / `seren-meninges` named (so they move
   to what the new pins allow, rather than staying put), the new version must
   import before anything restarts, then restart, health, and the config put
   back if anything changed it. Any failure there is rolled back on the node:
   the old versions pinned back, the config restored, restarted.
3. **Carry on.** A job that doesn't end `done` fails that app (it was rolled
   back on its node) or holds it (a peer too old), and the run goes on. It
   ends `done`, `partial` (something failed, something moved) or `failed`.
   A service with no pip install the Observatory can see (llama, kokoro — the
   node installer builds those) is skipped.

Lodestar's own upgrade is last. It hands the job to its node's Observatory and
records the hand-off; the Lodestar that comes up afterwards follows the job to
its end and closes the run. Runs are kept as JSON in `upgrades/` beside the
config. The MCP tools `upgrade_behind`, `upgrade_plan`, `upgrade_start` and
`upgrade_status` do the same over MCP.

## What it won't do

It won't swap its own code in place (see Upgrades). It won't install services — that's the
Observatory's job on each node. It won't
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

AGPL-3.0-or-later.
