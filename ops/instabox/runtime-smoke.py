"""Exercise the installed runtime and budget guard without external model calls."""
import asyncio
import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, '/app/backend')

from fastapi import HTTPException
from open_webui.utils import context_compaction as cc


async def main():
    config = await cc._load_config()
    assert config['enable'], 'Smoke instance must enable compaction'
    request = SimpleNamespace(state=SimpleNamespace())
    user = SimpleNamespace(role='admin')
    models = {'smoke': {'id': 'smoke', 'info': {'params': {}}}}
    text = '\n'.join(f'{i:08x}: 搜索结果与代码 return value_{i} + {i*i}' for i in range(5000))
    assert cc._estimate_tokens(text) > 100000
    question = 'Keep the exact question and sources.'
    payload = {
        'model': 'smoke', 'metadata': {'chat_id': 'runtime-smoke', 'base_user_prompt': question},
        'messages': [
            {'role': 'user', 'content': question},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'fetch_url', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': text},
        ],
    }
    original = copy.deepcopy(payload['messages'])
    cc._generate_summary = AsyncMock(return_value='Summary with original source references retained.')
    await cc.enforce_context_budget(request, payload, user, models)
    assert cc._estimate_messages_tokens(payload['messages']) <= 80000
    assert payload['messages'][0]['content'] == question
    assert payload['messages'][-1]['tool_call_id'] == 'call-1'
    assert original[-1]['content'] == text
    cc._generate_summary = AsyncMock(side_effect=RuntimeError('synthetic summary failure'))
    payload['messages'] = original
    payload['metadata'].pop('_context_result_summaries', None)
    try:
        await cc.enforce_context_budget(request, payload, user, models)
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError('Failed compaction must stop the oversized request')
    print('PASS: installed runtime imports, real tokenizer, final budget, intact question/pairing, and failure blocking')


asyncio.run(main())
