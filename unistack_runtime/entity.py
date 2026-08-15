"""
Resolve an activity's `entity_key` from the agent's own declaration.

The agent says, as plain data in `UNISTACK_CONFIG` (no `unistack` import, as ever):

    "entity_key": "${state.alarm.site_id}:${state.alarm.alarm_class}"

and the runtime renders it against the `initial_state` of each start request. The result is
prefixed with the workflow, because two clients' `MUM-042` are not the same site.

**Why a template and not an LLM.** A wrong boolean is a wrong data point; a wrong *join key* is
a corrupted grouping — it silently merges two entities or splits one, the error does not average
out, and nothing downstream can detect it. This is the single worst place in the system to put a
model, so it is the one place that is pure string substitution.

**Why the runtime and not the projector.** The key must be on the durable record at start:
telemetry is fail-open, so deriving it from a trace would make a rate's denominator depend on
whether an export succeeded. And it cannot be backfilled — terminal checkpoints are deleted.

**Missing is not an error.** An agent whose entity is only known after diagnosis (the site is
identified by the run itself) cannot produce one at start. That is a real gap, and it is recorded
as such: the key is simply absent, cross-activity metrics exclude the activity, and the coverage
is visible. Guessing would be worse.
"""

import logging
import re

logger = logging.getLogger("unistack.runtime")

#: `${state.a.b}` — dotted paths into the initial state. Nothing else is substitutable: a
#: template language here would be a second config surface with no reviewer.
_PLACEHOLDER = re.compile(r"\$\{state\.([A-Za-z0-9_.]+)\}")

#: Keys longer than this are almost certainly a whole payload pasted in by accident, and would
#: make the index they exist for useless.
MAX_KEY_LEN = 256


class EntityKeyError(ValueError):
    """A malformed `entity_key` template. Raised at startup, never mid-activity."""


def validate(template) -> str | None:
    """Check the declaration at boot, so a typo is a refused start rather than a silent gap."""
    if template is None:
        return None
    if not isinstance(template, str) or not template.strip():
        raise EntityKeyError("entity_key must be a non-empty string template, "
                             'e.g. "${state.alarm.site_id}:${state.alarm.alarm_class}"')
    if not _PLACEHOLDER.search(template):
        raise EntityKeyError(
            f"entity_key {template!r} contains no ${{state....}} placeholder, so every activity "
            f"would share one key and every cross-activity metric would be meaningless")
    return template


def _lookup(state: dict, path: str):
    node = state
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def resolve(template: str | None, initial_state: dict, workflow: str) -> str | None:
    """
    Render the template, or return None if any referenced field is absent.

    All-or-nothing on purpose. A key rendered with one half missing
    (`"content:"`, `"MUM-042:"`) is not a partial key — it is a *different* key that silently
    collides with every other activity missing the same field, which is worse than having none.
    """
    if not template:
        return None
    missing: list[str] = []

    def _sub(match) -> str:
        value = _lookup(initial_state or {}, match.group(1))
        if value is None or isinstance(value, (dict, list)):
            missing.append(match.group(1))
            return ""
        return str(value).strip()

    rendered = _PLACEHOLDER.sub(_sub, template).strip()
    if missing:
        logger.warning("entity_key not set: initial_state has no %s — this activity is excluded "
                       "from every cross-activity metric", ", ".join(missing))
        return None
    if not rendered:
        return None
    key = f"{workflow}:{rendered}"
    if len(key) > MAX_KEY_LEN:
        logger.warning("entity_key %r exceeds %d chars — truncated; check the template points at "
                       "an identifier and not a payload", key[:60], MAX_KEY_LEN)
        key = key[:MAX_KEY_LEN]
    return key


__all__ = ["resolve", "validate", "EntityKeyError", "MAX_KEY_LEN"]
