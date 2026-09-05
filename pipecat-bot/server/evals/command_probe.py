"""Live single-call compiler + real-store regression. Run python -m evals.command_probe."""

import asyncio
import json
import os

from dotenv import load_dotenv
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.vertex.llm import GoogleVertexLLMService

from flows.commands import COMPILER_PROMPT, CommandBatch
from flows.runtime import CommandRuntime
from flows.session import SessionDeps
from language import LanguageBandTracker
from store.clock import IstClock
from store.sqlite import SqliteSupportStore


async def main():
    load_dotenv()
    llm = GoogleVertexLLMService(
        project_id=os.environ['VERTEX_PROJECT_ID'],
        credentials_path=os.environ['VERTEX_CREDENTIALS_PATH'],
        location=os.getenv('VERTEX_LOCATION', 'asia-south1'),
        settings=GoogleVertexLLMService.Settings(
            model=os.getenv('VERTEX_MODEL', 'gemini-2.5-flash'), temperature=0,
            extra={'response_mime_type': 'application/json',
                   'response_json_schema': CommandBatch.model_json_schema()},
        ),
    )
    clock = IstClock()
    deps = SessionDeps(store=SqliteSupportStore.seeded_in_memory(clock),
                       clock=clock, tracker=LanguageBandTracker())
    runtime = CommandRuntime(deps)
    async def turn(text):
        # This external smoke test deliberately sends no runtime/store metadata.
        # All three compiler inputs are fixed synthetic utterances at a fresh menu.
        payload = {'utterance': text, 'state': {'authenticated': deps.customer is not None,
                   'frames': [], 'choices': {}}, 'policy_sources': {}}
        response = await llm.run_inference(LLMContext(messages=[{
            'role': 'user', 'content': json.dumps(payload, default=str)}]),
            system_instruction=COMPILER_PROMPT)
        batch = CommandBatch.model_validate_json(response)
        print('COMMANDS:', batch.model_dump_json().encode('ascii', 'backslashreplace').decode())
        answer = await runtime.apply(batch)
        print('ANSWER:', answer.encode('ascii', 'backslashreplace').decode())
        return answer
    assert 'registered mobile' in await turn('Mere orders kya kya hain?')
    assert 'कोई orders नहीं' in await runtime.shortcut('9111111111')
    assert 'कोई orders नहीं' in await turn('Mujhe apna order cancel karna hai')
    answer = await turn('Cancel the return request I raised yesterday')
    assert 'समझना' in answer
    print('LIVE COMPILER REGRESSIONS PASSED')


if __name__ == '__main__':
    asyncio.run(main())
