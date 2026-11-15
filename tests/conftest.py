"""Shared fixtures: frozen clock helpers, snapshot factory, scripted aiohttp session."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.fakes import (
    FakeAiohttpSession,
    FrozenClock,
    FrozenDateTime,
    make_level,
    make_opportunity,
    make_snapshot,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def freeze_datetime(monkeypatch: pytest.MonkeyPatch) -> Iterator[FrozenClock]:
    """Patch datetime.now used by core.models: yields a controllable wall clock."""
    import core.models as models

    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    controller = FrozenClock()
    yield controller


__all__ = [
    "FakeAiohttpSession",
    "clock",
    "freeze_datetime",
    "make_level",
    "make_opportunity",
    "make_snapshot",
]
