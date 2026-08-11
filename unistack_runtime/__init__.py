"""
UniStack graph-runtime — the HTTP service that starts activities and resolves HITL pauses.

Separate from `unistack-sdk` on purpose: the SDK is a LangGraph library for guardrails, HITL
and memory, and must not carry a web server or authentication. This repo owns both.
"""

from unistack_runtime.server import create_app

__all__ = ["create_app"]
