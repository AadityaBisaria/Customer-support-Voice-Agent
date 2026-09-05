"""Read-only report and terminal conversational nodes."""

from .menu import wrap_tools
from .shared import task_messages


def factual_text(text: str) -> str:
    """Strip legacy factory directives; all remaining facts originate in code."""
    for prefix in ('Tell the user about their refunds: ', 'Tell the user: ',
                   'Tell the user ', 'Apologize: '):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    for instruction in (' Ask if they need anything else.',
                        ' Ask them to try again in a moment.'):
        text = text.replace(instruction, '')
    return text.strip()


async def make_nothing_here(deps, flow_manager, message: str) -> dict:
    return {"name": "nothing_here", "speech": factual_text(message), "task_messages": task_messages(deps, f"{message} Ask if they need anything else."), "functions": wrap_tools()}


async def make_wrap(deps, flow_manager, text: str, *, name: str = "wrap") -> dict:
    if getattr(flow_manager, 'stack', None) is not None:
        return {'name': name, 'speech': factual_text(text), 'functions': wrap_tools()}
    return {"name": name, "task_messages": task_messages(deps, f"{text}\nIf the user brings up another order or task, call the matching function right away — never ask permission, never mention tools."), "functions": wrap_tools()}


async def make_end(deps, flow_manager) -> dict:
    return {"name": "end", "task_messages": task_messages(deps, "Say a short, warm goodbye."), "functions": [], "post_actions": [{"type": "end_conversation"}]}
