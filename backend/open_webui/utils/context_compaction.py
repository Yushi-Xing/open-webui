from __future__ import annotations

import copy
import hashlib
import logging
import re
from collections import deque
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.utils.chat_id import is_saved_chat_id
from open_webui.utils.context_tokens import estimate_text_tokens, split_text_to_budget
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import get_content_from_message, get_last_user_message, get_message_list
from open_webui.utils.payload import apply_params_to_form_data
from open_webui.utils.task import (
    prompt_template,
    prompt_variables_template,
    replace_messages_variable,
    replace_prompt_variable,
)

log = logging.getLogger(__name__)

BASE64_DATA_URI_RE = re.compile(r'data:[\w/+.;=%-]*;base64,[A-Za-z0-9+/=]*')

DEFAULT_CONTEXT_COMPACTION_PROMPT = """### Task:
Summarize the conversation history that will be compacted out of the active chat context.

### Instructions:
- Preserve key decisions, user preferences, and constraints.
- Preserve files, artifacts, tool results, and code changes that matter going forward.
- Preserve relevant source URLs, citation identifiers, errors, and exact values needed for the task.
- Preserve the current task state, unresolved questions, and next steps.
- Be factual and specific. Do not invent details.
- Keep the summary concise, but complete enough for the assistant to continue without the removed messages.

### Previous Summary:
{{PREVIOUS_SUMMARY}}

### Messages Being Compacted:
{{COMPACTED_MESSAGES}}

### Recent Messages Kept In Context:
{{RECENT_MESSAGES}}"""


async def _emit_compaction_event(event_emitter, event):
    if event_emitter:
        try:
            await event_emitter(event)
        except Exception as exc:
            # A broken progress channel must not abort a successful compaction.
            # Do not log transport exception text, which can contain credentials.
            log.warning('Context compaction progress notification failed: %s', type(exc).__name__)


async def compact_messages_for_request(
    request,
    user,
    messages: list[dict],
    metadata: dict,
    model_id: str,
    models: dict,
    system_prompt: str = '',
    *,
    token_threshold: int | None = None,
    persist: bool = True,
) -> tuple[list[dict], str | None, bool]:
    config = await _load_config()
    if not config['enable']:
        return messages, None, False

    system_messages = [messages[0]] if messages and messages[0].get('role') == 'system' else []
    messages = messages[1:] if system_messages else messages

    messages, previous_summary = _apply_latest_summary_checkpoint(messages)
    system_prompt = system_prompt or (get_content_from_message(system_messages[0]) if system_messages else '')
    token_threshold = token_threshold or _resolve_token_threshold(
        config['token_threshold'], config['token_cap'], metadata
    )
    if not _exceeds_token_threshold(messages, system_prompt, previous_summary, token_threshold):
        return [*system_messages, *messages], previous_summary, False

    boundary = _find_compaction_boundary(messages, config['retention_percentage'], token_threshold)
    compacted_messages = messages[:boundary]
    recent_messages = messages[boundary:]
    if not compacted_messages or not recent_messages:
        return [*system_messages, *messages], previous_summary, False

    # A tool boundary can fall after the current question. Keep the exact
    # question alongside its call/result batch, rather than only in a summary.
    if recent_messages[0].get('role') != 'user':
        latest_user = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
        if latest_user is not None and any(m is latest_user for m in compacted_messages):
            recent_messages = [latest_user, *recent_messages]
            compacted_messages = [m for m in compacted_messages if m is not latest_user]
            if not compacted_messages:
                return [*system_messages, *messages], previous_summary, False

    event_emitter = None
    if persist and metadata.get('chat_id') and metadata.get('message_id'):
        from open_webui.socket.main import get_event_emitter

        event_emitter = await get_event_emitter(metadata)

    if event_emitter:
        await _emit_compaction_event(
            event_emitter,
            {
                'type': 'context_compaction',
                'data': {
                    'action': 'context_compaction',
                    'description': 'Compacting context',
                    'done': False,
                },
            },
        )

    try:
        summary = await _generate_summary(
            request,
            user,
            model_id,
            models,
            compacted_messages,
            recent_messages,
            previous_summary,
            config['prompt_template'],
        )
    except Exception:
        if event_emitter:
            await _emit_compaction_event(
                event_emitter,
                {
                    'type': 'context_compaction',
                    'data': {
                        'action': 'context_compaction',
                        'description': 'Context compaction failed',
                        'done': True,
                        'error': True,
                    },
                },
            )
        raise

    chat_id = metadata.get('chat_id')
    checkpoint_message_id = (
        recent_messages[0].get('id') or metadata.get('user_message_id') or metadata.get('message_id')
    )
    # Only whole-turn user boundaries correspond to persisted chat checkpoints.
    if persist and recent_messages[0].get('role') == 'user' and is_saved_chat_id(chat_id) and checkpoint_message_id:
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            checkpoint_message_id,
            {'contextSummary': summary},
            touch=False,
        )

    log.info(
        'Compacted chat context for chat=%s checkpoint=%s response=%s dropped=%d kept=%d summary_chars=%d',
        chat_id,
        checkpoint_message_id,
        metadata.get('message_id'),
        len(compacted_messages),
        len(recent_messages),
        len(summary),
    )

    if event_emitter:
        await _emit_compaction_event(
            event_emitter,
            {
                'type': 'context_compaction',
                'data': {
                    'action': 'context_compaction',
                    'description': 'Context compacted',
                    'done': True,
                    'checkpoint_message_id': (
                        recent_messages[0].get('id') if persist and recent_messages[0].get('role') == 'user' else None
                    ),
                    'context_summary': summary,
                },
            },
        )

    return [*system_messages, *recent_messages], summary, True


