"""Read-only report and terminal conversational nodes."""

from .menu import wrap_tools
from .shared import task_messages


async def make_nothing_here(deps, flow_manager, message: str) -> dict:
    return {"name": "nothing_here", "task_messages": task_messages(deps, f"{message} Ask if they need anything else."), "functions": wrap_tools()}


async def make_wrap(deps, flow_manager, text: str, *, name: str = "wrap") -> dict:
    return {"name": name, "task_messages": task_messages(deps, f"{text}\nIf the user brings up another order or task, call the matching function right away — never ask permission, never mention tools."), "functions": wrap_tools()}


async def make_end(deps, flow_manager) -> dict:
    return {"name": "end", "task_messages": task_messages(deps, "Say a short, warm goodbye."), "functions": [], "post_actions": [{"type": "end_conversation"}]}
