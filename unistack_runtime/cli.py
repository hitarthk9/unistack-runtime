"""
`unistack serve module:builder ...` — deploy a graph as a durable HITL runtime with no
hand-written init/compile boilerplate. Mirrors how `langgraph` serves a graph.

The CLI is the consuming app here: it reads its own environment (MONGO_URI,
ANTHROPIC_API_KEY, OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
OTEL_EXPORTER_OTLP_HEADERS, OTEL_SERVICE_NAME, and the UNISTACK_AUTH_MODE /
UNISTACK_OIDC_* / UNISTACK_SCOPE_* / UNISTACK_API_TOKEN / UNISTACK_DEV_IDENTITY /
UNISTACK_TOKEN_SCOPES auth vars) and passes the values explicitly into UniStack — the SDK
itself still reads no environment.

Auth is required. In the default `oidc` mode the JWKS URL, issuer and audience must all be
supplied or the runtime refuses to boot; `--auth-mode token` is a local-dev fallback. There
is no way to serve without authentication.

Governance (workflow / guards / reviews / context) can also be declared as plain data next
to the builder — a module-level `UNISTACK_CONFIG` dict — so a deploy command doesn't need to
carry policy text (which can be long) as shell arguments. This is pure data: the module still
imports nothing from `unistack`, preserving "the author's graph is untouched." CLI flags merge
on top of `UNISTACK_CONFIG` for one-off overrides without a redeploy.
"""

import argparse
import importlib
import os
import pathlib
import sys


def _load_builder_and_config(spec: str):
    """
    Import 'package.module:attribute' for the StateGraph builder. Also reads a sibling
    module-level `UNISTACK_CONFIG` dict from the same module, if present — absent is fine,
    returned as {} (fully backward compatible with builder-only modules).
    """
    if ":" not in spec:
        sys.exit("builder must be 'module:attribute', e.g. agent:builder")
    module_path, attr = spec.split(":", 1)
    module = importlib.import_module(module_path)
    builder = getattr(module, attr)
    config = getattr(module, "UNISTACK_CONFIG", {})
    return builder, config


def _load_knowledge_bases(spec: str) -> dict:
    """
    Load `knowledge/*.yaml` from beside the agent module — the policy a guard names.

    The CLI does this, not the SDK: the SDK reads no environment and loads no files
    (hard constraint #9), so it takes parsed data. Keeping the YAML next to the agent is the
    same reasoning as the agent's langfuse/ folder — a knowledge base encodes THIS agent's
    business policy,
    so it belongs in the same commit as the agent it governs.

    A knowledge base that fails to parse EXITS rather than starting a runtime whose guards
    would silently judge against a partial policy.
    """
    module = importlib.import_module(spec.split(":", 1)[0])
    # A module with no __file__ (namespace package, or one built in memory) has no directory to
    # look beside — that is "no knowledge bases", not an error.
    origin = getattr(module, "__file__", None)
    if not origin:
        return {}
    directory = pathlib.Path(origin).parent / "knowledge"
    if not directory.is_dir():
        return {}

    import yaml
    bases = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text())
        except Exception as exc:
            sys.exit(f"[UniStack] cannot parse knowledge base {path}: {exc}")
        name = (doc or {}).get("knowledge_base")
        if not name:
            sys.exit(f"[UniStack] {path} has no 'knowledge_base' name")
        bases[name] = doc
    return bases


def _build_auth(args, AuthConfig):
    """
    Auth settings come from the environment, like MONGO_URI and every OTEL_* var; only the
    two you actually type in local dev are flags. Misconfiguration EXITS rather than falling
    back to an open runtime — refusing to boot is what makes "no unauthenticated mode" real.
    """
    env = os.environ.get
    mode = (args.auth_mode or env("UNISTACK_AUTH_MODE") or "oidc").lower()
    try:
        if mode == "oidc":
            return AuthConfig.oidc(jwks_url=env("UNISTACK_OIDC_JWKS_URL", ""),
                                   issuer=env("UNISTACK_OIDC_ISSUER", ""),
                                   audience=env("UNISTACK_OIDC_AUDIENCE", ""))
        if mode == "token":
            return AuthConfig.static_token(token=args.token or env("UNISTACK_API_TOKEN", ""),
                                           identity=env("UNISTACK_DEV_IDENTITY", "dev@local"),
                                           scopes=env("UNISTACK_TOKEN_SCOPES") or None)
    except ValueError as exc:
        sys.exit(f"[UniStack] cannot start: {exc}.\n"
                 "  Set UNISTACK_OIDC_JWKS_URL / _ISSUER / _AUDIENCE,\n"
                 "  or run local dev with:  --auth-mode token --token <secret>")
    sys.exit(f"[UniStack] unknown --auth-mode {mode!r} (expected 'oidc' or 'token')")


