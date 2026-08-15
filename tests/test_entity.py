"""
`entity_key` resolution — the join key for every cross-activity metric.

The property these tests exist to protect is not "the template renders". It is that a key is
either **complete or absent**, never partial: a half-rendered key silently collides with every
other activity missing the same field, which corrupts a grouping rather than losing a row.
"""

import pytest

from unistack_runtime import entity


def test_renders_a_composite_key_namespaced_by_workflow():
    key = entity.resolve("${state.alarm.site_id}:${state.alarm.alarm_class}",
                         {"alarm": {"site_id": "MUM-042", "alarm_class": "LOS"}}, "network")
    # Namespaced because two clients' MUM-042 are not the same site.
    assert key == "network:MUM-042:LOS"


def test_a_missing_field_yields_no_key_rather_than_a_partial_one():
    """`"network:MUM-042:"` is not a partial key — it is a DIFFERENT key that every activity
    missing `alarm_class` would share, merging unrelated faults into one entity."""
    assert entity.resolve("${state.a}:${state.b}", {"a": "x"}, "w") is None
    assert entity.resolve("${state.a}", {}, "w") is None
    assert entity.resolve("${state.a.b.c}", {"a": {"b": {}}}, "w") is None


def test_a_container_is_not_an_identifier():
    """Rendering a dict or list would stringify a whole payload into the join key."""
    assert entity.resolve("${state.a}", {"a": {"nested": 1}}, "w") is None
    assert entity.resolve("${state.a}", {"a": [1, 2]}, "w") is None


def test_no_declaration_is_legal():
    """An agent whose entity is only known after diagnosis cannot produce one at start. That is a
    real gap, recorded as absence — not guessed at."""
    assert entity.resolve(None, {"topic": "x"}, "content") is None
    assert entity.validate(None) is None


def test_validate_refuses_a_template_that_would_collapse_every_activity():
    """A constant template gives every activity one key, which makes every cross-activity metric
    silently meaningless rather than loudly broken."""
    with pytest.raises(entity.EntityKeyError, match="no .* placeholder"):
        entity.validate("all-the-same")
    for bad in ("", "   ", 123, []):
        with pytest.raises(entity.EntityKeyError):
            entity.validate(bad)


def test_validate_accepts_and_returns_a_good_template():
    tpl = "${state.alarm.site_id}:${state.alarm.alarm_class}"
    assert entity.validate(tpl) == tpl


def test_an_overlong_key_is_truncated_not_stored_whole():
    key = entity.resolve("${state.blob}", {"blob": "x" * 5000}, "w")
    assert len(key) == entity.MAX_KEY_LEN
