"""Shared values of the worker image build and cloud operations tests (fakes only)."""

from __future__ import annotations

from datetime import UTC, datetime

NOW = datetime(2027, 1, 1, 21, 55, tzinfo=UTC)
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests pi-camp-mac"
