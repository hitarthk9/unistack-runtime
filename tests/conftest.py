"""
Fixtures for the graph-runtime suite. Requires MongoDB on localhost:27017 (isolated
"unistack_test" database).

No RSA/JWKS harness here: token verification lives in `unistack-auth` and is tested there,
against a generated keypair. What this repo has to prove is different — that `activity.start`
and `activity.resolve` are enforced separately, and that the approver cannot be forged — and
static-token mode proves both without an identity provider.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_unistack_env(monkeypatch):
    """A developer's exported UNISTACK_*/OTEL_* vars must not change test results."""
    for name in [k for k in os.environ if k.startswith(("UNISTACK_", "OTEL_"))]:
        monkeypatch.delenv(name, raising=False)
