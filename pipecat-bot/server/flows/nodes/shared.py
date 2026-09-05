"""Common node-building primitives."""

from pipecat.flows import FlowsFunctionSchema

from language import short_hint_for_band


def task_messages(deps, text: str) -> list[dict]:
    """One developer task message, always closing with the language hint."""
    content = f"{text}\nReply language: {short_hint_for_band(deps.tracker.band)}"
    if deps.transcript is not None:
        deps.transcript.log_turn("developer", content)
    return [{"role": "developer", "content": content}]


def deps_for(flow_manager):
    return flow_manager.state["deps"]


def routing(name: str, description: str, factory) -> FlowsFunctionSchema:
    """A pure navigation tool; data is collected only by selection nodes."""
    async def handler(args, flow_manager):
        next_node = await factory(deps_for(flow_manager), flow_manager)
        return None, next_node

    return FlowsFunctionSchema(
        name=name,
        description=description,
        properties={},
        required=[],
        handler=handler,
    )