async def compact_chat_branch(request, user, chat: Any, model_id: str, models: dict) -> dict:
    config = await _load_config()
    if not config['enable']:
        return {'ok': True, 'compacted': False, 'reason': 'disabled'}

    chat_data = chat.chat or {}
    history = chat_data.get('history') or {}
    current_id = getattr(chat, 'current_message_id', None) or history.get('currentId')
    if not current_id:
        current_id = chat_data.get('currentId') or chat_data.get('branchPointMessageId')
    if not current_id and isinstance(chat_data.get('messages'), list) and chat_data['messages']:
        current_id = chat_data['messages'][-1].get('id')
    if not current_id:
        return {'ok': True, 'compacted': False, 'reason': 'empty'}

    messages_map = await Chats.get_messages_map_by_chat_id(chat.id)
    if not messages_map:
        messages_map = history.get('messages') or {}

    messages, previous_summary = _apply_latest_summary_checkpoint(get_message_list(messages_map, current_id))
    compacted_messages = messages[:-1]
    recent_messages = messages[-1:]
    if not compacted_messages or not recent_messages:
        return {'ok': True, 'compacted': False, 'reason': 'too_short'}

    summary = await _generate_summary(
        request,
        user,
        model_id,
        models,
        compacted_messages,
        recent_messages,
        previous_summary,
        config['prompt_template'],
    )
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat.id, current_id, {'contextSummary': summary}, touch=False
    )

    return {
        'ok': True,
        'compacted': True,
        'dropped_messages': len(compacted_messages),
        'kept_messages': len(recent_messages),
        'summary_chars': len(summary),
    }


async def _load_config() -> dict:
    values = await Config.get_many(
        'chat.context_compaction.enable',
        'chat.context_compaction.token_threshold',
        'chat.context_compaction.token_cap',
        'chat.context_compaction.retention_percentage',
        'chat.context_compaction.prompt_template',
    )
    token_threshold = _parse_positive_int(values.get('chat.context_compaction.token_threshold')) or 80000
    return {
        'enable': bool(values.get('chat.context_compaction.enable', False)),
        'token_threshold': token_threshold,
        'token_cap': _parse_positive_int(values.get('chat.context_compaction.token_cap')) or token_threshold,
        'retention_percentage': _clamp_retention_percentage(values.get('chat.context_compaction.retention_percentage')),
        'prompt_template': values.get('chat.context_compaction.prompt_template', '') or '',
    }


