# UniStack Runtime

The HTTP graph-runtime: it starts activities and resolves HITL pauses. `unistack serve` lives
here.

## Why it is separate from the SDK

`unistack-sdk` is the LangGraph connection for **guardrails, HITL and memory** — a library. A
web server and its authentication are not that. Keeping them apart means the SDK ships with no
fastapi, no uvicorn, and no auth code, and can be embedded in anything.

| Repo | Owns |
|---|---|
| `unistack-sdk` | guards, reviews, HITL, checkpointing, OTel. A library. |
| **`unistack-runtime`** (here) | the HTTP surface + authentication + `unistack serve`. |
| `unistack-auth` | token verification, shared by every service. |
| `unistack-api` | read-only views (pending approvals, history). |

Dependency direction is one-way: this service imports the SDK; the SDK never imports this.

## Endpoints

| Method | Path | Scope required |
|---|---|---|
| `POST` | `/activities` | `activity.start` |
| `POST` | `/activities/{id}/resolve` | `activity.resolve` |
| `GET` | `/health` | none — liveness probes carry no token |

401 for a bad or missing token, **403** for a valid token missing the scope, 404 for an unknown
activity, 422 for a bad `decision` or for sending `resolved_by` in the body.

## Auth

**Mandatory and not omittable** — `auth` is a required keyword-only argument to `create_app`,
and `unistack serve` exits rather than degrading when config is incomplete. There is no open
mode. Verification itself comes from `unistack-auth`; see its CLAUDE.md for the claim mapping
and status-code taxonomy.

```bash
# Production — env-driven, like MONGO_URI and OTEL_*
export UNISTACK_OIDC_JWKS_URL=https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys
export UNISTACK_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
export UNISTACK_OIDC_AUDIENCE=api://unistack-runtime
unistack serve my_app.graph:builder

# Local dev — no IdP needed; identity is CONFIGURED, never taken from the caller
unistack serve my_app.graph:builder --auth-mode token --token dev-secret
```

> **Grant `activity.start` and `activity.resolve` to DIFFERENT identities.** Separation is what
> stops an agent approving its own guardrail breaches, but only if an operator actually grants
> them apart. Closing it properly needs `started_by` on the activity record — BUILD_PLAN item 3.

**The approver cannot be forged.** `resolved_by` is rejected in the request body (422); the
verified principal becomes the `Resolver` the SDK writes to the audit record, along with
`resolved_by_subject`, `resolved_by_issuer` and `resolved_auth_mode` — the last distinguishing
a verified approver from a dev-mode attribution.

## Governance as data

`unistack serve` auto-discovers a module-level `UNISTACK_CONFIG` dict beside the builder, so a
deploy command carries no policy text. That dict is **plain data** — the author's module still
imports nothing from UniStack. CLI flags merge on top for one-off overrides.

### Knowledge bases

It also loads every `knowledge/*.yaml` sitting beside that module and passes the parsed dicts to
`UniStack.init(knowledge_bases=...)`, so a guard can name one:
`guards={"generate": {"knowledge_base": "brand-policy"}}`. **This CLI is the only file loader** —
the SDK reads no files (its hard constraint #9), so it takes data. A base that fails to parse, or
carries no `knowledge_base` name, **exits** rather than serving guards that would judge against a
partial policy.

⚠️ `--guard NODE=POLICY` can only ever express prose, so pointing it at a node whose config has a
knowledge-base guard would silently downgrade that guard to one sentence — a guard that still
looks configured. The merge **refuses** in that case instead of overwriting. Overriding a plain
string guard with another string is unchanged.

## Environment variables

| Var | Purpose |
|---|---|
| `MONGO_URI` | checkpointer + `hitl_resolutions` |
| `UNISTACK_AUTH_MODE` | `oidc` (default) or `token` (local dev) |
| `UNISTACK_OIDC_JWKS_URL` / `_ISSUER` / `_AUDIENCE` | required in oidc mode |
| `UNISTACK_API_TOKEN` / `UNISTACK_DEV_IDENTITY` / `UNISTACK_TOKEN_SCOPES` | token mode |
| `UNISTACK_LLM_BASE_URL` / `UNISTACK_LLM_API_KEY` / `UNISTACK_GUARDRAIL_MODEL` | the guardrail judge's gateway (see `unistack-gateway`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_HEADERS` / `OTEL_SERVICE_NAME` | tracing |
| `ANTHROPIC_API_KEY` | legacy; the judge now goes through the gateway |

Knowledge bases are **not** an env var — they are files next to the agent, discovered by path.

The SDK reads none of these — this CLI reads them and passes them in explicitly.

## Files

```
unistack_runtime/
  server.py   ← create_app(sdk, graph, *, auth): start + resolve, nothing else
  cli.py      ← `unistack serve module:builder …`, env → AuthConfig, UNISTACK_CONFIG discovery
tests/
  test_server.py  ← scope separation, 401/403/422, unforgeable approver
  test_cli.py     ← flag/config merge semantics, auth config from env
```

Server tests use static-token mode: OIDC verification is proven once in `unistack-auth`, and
what this repo must prove is that the two scopes are enforced separately.

## Install & test

```bash
python3.13 -m venv venv
venv/bin/python -m pip install -e ../unistack-auth -e ../unistack-sdk   # local dev
venv/bin/python -m pip install -e ".[dev]"
PYTHONPATH=. venv/bin/python -m pytest tests/ -v      # needs MongoDB on localhost:27017
```
