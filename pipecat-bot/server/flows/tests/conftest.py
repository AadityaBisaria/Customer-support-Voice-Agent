"""Shared fixtures: real seeded in-memory store + a fake FlowManager.

The flow graph is tested WITHOUT any model or pipeline — factories are plain
async functions returning NodeConfig dicts, and gate callbacks are invoked
directly.
"""

from datetime import datetime

import pytest

from flows.session import SessionDeps
from language import LanguageBandTracker
from store.clock import IST, FixedClock
from store.domain import PhoneNumber
from store.sqlite import SqliteSupportStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=IST)  # Wednesday


class FakeFlowManager:
    def __init__(self, deps: SessionDeps) -> None:
        self.state = {"deps": deps}
        self.set_nodes: list[dict] = []
        self.current_node: str | None = None

    async def set_node_from_config(self, node_config: dict) -> None:
        self.set_nodes.append(node_config)
        self.current_node = node_config.get("name")


@pytest.fixture()
def deps() -> SessionDeps:
    clock = FixedClock(NOW)
    return SessionDeps(
        store=SqliteSupportStore.seeded_in_memory(clock),
        tracker=LanguageBandTracker(),
        clock=clock,
    )


@pytest.fixture()
def fm(deps: SessionDeps) -> FakeFlowManager:
    return FakeFlowManager(deps)


@pytest.fixture()
async def priya(deps: SessionDeps):
    customer = await deps.store.customer_by_phone(PhoneNumber("9876543210"))
    deps.customer = customer
    return customer


def tool_names(node: dict) -> set[str]:
    return {schema.name for schema in node.get("functions", [])}