def _parse_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clamp_retention_percentage(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 40
    return min(50, max(10, parsed))


def _resolve_token_threshold(global_threshold: int, global_cap: int, metadata: dict) -> int:
    configured_threshold = _parse_positive_int((metadata.get('params') or {}).get('compact_token_threshold'))
    return min(configured_threshold or global_threshold, global_cap)


def _usage_token_count(usage: dict) -> int:
    prompt_tokens = int(usage.get('prompt_tokens') or usage.get('prompt_eval_count') or 0)
    if not prompt_tokens and (usage.get('prompt_n') is not None or usage.get('cache_n') is not None):
        prompt_tokens = int(usage.get('prompt_n') or 0) + int(usage.get('cache_n') or 0)
    if not prompt_tokens:
        prompt_tokens = int(usage.get('input_tokens') or 0)

    completion_tokens = int(
        usage.get('completion_tokens')
        or usage.get('output_tokens')
        or usage.get('eval_count')
        or usage.get('predicted_n')
        or 0
    )
    return prompt_tokens + completion_tokens


async def get_chat_context_usage(chat: Any, model_id: str | None = None) -> dict | None:
    chat_data = chat.chat or {}
    history = chat_data.get('history') or {}
    current_id = getattr(chat, 'current_message_id', None) or history.get('currentId')
    if not current_id:
        current_id = chat_data.get('currentId') or chat_data.get('branchPointMessageId')
    if not current_id and isinstance(chat_data.get('messages'), list) and chat_data['messages']:
        current_id = chat_data['messages'][-1].get('id')
    if not current_id:
        return None

    messages_map = await Chats.get_messages_map_by_chat_id(chat.id)
    messages = get_message_list(messages_map or history.get('messages') or {}, current_id)
    if not messages:
        return None

    config = await _load_config()
    if not config['enable']:
        return None

    params = ((chat.chat or {}).get('params') or {}).copy()
    if model_id:
        params['model'] = model_id
    threshold = _resolve_token_threshold(config['token_threshold'], config['token_cap'], {'params': params})
    messages, previous_summary = _apply_latest_summary_checkpoint(messages)

    # Stored usage describes an earlier provider request, including history
    # that a checkpoint may have removed. Count the active branch instead.
    tokens = _estimate_tokens(previous_summary or '') + _estimate_messages_tokens(messages)
    return _build_context_usage(tokens, threshold)


def _build_context_usage(tokens: int, threshold: int) -> dict:
    return {
        'tokens': tokens,
        'estimated_tokens': tokens,
        'threshold': threshold,
        'percent': round((tokens / threshold) * 100) if threshold > 0 else 0,
        'source': 'estimated',
    }


def _apply_latest_summary_checkpoint(messages: list[dict]) -> tuple[list[dict], str | None]:
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        value = message.get('contextSummary') or message.get('context_summary')
        if isinstance(value, str) and value.strip():
            return messages[idx:], value

        marker = next(
            (
                item
                for item in reversed(message.get('output') or [])
                if item.get('type') == 'open_webui:context_compaction'
            ),
            None,
        )
        if not marker:
            continue

        # A tool-loop checkpoint can retain output from an earlier reply.
        for cut_idx in range(idx, -1, -1):
            output = messages[cut_idx].get('output') or []
            start = next(
                (
                    position
                    for position, item in enumerate(output)
                    if item.get('type') == 'function_call' and item.get('call_id') == marker['call_id']
                ),
                None,
            )
            if start is not None:
                while start and output[start - 1].get('type') != 'function_call_output':
                    start -= 1
                return [
                    *messages[max(0, cut_idx - 1) : cut_idx],
                    {
                        **messages[cut_idx],
                        'output': [
                            item for item in output[start:] if item.get('type') != 'open_webui:context_compaction'
                        ],
                    },
                    *messages[cut_idx + 1 :],
                ], marker['summary']

    return messages, None


def _exceeds_token_threshold(messages: list[dict], system_prompt: str, summary: str | None, threshold: int) -> bool:
    if threshold <= 0:
        return False

    # Provider usage is a receipt for a past request, not the size of this
    # branch after checkpoint replay. An old large receipt otherwise causes
    # the same compacted history to be summarized again on every follow-up.
    return (
        _estimate_tokens(system_prompt) + _estimate_tokens(summary or '') + _estimate_messages_tokens(messages)
        > threshold
    )


def _find_compaction_boundary(
    messages: list[dict], retention_percentage: int = 40, token_budget: int | None = None
) -> int:
    retention_percentage = _clamp_retention_percentage(retention_percentage)
    keep_count = max(2, len(messages) * retention_percentage // 100)
    target = max(1, len(messages) - keep_count)
    boundaries = []
    pending = set()
    for idx, message in enumerate(messages):
        calls = message.get('tool_calls') or []
        # A real user turn or the start of a complete call/result batch is a
        # safe cut. Generated user image messages stay with their tool batch.
        content = message.get('content')
        tool_images = (
            isinstance(content, list)
            and content
            and isinstance(content[0], dict)
            and content[0].get('text') == 'Here are the images from the tool results above. Please analyze them.'
        )
        user_turn = message.get('role') == 'user' and not tool_images
        if idx and not pending and (user_turn or (message.get('role') == 'assistant' and calls)):
            boundaries.append(idx)
        if message.get('role') == 'assistant':
            pending.update(call['id'] for call in calls if call.get('id'))
        elif message.get('role') == 'tool':
            pending.discard(message.get('tool_call_id'))
    target = max(target, min(boundaries, default=0))
    boundary = next((idx for idx in reversed(boundaries) if idx <= target), 0)
    if boundary and token_budget:
        # Retaining 40% of messages need not retain 40% of tokens. Advance to
        # another safe boundary when the retained portion alone is too large.
        for candidate in boundaries:
            if candidate >= boundary:
                boundary = candidate
                if _estimate_messages_tokens(messages[candidate:]) <= token_budget * 3 // 4:
                    break
    return boundary


class _SummaryInputTooLarge(ValueError):
    pass


class _CompactionFailure(ValueError):
    def __init__(self, code: str, reason: str, provider_status: int | None = None):
        self.code = code
        self.reason = reason
        self.provider_status = provider_status
        super().__init__(reason)


async def _generate_summary(
    request, user, model_id, models, compacted_messages, recent_messages, previous_summary, summary_prompt_template
) -> str:
    # A summary must not send the same oversized input to the same short-window
    # model. Try the original template first, then fold bounded chunks in order.
    config = await _load_config()
    budget = min(config['token_threshold'], config['token_cap'], 16000)
    args = (request, user, model_id, models)
    try:
        return await _generate_summary_once(
            *args, compacted_messages, recent_messages, previous_summary, summary_prompt_template, budget
        )
    except _SummaryInputTooLarge:
        pass

    text_parts = [previous_summary] if previous_summary else []
    for message in compacted_messages:
        text_parts.append(f'[{message.get("role", "unknown")}] {get_content_from_message(message) or ""}')
        if message.get('tool_calls'):
            text_parts.append('[TOOL CALLS] ' + JSONCodec.dumps(message['tool_calls'], ensure_ascii=False))
    text = '\n'.join(text_parts)
    # Recent messages remain in the main request. A bounded preview supplies
    # continuity to the summarizer without duplicating a huge retained result.
    question = get_last_user_message(recent_messages) or ''
    preview_budget = max(4, min(2048, budget // 8))
    if _estimate_tokens(question) > preview_budget:
        head = next(split_text_to_budget(question, max(4, preview_budget // 2)))
        tail = next(split_text_to_budget(question[::-1], max(4, preview_budget // 2)))[::-1]
        question = head + '\n[Summary context preview shortened]\n' + tail
    recent = [{'role': 'user', 'content': question}] if question else []
    chunks = deque(split_text_to_budget(text, max(4, budget * 3 // 4)))
    summary = None
    calls = 0
    while chunks:
        if calls >= 64:
            raise _CompactionFailure('summary_chunk_limit', 'Context requires more than 64 summary chunks.')
        chunk = chunks.popleft()
        try:
            summary = await _generate_summary_once(
                *args, [{'role': 'user', 'content': chunk}], recent, summary, summary_prompt_template, budget
            )
            calls += 1
        except _SummaryInputTooLarge:
            if len(chunk) <= 1:
                raise _CompactionFailure(
                    'summary_prompt_too_large',
                    'Compaction prompt or previous summary exceeds the summary input budget.',
                )
            mid = len(chunk) // 2
            chunks.extendleft([chunk[mid:], chunk[:mid]])
    if not summary:
        raise _CompactionFailure('summary_empty', 'The compaction model returned no usable summary.')
    return summary


async def _generate_summary_once(
    request,
    user,
    model_id: str,
    models: dict,
    compacted_messages: list[dict],
    recent_messages: list[dict],
    previous_summary: str | None,
    summary_prompt_template: str,
    input_budget: int,
) -> str:
    from open_webui.utils.chat import generate_chat_completion

    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {**dict(models.items()), request.state.model['id']: request.state.model}

    task_config = await Config.get_many(
        'task.model.params',
        'chat.context_compaction.model',
    )
    context_compaction_model = task_config.get('chat.context_compaction.model')
    task_model_id = context_compaction_model if context_compaction_model in models else model_id
    if task_model_id not in models:
        raise _CompactionFailure('summary_model_unavailable', 'No available model for context compaction.')

    summary_prompt_template = summary_prompt_template.strip() or DEFAULT_CONTEXT_COMPACTION_PROMPT
    # The text-only prompt formatter otherwise omits tool names and arguments.
    compacted_messages, recent_messages = list(compacted_messages), list(recent_messages)
    for messages in (compacted_messages, recent_messages):
        for idx, message in enumerate(messages):
            if message.get('tool_calls'):
                messages[idx] = {
                    **message,
                    'content': f'{get_content_from_message(message) or ""}\n[TOOL CALLS] {JSONCodec.dumps(message["tool_calls"])}',
                }
    all_messages = [*compacted_messages, *recent_messages]
    prompt = replace_prompt_variable(summary_prompt_template, get_last_user_message(all_messages) or '')
    prompt = replace_messages_variable(prompt, all_messages)
    prompt = replace_messages_variable(prompt, compacted_messages, 'COMPACTED_MESSAGES')
    prompt = replace_messages_variable(prompt, recent_messages, 'RECENT_MESSAGES')
    prompt = prompt_variables_template(prompt, {'{{PREVIOUS_SUMMARY}}': previous_summary or ''})
    prompt = await prompt_template(prompt, user)

    task_model_params = task_config.get('task.model.params') or {}
    if not isinstance(task_model_params, dict):
        task_model_params = {}
    task_model_params = {key: value for key, value in task_model_params.items() if value is not None and value != ''}
    task_model_params = task_model_params or {
        'max_tokens': models[task_model_id].get('info', {}).get('params', {}).get('max_tokens', 1000)
    }
    output_budget = max(1, min(1000, input_budget // 8))
    output_keys = [
        key for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if key in task_model_params
    ]
    for key in output_keys or ['max_tokens']:
        task_model_params[key] = min(_parse_positive_int(task_model_params.get(key)) or output_budget, output_budget)

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': 'context_compaction',
            '_context_compaction_input_budget': input_budget,
        },
    }

    payload = apply_params_to_form_data(payload, models[task_model_id], task_model_params)
    model_system = (models[task_model_id].get('info', {}).get('params') or {}).get('system') or ''
    if _estimate_messages_tokens(payload['messages']) + _estimate_tokens(model_system) > input_budget:
        raise _SummaryInputTooLarge('Summary prompt exceeds its input budget')
    # The dispatcher/provider router store per-call flags on request.state.
    # An internal summary must not reset the parent tool continuation's flags.
    missing = object()
    state_before = {
        key: getattr(request.state, key, missing) for key in ('bypass_filter', 'bypass_system_prompt', 'model_id')
    }
    try:
        response = await generate_chat_completion(request, form_data=payload, user=user)
    except _SummaryInputTooLarge:
        raise
    except Exception as exc:
        status = getattr(exc, 'status_code', None)
        raise _CompactionFailure(
            'summary_request_failed',
            'The compaction model request failed.',
            status if isinstance(status, int) else None,
        ) from exc
    finally:
        for key, value in state_before.items():
            if value is missing:
                if hasattr(request.state, key):
                    delattr(request.state, key)
            else:
                setattr(request.state, key, value)
    if (isinstance(response, JSONResponse) and response.status_code >= 400) or (
        isinstance(response, dict) and response.get('error')
    ):
        raise _CompactionFailure(
            'summary_request_failed',
            'The compaction model request failed.',
            response.status_code if isinstance(response, JSONResponse) else None,
        )
    summary = _response_text(response).strip()
    if not summary:
        raise _CompactionFailure('summary_empty', 'The compaction model returned no usable summary.')
    if _estimate_tokens(summary) > max(1, input_budget // 4):
        raise _CompactionFailure('summary_too_large', 'The compaction model returned an oversized summary.')
    return summary


SOURCE_BODY_RE = re.compile(r'(<source\b[^>]*>)(.*?)(</source>)', re.DOTALL)


async def enforce_context_budget(request, form_data: dict, user, models: dict, *, bypass_system_prompt=False):
    """Check the rendered request, including search results and tool schemas.

    Mutate only the outgoing payload. Persisted user text, tool results and
    citations stay intact; reductions here must not create DB checkpoints.
    """
    metadata = form_data.get('metadata') or {}
    summary_task = metadata.get('task') == 'context_compaction'
    if (metadata.get('task') and not summary_task) or (not metadata.get('chat_id') and not summary_task):
        return
    config = await _load_config()
    if not config['enable']:
        return

    budget = _resolve_token_threshold(config['token_threshold'], config['token_cap'], metadata)
    model_id = form_data['model']
    model = models.get(model_id) or {}
    model_params = model.get('info', {}).get('params') or {}
    model_system = '' if bypass_system_prompt else model_params.get('system') or ''
    output_reserve = max(
        (_parse_positive_int(form_data.get(key)) or _parse_positive_int(model_params.get(key)) or 0)
        for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens')
    )
    extra_tokens = (
        output_reserve
        + _estimate_tokens(model_system)
        + sum(_estimate_tokens(form_data.get(key)) for key in ('tools', 'functions', 'tool_choice', 'response_format'))
    )

    def size(messages):
        # Freshly count the actual payload: old usage predates search/RAG,
        # model instructions, schema changes, and possibly earlier reductions.
        return extra_tokens + _estimate_messages_tokens(messages)

    original = form_data.get('messages') or []
    if summary_task:
        # Validate task requests too, including model-preset variable expansion
        # in the provider router, but never recursively summarize a summary.
        summary_budget = min(_parse_positive_int(metadata.get('_context_compaction_input_budget')) or 16000, 16000)
        if size(original) - output_reserve > summary_budget:
            raise _SummaryInputTooLarge('Rendered summary request exceeds its input budget')
        return
    if size(original) <= budget:
        return

    event_emitter = None
    if metadata.get('message_id'):
        from open_webui.socket.main import get_event_emitter

        event_emitter = await get_event_emitter(metadata)

    async def progress(description, *, done=False, error=False):
        if event_emitter:
            await _emit_compaction_event(
                event_emitter,
                {
                    'type': 'context_compaction',
                    'data': {
                        'action': 'context_compaction',
                        'description': description,
                        'done': done,
                        'error': error,
                    },
                },
            )

    estimated_before = size(original)
    final_size = None
    phase = 'history'
    try:
        await progress('Compacting context')
        messages = copy.deepcopy(original)
        systems = [m for m in messages if m.get('role') in {'system', 'developer'}]
        history = [m for m in messages if m.get('role') not in {'system', 'developer'}]
        available = budget - size(systems)
        summary, compacted = None, False
        if available > 32:
            history, summary, compacted = await compact_messages_for_request(
                request, user, history, metadata, model_id, models, token_threshold=available, persist=False
            )
        if compacted and summary:
            systems.append({'role': 'system', 'content': '[CONVERSATION SUMMARY]\n' + summary})
        messages = [*systems, *history]

        # Reduce only tool result text and server-injected source bodies. Never
        # shorten the actual user question, tool arguments, IDs, or images.
        question = metadata.get('base_user_prompt') or get_last_user_message(history) or ''
        cache = metadata.setdefault('_context_result_summaries', {})

        async def summarize(text):
            key = hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()
            if key not in cache:
                cache[key] = await _generate_summary(
                    request,
                    user,
                    model_id,
                    models,
                    [{'role': 'tool', 'content': text}],
                    [{'role': 'user', 'content': question}],
                    None,
                    config['prompt_template'],
                )
            result = (
                '[Summarized to fit context; original tool results and source references are unchanged]\n' + cache[key]
            )
            if _estimate_tokens(result) >= _estimate_tokens(text):
                raise _CompactionFailure('summary_not_smaller', 'A tool/source summary did not reduce the context.')
            return result

        phase = 'tool_results'
        candidates = []
        for message in messages:
            content = message.get('content')
            if message.get('role') == 'tool':
                if isinstance(content, str):
                    candidates.append((message, 'content', content))
                elif isinstance(content, list):
                    candidates.extend(
                        (part, 'text', part['text'])
                        for part in content
                        if isinstance(part, dict) and isinstance(part.get('text'), str)
                    )
            elif metadata.get('sources') and isinstance(content, str):
                # Protect the literal question if it itself contains <source>
                # examples. Only the injected portion preceding it is eligible.
                protected_at = content.rfind(question) if question else -1
                if message.get('role') == 'user' and protected_at < 0:
                    continue
                source_text = content[:protected_at] if protected_at >= 0 else content
                for match in SOURCE_BODY_RE.finditer(source_text):
                    candidates.append((message, match, match[2]))

        # Apply source replacements in one pass after collection; offsets stay
        # tied to the original content even when several bodies are summarized.
        replacements = {}
        for target, field, text in sorted(candidates, key=lambda item: _estimate_tokens(item[2]), reverse=True):
            if size(messages) <= budget:
                break
            if _estimate_tokens(text) <= 256:
                continue
            result = await summarize(text)
            if isinstance(field, str):
                target[field] = result
            else:
                identity = id(target)
                entry = replacements.setdefault(identity, (target, target['content'], {}))
                entry[2][field.span(2)] = result
                content = entry[1]
                for (start, end), replacement in sorted(entry[2].items(), reverse=True):
                    content = content[:start] + replacement + content[end:]
                target['content'] = content

        # Source bodies may initially fill the system prompt. Once those have
        # been reduced, history can have a usable budget for the first time.
        if size(messages) > budget and available <= 32:
            phase = 'history'
            systems = [m for m in messages if m.get('role') in {'system', 'developer'}]
            history = [m for m in messages if m.get('role') not in {'system', 'developer'}]
            available = budget - size(systems)
            if available > 32:
                history, summary, compacted = await compact_messages_for_request(
                    request, user, history, metadata, model_id, models, token_threshold=available, persist=False
                )
                if compacted and summary:
                    systems.append({'role': 'system', 'content': '[CONVERSATION SUMMARY]\n' + summary})
                messages = [*systems, *history]

        phase = 'final_budget'
        final_size = size(messages)
        if final_size > budget:
            raise _CompactionFailure(
                'final_budget_exceeded',
                'The remaining protected content and fixed request overhead exceed the budget.',
            )
        # Commit only after every reduction and the final check succeeds.
        form_data['messages'] = messages
        await progress('Context compacted', done=True)
        log.info(
            'Context budget enforced: chat=%s estimated_before=%d estimated_after=%d budget=%d',
            metadata.get('chat_id'),
            size(original),
            final_size,
            budget,
        )
    except Exception as exc:
        await progress('Context compaction failed', done=True, error=True)
        code = exc.code if isinstance(exc, _CompactionFailure) else 'compaction_internal_error'
        reason = exc.reason if isinstance(exc, _CompactionFailure) else 'Context compaction failed unexpectedly.'
        provider_status = exc.provider_status if isinstance(exc, _CompactionFailure) else None
        log.warning(
            'Context compaction failed: chat=%s code=%s phase=%s estimated_before=%d '
            'estimated_after=%s budget=%d provider_status=%s exception_type=%s',
            metadata.get('chat_id'),
            code,
            phase,
            estimated_before,
            final_size,
            budget,
            provider_status,
            type(exc).__name__,
        )
        diagnostic = (
            f'Reason [{code}] at {phase}: {reason} '
            f'Estimated before={estimated_before}, after={final_size}, budget={budget}'
            + (f', summary HTTP status={provider_status}' if provider_status is not None else '')
            + '. '
        )
        raise HTTPException(
            status_code=400,
            detail='Context could not be compacted within the configured token budget. '
            + diagnostic
            + 'Reduce attached/search content or the output limit, select a larger-context compaction model, '
            'or start a new chat. '
            'The oversized request was not sent to the model.',
        ) from exc


def _response_text(response: Any) -> str:
    if isinstance(response, list) and len(response) == 1:
        response = response[0]

    if isinstance(response, JSONResponse):
        try:
            response = JSONCodec.loads(response.body.decode('utf-8', 'replace'))
        except Exception:
            return ''

    if not isinstance(response, dict):
        return ''

    choices = response.get('choices') or []
    if choices:
        message = choices[0].get('message') or {}
        return message.get('content') or message.get('reasoning_content') or ''

    parts = []
    for item in response.get('output') or []:
        for content in item.get('content') or []:
            if isinstance(content, dict):
                parts.append(content.get('text') or content.get('content') or '')
    return '\n'.join(part for part in parts if part)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += 4
        content = message.get('content')
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    total += _estimate_tokens(item)
                elif item.get('type') in {'image', 'image_url'}:
                    total += 1000
                else:
                    total += _estimate_tokens(item.get('text') or item.get('content') or item)
        else:
            total += _estimate_tokens(content)

        total += _estimate_tokens(message.get('output'))
        total += _estimate_tokens(message.get('tool_calls'))
        files = message.get('files')
        if files:
            # Inline data is not part of the file tags sent to the model.
            total += _estimate_tokens(BASE64_DATA_URI_RE.sub('', JSONCodec.dumps(files, ensure_ascii=False)))
    return total


def _estimate_tokens(value: Any) -> int:
    if value is None:
        return 0

    if not isinstance(value, str):
        try:
            value = JSONCodec.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)

    if not value:
        return 0

    return estimate_text_tokens(value)