def _serve(args) -> None:
    from unistack import UniStack

    from unistack_runtime import entity
    from unistack_runtime.server import AuthConfig, create_app
    import uvicorn

    # Resolve auth first: a misconfiguration should exit before any Mongo/LLM setup work.
    auth = _build_auth(args, AuthConfig)
    builder, config = _load_builder_and_config(args.builder)
    knowledge_bases = _load_knowledge_bases(args.builder)

    workflow = args.workflow or config.get("workflow")
    if not workflow:
        sys.exit("workflow is required: pass --workflow, or set 'workflow' in the module's "
                 "UNISTACK_CONFIG")

    guards = dict(config.get("guards") or {})
    for flag in (args.guard or []):
        node, _, policy = flag.partition("=")
        # `--guard NODE=POLICY` can only ever express prose. Letting it overwrite a guard that
        # names a knowledge base would silently drop the entire policy and leave a guard that
        # looks configured but judges against one sentence — refuse instead.
        if isinstance(guards.get(node), dict):
            sys.exit(f"[UniStack] --guard {node}=... would replace a knowledge-base guard with "
                     f"plain text, dropping its policy. Edit the agent's UNISTACK_CONFIG or its "
                     f"knowledge/ files instead.")
        guards[node] = policy
    reviews = sorted(set(config.get("reviews") or []) | set(args.review or []))
    context = args.context if args.context is not None else config.get("context")

    sdk = UniStack.init(
        workflow=workflow,
        mongo_uri=os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        # Standard OTel env vars; the signal-specific TRACES endpoint wins when both set.
        otel_endpoint=(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
                       or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None),
        otel_headers=os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") or None,
        otel_service_name=os.environ.get("OTEL_SERVICE_NAME") or f"unistack-{workflow}",
        context=context,
        # Point the judge at an OpenAI-compatible endpoint (a gateway) so its calls are
        # metered and budget-capped. UNISTACK_GUARDRAIL_MODEL is then whatever name that
        # endpoint exposes, e.g. "judge-fast".
        llm_base_url=os.environ.get("UNISTACK_LLM_BASE_URL") or None,
        llm_api_key=os.environ.get("UNISTACK_LLM_API_KEY") or None,
        # Parsed here, never read by the SDK — see _load_knowledge_bases.
        knowledge_bases=knowledge_bases,
        **({"guardrail_model": os.environ["UNISTACK_GUARDRAIL_MODEL"]}
           if os.environ.get("UNISTACK_GUARDRAIL_MODEL") else {}),
    )
    graph = sdk.compile(builder, guards=guards, reviews=reviews)
    # Validated at boot, so a malformed template is a refused start rather than a
    # silent gap discovered when someone asks why a KRA has no denominator.
    try:
        entity_key_template = entity.validate(config.get('entity_key'))
    except entity.EntityKeyError as exc:
        sys.exit(f'[UniStack] {exc}')

    # flush=True: stdout is block-buffered when piped to a container log, and an auth
    # warning nobody sees until shutdown is worthless.
    auth_line = (f"auth=oidc issuer={auth.issuer} aud={','.join(auth.audience)}"
                 if auth.mode == "oidc" else
                 f"auth=token (LOCAL DEV ONLY — identity is NOT verified; every resolution "
                 f"is attributed to '{auth.identity}') scopes={sorted(auth.token_scopes)}")
    kb_line = f" knowledge={sorted(knowledge_bases)}" if knowledge_bases else ""
    # Printed because its ABSENCE is the interesting case: no entity_key means every
    # cross-activity metric silently has no denominator, and that should be visible at boot
    # rather than discovered from an empty dashboard.
    ek_line = (f" entity_key={entity_key_template!r}" if entity_key_template
               else " entity_key=NONE (cross-activity metrics unavailable)")
    print(f"[UniStack] serving '{workflow}' from {args.builder} (guards={list(guards)}, "
          f"reviews={reviews}{kb_line}{ek_line}) on {args.host}:{args.port}"
          f"\n[UniStack] {auth_line}",
          flush=True)

    try:
        uvicorn.run(create_app(sdk, graph, auth=auth,
                               entity_key_template=entity_key_template),
                    host=args.host, port=args.port)
    finally:
        # Span flushing happens in create_app's lifespan shutdown hook, NOT here: on SIGTERM
        # uvicorn exits without returning, so this block never runs. Kept for the non-signal
        # exit paths (a bind failure, Ctrl-C in some terminals); close() is idempotent.
        sdk.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="unistack")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Serve a compiled graph as a durable HITL runtime.")
    serve.add_argument("builder", help="StateGraph builder as 'module:attribute'")
    serve.add_argument("--workflow", default=None,
                       help="Workflow name (project / activity prefix). "
                            "Falls back to the module's UNISTACK_CONFIG['workflow'] if omitted.")
    serve.add_argument("--guard", action="append", metavar="NODE=POLICY",
                       help="Guard a node (repeatable): --guard generate='No unverified claims'. "
                            "Merges with (and overrides per-key) UNISTACK_CONFIG['guards'].")
    serve.add_argument("--review", action="append", metavar="NODE",
                       help="Require human sign-off after a node (repeatable). Merges with "
                            "UNISTACK_CONFIG['reviews'].")
    serve.add_argument("--context", default=None,
                       help="Business context for the guardrail judge. Overrides "
                            "UNISTACK_CONFIG['context'] if given.")
    # Auth settings are env-driven, like MONGO_URI and OTEL_*; only these two are flags,
    # because they are what you type by hand in local dev.
    serve.add_argument("--auth-mode", choices=("oidc", "token"), default=None,
                       help="'oidc' (default) validates JWTs against UNISTACK_OIDC_JWKS_URL / "
                            "_ISSUER / _AUDIENCE; 'token' is a LOCAL-DEV static bearer token. "
                            "Env: UNISTACK_AUTH_MODE. There is no unauthenticated mode.")
    serve.add_argument("--token", default=None,
                       help="Static bearer token for --auth-mode token. Env: UNISTACK_API_TOKEN "
                            "(see also UNISTACK_DEV_IDENTITY, UNISTACK_TOKEN_SCOPES)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
