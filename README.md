# hermes-a2a-admin

A backend plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/docs) that adds an
**A2A administration surface**: per-profile status and configuration for the A2A platform.

Two front ends ship with it and share one backend namespace:

- `dashboard/` — a web dashboard tab at `/a2a` (built bundle in `dashboard/dist/`).
- `desktop/` — a native desktop app pane (`desktop/plugin.js`).

## What it shows

- The A2A port per profile and whether the endpoint answers.
- Whether a self token (identity) is present, and in which format.
- The configured peers and their last audit entries.
- The agent card served by the profile, fetched over loopback.

## Requirements

- Hermes Agent with dashboard plugin and desktop plugin support.
- A2A configured for the profiles you want to watch (see the `a2a` platform settings).

## Install

Copy this directory into your Hermes home:

```
<HERMES_HOME>/plugins/hermes-a2a-admin/
```

Then restart the gateway and the dashboard. The web tab appears after *Skills*.

## Files it reads and writes

Per profile home:

| Path | Use |
|---|---|
| `.env` | `A2A_SELF_TOKEN`, `A2A_PEER_TOKENS`, `A2A_BEARER_TOKEN`, `A2A_HOST`, `A2A_TRUSTED_PEERS`, `A2A_PORT` |
| `a2a_audit.jsonl` | latest audit entries shown in the status view |
| `http://127.0.0.1:<port>/.well-known/agent-card.json` | the profile's agent card |

Token generation and token rotation **write `A2A_SELF_TOKEN` into that profile's `.env`**. No
secret value is ever returned by the API or rendered by the UI; the surfaces only report whether a
token is present.

## Backend routes

Namespace `/api/plugins/hermes-a2a-admin/`:

| Route | Purpose |
|---|---|
| `GET /state` | status for every profile |
| `GET /card` | the agent card of one profile |
| `POST /token/generate` | create a self token for a profile |
| `POST /token/rotate` | replace an existing self token |
| `POST /map/sync` | sync the peer map |
| `GET /config/auth`, `POST /config/auth` | read/write the auth configuration |
| `POST /timeout/set` | set the A2A timeout |
| `POST /cleanup/duplicates` | remove duplicate peer entries |
| `POST /peers/toggle` | enable/disable a peer |

## Status

Version 0.1.0. The status views are read-only; every change is an explicit call triggered from the
UI.
