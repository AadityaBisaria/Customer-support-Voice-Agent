"""Per-session dependencies shared by every node factory and gate handler.

Lives in `flow_manager.state["deps"]` and as the worker's `app_resources`.
Depends only on store ports/domain (never sqlite) and the language tracker.
"""

from dataclasses import dataclass, field

from dialogue.slots import FitContext
from language import LanguageBandTracker
from store.clock import Clock
from store.domain import Customer
from store.ports import SupportStore


@dataclass
class SessionDeps:
    store: SupportStore
    tracker: LanguageBandTracker
    clock: Clock
    customer: Customer | None = None
    verify_attempts: int = 0
    wip: dict = field(default_factory=dict)  # in-progress flow selections

    def fit_ctx(self, attempts: int = 0) -> FitContext:
        return FitContext(now=self.clock.now(), locale="hi-IN", attempts=attempts)
