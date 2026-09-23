# Security Policy

## Supported versions

| Component | Supported |
|-----------|-----------|
| Latest release tag | ✅ |
| Older tags | ❌ |

Security fixes are applied to the current release only. Pin to the latest tag.

---

## Threat model

SerenLodestar is a **self-hosted** cluster head. Nothing is sent to a third
party except the optional update check against the package index, which
`updates.enabled: false` switches off.

It is also the most sensitive process in the constellation: it holds a
bearer token for **every node's Observatory**, and through those it can start
and stop services, stop the GPU daemons, push a package and run a script on a
node, and schedule a reboot. Treat its config file and its bind address
accordingly.

| Surface | Default | Notes |
|---------|---------|-------|
| HTTP API | `127.0.0.1:6361` | Loopback only. A host beyond loopback with no bearer **refuses to start** and prints the three ways out; `allow_open_lan: true` overrides with a banner every boot. |
| Bearer token | Not set | Required on everything except `/`, `/health`, `/viewer`, `system/ping` and `system/version`. Set it before widening the bind. Pointers (`bearer_token_env`, `bearer_token_keyring`) keep the secret out of the yaml. |
| Node tokens | Per node, or Lodestar's own | `cluster.nodes[].agent_token`, or with `runtime.inject_bearer_token` the head presents its own bearer to nodes that have none. Either way these live in `seren-lodestar.yaml`; keep it `0600` and out of version control. |
| MCP endpoint (`/mcp/`) | Same host/port | Behind the same bearer. DNS-rebinding protection is off by default for a trusted LAN; `SEREN_LODESTAR_ALLOWED_HOSTS` turns it on. |
| Chat tool loop | Lodestar's own tools, plus `tooling.remote_mcp` | Anything the model emits as a tool call is executed with the head's authority: service control on any node, scheduled tasks. Only attach remote MCP servers you would let the model drive. |
| Reclaim | GPU daemons only | `POST /system/reclaim` stops the pid_file services on each node and never the constellation or the Observatory itself, unless the body says `all: true` or names a service in `include`. |
| Observatory update | Off unless `runtime.agent_package_path` is set | Pushes a tarball and runs `seren-observatory-update.sh` on the node, inside that node's home directory only. |
| Viewer (`/viewer`) | Public shell | The page is public so the token modal can render; every data call carries the bearer. |

---

## Deployment recommendations

- **One box, one person**: the defaults. Loopback, no token, Symposium on
  the same machine.
- **A cluster on your LAN**: set a bearer on Lodestar, set `agent_token` per
  node (or rely on `inject_bearer_token` and give the Observatories the same
  secret), then widen `server.host`. Provision Observatory tokens with
  `seren-secrets.sh`; an Observatory with no token refuses every mutating
  call, so an unprovisioned node cannot be restarted from here.
- **Anything routable from outside the house**: don't. A VPN or SSH tunnel
  in, never the raw port.

---

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Open a [GitHub Security Advisory](https://github.com/ChadRoesler/SerenLodestar/security/advisories/new) (private disclosure). Include:

- A description of the issue and its impact
- Steps to reproduce
- Any relevant config or environment details

You will get a response within **7 days**. If a fix is needed, a patched release will be tagged and the advisory will be published after users have had time to update.
