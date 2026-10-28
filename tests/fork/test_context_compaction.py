"""Focused backend tests: load real compaction code with DB/network boundaries isolated.

Run: python -m unittest discover -s tests/fork -p 'test_context_*.py' -v
No frontend compilation or application/database startup is needed.
"""

import ast
import collections
import copy
import importlib.util
import json
import logging
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
UTILS = ROOT / 'backend/open_webui/utils'


def module(name, **attrs):
    result = ModuleType(name)
    result.__dict__.update(attrs)
    return result


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class JSONResponse:
    def __init__(self, content, status_code=200):
        self.body, self.status_code = json.dumps(content).encode(), status_code


def text(message):
    value = message.get('content') or ''
    if isinstance(value, list):
        return '\n'.join(p.get('text', '') for p in value if isinstance(p, dict))
    return value


def actual_functions(path, names, namespace=None):
    namespace = {} if namespace is None else namespace
    tree = ast.parse(path.read_text())
    nodes = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert len(nodes) == len(names)
    program = ast.Module(
        body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(program), str(path), 'exec'), namespace)
    return namespace


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.values = {
            'chat.context_compaction.enable': True,
            'chat.context_compaction.token_threshold': 80000,
            'chat.context_compaction.token_cap': 80000,
            'chat.context_compaction.retention_percentage': 40,
        }
        config = SimpleNamespace(get_many=AsyncMock(side_effect=lambda *keys: {k: self.values.get(k) for k in keys}))
        self.chats = SimpleNamespace(upsert_message_to_chat_by_id_and_message_id=AsyncMock())

        async def identity(value, *args):
            return value

        def replace(template, messages, variable_name='MESSAGES'):
            return template.replace('{{' + variable_name + '}}', '\n'.join(f'{m["role"]}: {text(m)}' for m in messages))

        self.summary_requests = []

        async def provider(request, form_data, user, **kwargs):
            self.summary_requests.append(copy.deepcopy(form_data))
            self.assertEqual(form_data['metadata']['task'], 'context_compaction')
            self.assertLessEqual(self.cc._estimate_messages_tokens(form_data['messages']), 16000)
            return {'choices': [{'message': {'content': 'A concise summary. Source: https://example.com/article [1]'}}]}

        self.provider = AsyncMock(side_effect=provider)
        self.events = AsyncMock()
        packages = {
            'open_webui': module('open_webui', __path__=[str(ROOT / 'backend/open_webui')]),
            'open_webui.utils': module('open_webui.utils', __path__=[str(UTILS)]),
            'fastapi': module('fastapi', HTTPException=HTTPException),
            'fastapi.responses': module('fastapi.responses', JSONResponse=JSONResponse),
            'open_webui.models.chats': module('chats', Chats=self.chats),
            'open_webui.models.config': module('config', Config=config),
            'open_webui.utils.chat_id': module('chat_id', is_saved_chat_id=lambda value: value == 'saved'),
            'open_webui.utils.json_codec': module('json_codec', JSONCodec=json),
            'open_webui.utils.misc': module(
                'misc',
                get_content_from_message=text,
                get_last_user_message=lambda ms: next((text(m) for m in reversed(ms) if m.get('role') == 'user'), ''),
                get_message_list=lambda *args: [],
            ),
            'open_webui.utils.payload': module(
                'payload', apply_params_to_form_data=lambda payload, model, params: {**payload, **params}
            ),
            'open_webui.utils.task': module(
                'task',
                prompt_template=identity,
                prompt_variables_template=lambda p, values: p.replace(
                    '{{PREVIOUS_SUMMARY}}', values.get('{{PREVIOUS_SUMMARY}}', '')
                ),
                replace_messages_variable=replace,
                replace_prompt_variable=lambda p, question: p.replace('{{PROMPT}}', question),
            ),
            'open_webui.utils.chat': module('chat', generate_chat_completion=self.provider),
            'open_webui.socket': module('socket', __path__=[]),
            'open_webui.socket.main': module('socket.main', get_event_emitter=AsyncMock(return_value=self.events)),
        }
        self.modules_patch = patch.dict(sys.modules, packages)
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        for name in ('context_tokens', 'context_compaction'):
            full = 'open_webui.utils.' + name
            spec = importlib.util.spec_from_file_location(full, UTILS / (name + '.py'))
            loaded = importlib.util.module_from_spec(spec)
            sys.modules[full] = loaded
            spec.loader.exec_module(loaded)
            setattr(self, 'tokens' if name == 'context_tokens' else 'cc', loaded)
        # Deterministic conservative fallback, independent of local/CI packages.
        self.token_patch = patch.object(self.tokens, '_encoding', return_value=None)
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.request = SimpleNamespace(state=SimpleNamespace())
        self.models = {'test': {'id': 'test', 'owned_by': 'openai', 'info': {'params': {}}}}
        self.user = SimpleNamespace(role='admin')

    def payload(self, messages, **extra):
        return {
            'model': 'test',
            'messages': messages,
            'metadata': {
                'chat_id': 'saved',
                'message_id': 'assistant-1',
                'user_message_id': 'user-1',
                'base_user_prompt': 'Find the exact value and cite its source.',
            },
            **extra,
        }

    async def guard(self, payload):
        await self.cc.enforce_context_budget(self.request, payload, self.user, self.models)

    def tool_messages(self, body):
        return [
            {'role': 'user', 'content': 'Find the exact value and cite its source.'},
            {
                'role': 'assistant',
                'content': '',
                'tool_calls': [
                    {
                        'id': 'call-1',
                        'type': 'function',
                        'function': {'name': 'fetch_url', 'arguments': '{"url":"https://example.com/article"}'},
                    }
                ],
            },
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': body},
        ]

    async def test_first_tool_batch_114025_tokens_is_summarized_before_next_request(self):
        original = self.tool_messages('x' * 114025)
        original_copy = copy.deepcopy(original)
        payload = self.payload(original)
        await self.guard(payload)
        self.assertLessEqual(self.cc._estimate_messages_tokens(payload['messages']), 80000)
        self.assertEqual(payload['messages'][0], original[0])
        self.assertEqual(payload['messages'][1], original[1])
        self.assertEqual(payload['messages'][2]['tool_call_id'], 'call-1')
        self.assertIn('Source: https://example.com/article [1]', payload['messages'][2]['content'])
        self.assertGreater(len(self.summary_requests), 1)
        self.assertEqual(original, original_copy)
        self.chats.upsert_message_to_chat_by_id_and_message_id.assert_not_awaited()

    async def test_legacy_rag_source_is_reduced_without_changing_query_or_citations(self):
        question = 'Keep my literal <source id="example">example</source> unchanged.'
        content = (
            '<context><source id="1" name="https://example.com/article">'
            + 'z' * 110000
            + '</source></context>\n'
            + question
        )
        payload = self.payload([{'role': 'user', 'content': content}])
        payload['metadata'].update(sources=[{'document': ['raw']}], base_user_prompt=question)
        await self.guard(payload)
        result = payload['messages'][0]['content']
        self.assertTrue(result.endswith(question))
        self.assertIn('<source id="1" name="https://example.com/article">', result)
        self.assertLessEqual(self.cc._estimate_messages_tokens(payload['messages']), 80000)

    async def test_system_rag_can_shrink_even_when_it_alone_exceeds_budget(self):
        payload = self.payload(
            [
                {'role': 'system', 'content': 'Keep instructions. <source id="1">' + 'z' * 100000 + '</source>'},
                {'role': 'user', 'content': 'Find the exact value and cite its source.'},
            ]
        )
        payload['metadata']['sources'] = [{'document': ['raw']}]
        await self.guard(payload)
        self.assertTrue(payload['messages'][0]['content'].startswith('Keep instructions. <source id="1">'))
        self.assertEqual(payload['messages'][1]['content'], payload['metadata']['base_user_prompt'])

    async def test_multiple_source_replacements_preserve_offsets_and_reference_ids(self):
        content = '<source id="1">' + 'a' * 90000 + '</source><source id="2">' + 'b' * 90000 + '</source>'
        payload = self.payload([{'role': 'system', 'content': content}, {'role': 'user', 'content': 'question'}])
        payload['metadata']['sources'] = [1, 2]
        await self.guard(payload)
        self.assertEqual(payload['messages'][0]['content'].count('Summarized to fit context'), 2)
        self.assertIn('<source id="1">', payload['messages'][0]['content'])
        self.assertIn('<source id="2">', payload['messages'][0]['content'])

    async def test_history_before_first_tool_batch_can_compact_and_keeps_current_question(self):
        history = [
            {'role': 'user', 'content': 'old'},
            {'role': 'assistant', 'content': 'h' * 90000},
            *self.tool_messages('new result'),
        ]
        result, summary, compacted = await self.cc.compact_messages_for_request(
            self.request, self.user, history, {}, 'test', self.models, persist=False
        )
        self.assertTrue(compacted)
        self.assertTrue(summary)
        self.assertEqual(result[0], history[2])
        self.assertEqual(result[1:], history[3:])
        self.assertNotIn(history[1], result)

    def image_turn(self):
        return {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Here are the images from the tool results above. Please analyze them.'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AUDIT_IMAGE_BYTES'}},
            ],
        }

    async def test_tool_image_turn_keeps_actual_question_and_tool_pair(self):
        images = self.image_turn()
        original = self.tool_messages('x' * 110000) + [images]
        saved = copy.deepcopy(original)
        payload = self.payload(original)
        await self.guard(payload)
        self.assertIn(saved[0], payload['messages'])
        self.assertIn(images, payload['messages'])
        self.assertEqual(next(m for m in payload['messages'] if m['role'] == 'tool')['tool_call_id'], 'call-1')
        self.assertEqual(original, saved)

    async def test_tool_cut_does_not_save_a_moved_question_as_a_whole_turn_checkpoint(self):
        history = self.tool_messages('x' * 90000)
        history[0]['id'] = 'current-user'
        history.extend(
            [
                {
                    'role': 'assistant',
                    'tool_calls': [{'id': 'call-2', 'function': {'name': 'fetch_url', 'arguments': '{}'}}],
                },
                {'role': 'tool', 'tool_call_id': 'call-2', 'content': 'Small result'},
                self.image_turn(),
            ]
        )
        result, _, compacted = await self.cc.compact_messages_for_request(
            self.request, self.user, history, {'chat_id': 'saved', 'message_id': 'answer'}, 'test', self.models
        )
        self.assertTrue(compacted)
        self.assertEqual(result[0], history[0])
        self.assertIn(history[-1], result)
        self.chats.upsert_message_to_chat_by_id_and_message_id.assert_not_awaited()
        self.assertIsNone(self.events.await_args.args[0]['data']['checkpoint_message_id'])

    def stored_tool_message(self, body='Invoice total 12345; source https://example.com/invoice'):
        return {
            'role': 'assistant',
            'content': 'Document found.',
            'output': [
                {
                    'type': 'function_call',
                    'call_id': 'read-1',
                    'name': 'fetch_url',
                    'arguments': '{"url":"https://example.com"}',
                    'status': 'completed',
                },
                {
                    'type': 'function_call_output',
                    'call_id': 'read-1',
                    'output': [
                        {'type': 'input_text', 'text': body},
                        {'type': 'input_image', 'image_url': 'data:image/png;base64,AUDIT_IMAGE_BYTES'},
                    ],
                },
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Document found.'}]},
            ],
        }

    async def test_saved_tool_output_is_in_summary_with_actual_text_formatter(self):
        helpers = actual_functions(
            UTILS / 'misc.py', {'get_output_text', 'get_content_from_message', 'get_messages_content'}
        )
        self.cc.get_content_from_message = helpers['get_content_from_message']
        self.cc.replace_messages_variable = lambda prompt, messages, name='MESSAGES': prompt.replace(
            '{{' + name + '}}', helpers['get_messages_content'](messages)
        )
        message = self.stored_tool_message()
        original = copy.deepcopy(message)
        await self.cc._generate_summary(self.request, self.user, 'test', self.models, [message], [], None, '')
        prompt = self.summary_requests[0]['messages'][0]['content']
        for value in ('fetch_url', 'read-1', 'Invoice total 12345', 'https://example.com/invoice'):
            self.assertIn(value, prompt)
        self.assertNotIn('AUDIT_IMAGE_BYTES', prompt)
        self.assertEqual(prompt.count('Document found.'), 1)
        self.assertEqual(message, original)

    async def test_large_saved_tool_output_is_chunked_without_losing_final_facts(self):
        message = self.stored_tool_message('Begin source [1]\n' + 'x' * 40000 + '\nEND-FACT: total 12345')
        await self.cc._generate_summary(self.request, self.user, 'test', self.models, [message], [], None, '')
        self.assertGreater(len(self.summary_requests), 1)
        combined = '\n'.join(p['messages'][0]['content'] for p in self.summary_requests)
        self.assertIn('END-FACT: total 12345', combined)
        self.assertIn('read-1', combined)
        self.assertTrue(all(self.cc._estimate_messages_tokens(p['messages']) <= 16000 for p in self.summary_requests))

    async def test_all_text_parts_of_old_messages_reach_summary(self):
        message = {
            'role': 'user',
            'content': [{'type': 'text', 'text': 'First fact'}, {'type': 'text', 'text': 'Second fact'}],
        }
        await self.cc._generate_summary(self.request, self.user, 'test', self.models, [message], [], None, '')
        self.assertIn('Second fact', self.summary_requests[0]['messages'][0]['content'])

    async def test_incomplete_summaries_never_save_a_checkpoint(self):
        for reason in ('length', 'content_filter', 'tool_calls'):
            with self.subTest(reason=reason):
                self.provider.side_effect = None
                self.provider.return_value = JSONResponse(
                    {'choices': [{'message': {'content': 'Partial facts'}, 'finish_reason': reason}]}
                )
                with self.assertRaises(self.cc._CompactionFailure) as error:
                    await self.cc.compact_messages_for_request(
                        self.request,
                        self.user,
                        [
                            {'role': 'user', 'content': 'Old'},
                            {'role': 'assistant', 'content': 'x' * 90000},
                            {'id': 'new-user', 'role': 'user', 'content': 'New'},
                        ],
                        {'chat_id': 'saved', 'message_id': 'answer'},
                        'test',
                        self.models,
                    )
                self.assertEqual(error.exception.code, 'summary_incomplete')
                self.assertTrue(self.events.await_args.args[0]['data']['error'])
        self.chats.upsert_message_to_chat_by_id_and_message_id.assert_not_awaited()

    async def test_reasoning_only_response_is_not_a_completed_summary(self):
        self.provider.side_effect = None
        self.provider.return_value = {
            'choices': [
                {'message': {'content': '', 'reasoning_content': 'Unfinished reasoning'}, 'finish_reason': 'stop'}
            ]
        }
        with self.assertRaises(self.cc._CompactionFailure) as error:
            await self.cc._generate_summary(
                self.request, self.user, 'test', self.models, [{'role': 'user', 'content': 'Old'}], [], None, ''
            )
        self.assertEqual(error.exception.code, 'summary_empty')

    async def test_responses_conversion_preserves_truncation_for_summary_validation(self):
        helper = actual_functions(
            ROOT / 'backend/open_webui/routers/openai.py', {'convert_responses_result'}, {'JSONCodec': json}
        )
        for status, details in [
            ('incomplete', {'reason': 'max_output_tokens'}),
            ('incomplete', {'reason': 'content_filter'}),
            ('failed', {}),
        ]:
            raw = {
                'status': status,
                'incomplete_details': details,
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Partial facts'}]}],
            }
            converted = helper['convert_responses_result'](raw)
            self.assertNotEqual(converted['choices'][0]['finish_reason'], 'stop')
            self.provider.side_effect = None
            self.provider.return_value = converted
            with self.assertRaises(self.cc._CompactionFailure) as error:
                await self.cc._generate_summary(
                    self.request, self.user, 'test', self.models, [{'role': 'user', 'content': 'Old'}], [], None, ''
                )
            self.assertEqual(error.exception.code, 'summary_incomplete')
        complete = helper['convert_responses_result'](
            {
                'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Complete summary'}]}],
            }
        )
        self.provider.return_value = complete
        self.assertEqual(
            await self.cc._generate_summary(
                self.request, self.user, 'test', self.models, [{'role': 'user', 'content': 'Old'}], [], None, ''
            ),
            'Complete summary',
        )

    def actual_payload_helpers(self):
        helpers = actual_functions(UTILS / 'misc.py', {'deep_update'}, {'collections': collections})
        helpers.update(JSONCodec=json, log=self.cc.log, Callable=Callable)
        actual_functions(
            UTILS / 'payload.py',
            {
                'apply_params_to_form_data',
                'remove_open_webui_params',
                'apply_model_params_to_body',
                'apply_model_params_to_body_openai',
            },
            helpers,
        )
        return helpers

    async def test_summary_custom_and_model_params_cannot_bypass_output_cap(self):
        self.cc.apply_params_to_form_data = self.actual_payload_helpers()['apply_params_to_form_data']
        params = {'custom_params': {'max_tokens': '64000', 'max_completion_tokens': '128000'}}
        self.values['task.model.params'] = params
        self.models['test']['info']['params'] = {'max_output_tokens': 128000}
        await self.cc._generate_summary(
            self.request, self.user, 'test', self.models, [{'role': 'user', 'content': 'Old'}], [], None, ''
        )
        self.assertEqual(self.summary_requests[0]['max_tokens'], 1000)
        self.assertEqual(self.summary_requests[0]['max_completion_tokens'], 1000)
        self.assertEqual(self.summary_requests[0]['max_output_tokens'], 1000)
        self.assertEqual(params, {'custom_params': {'max_tokens': '64000', 'max_completion_tokens': '128000'}})

    async def test_summary_guard_clamps_rendered_limits_after_model_presets(self):
        payload = self.payload(
            [{'role': 'user', 'content': 'Summarize'}], max_tokens=64000, max_completion_tokens=128000
        )
        payload['metadata'].update(task='context_compaction', _context_compaction_input_budget=16000)
        await self.guard(payload)
        self.assertEqual(payload['max_tokens'], 1000)
        self.assertEqual(payload['max_completion_tokens'], 1000)
        self.provider.assert_not_awaited()

    async def test_ollama_summary_preempts_unlimited_model_output_defaults(self):
        self.cc.apply_params_to_form_data = self.actual_payload_helpers()['apply_params_to_form_data']
        self.models['test']['owned_by'] = 'ollama'
        self.models['test']['info']['params'] = {'num_predict': -1}
        self.values['task.model.params'] = {'max_completion_tokens': 64000}
        await self.cc._generate_summary(
            self.request, self.user, 'test', self.models, [{'role': 'user', 'content': 'Old'}], [], None, ''
        )
        self.assertEqual(self.summary_requests[0]['options']['num_predict'], 1000)

    async def test_failed_checkpoint_write_emits_error_and_preserves_history(self):
        history = [
            {'role': 'user', 'content': 'Old'},
            {'role': 'assistant', 'content': 'x' * 90000},
            {'id': 'new-user', 'role': 'user', 'content': 'New'},
        ]
        original = copy.deepcopy(history)
        for failure in (None, RuntimeError('synthetic DB failure')):
            self.chats.upsert_message_to_chat_by_id_and_message_id.side_effect = failure
            self.chats.upsert_message_to_chat_by_id_and_message_id.return_value = None
            with self.assertRaises(self.cc._CompactionFailure) as error:
                await self.cc.compact_messages_for_request(
                    self.request, self.user, history, {'chat_id': 'saved', 'message_id': 'answer'}, 'test', self.models
                )
            self.assertEqual(error.exception.code, 'checkpoint_save_failed')
            self.assertTrue(self.events.await_args.args[0]['data']['error'])
            self.assertNotIn('checkpoint_message_id', self.events.await_args.args[0]['data'])
        self.assertEqual(history, original)

    async def test_manual_compaction_does_not_report_success_when_save_fails(self):
        self.chats.get_messages_map_by_chat_id = AsyncMock(return_value={'branch': True})
        self.chats.upsert_message_to_chat_by_id_and_message_id.return_value = None
        chat = SimpleNamespace(id='saved', current_message_id='last', chat={})
        with patch.object(
            self.cc,
            'get_message_list',
            return_value=[{'role': 'user', 'content': 'Old'}, {'role': 'assistant', 'content': 'Answer'}],
        ):
            with self.assertRaises(self.cc._CompactionFailure) as error:
                await self.cc.compact_chat_branch(self.request, self.user, chat, 'test', self.models)
        self.assertEqual(error.exception.code, 'checkpoint_save_failed')

    async def test_missing_boundary_id_does_not_write_to_an_unrelated_metadata_id(self):
        await self.cc.compact_messages_for_request(
            self.request,
            self.user,
            [
                {'role': 'user', 'content': 'Old'},
                {'role': 'assistant', 'content': 'x' * 90000},
                {'role': 'user', 'content': 'New'},
            ],
            {'chat_id': 'saved', 'message_id': 'answer', 'user_message_id': 'unrelated'},
            'test',
            self.models,
        )
        self.chats.upsert_message_to_chat_by_id_and_message_id.assert_not_awaited()
        self.assertIsNone(self.events.await_args.args[0]['data']['checkpoint_message_id'])

    async def test_summary_failure_blocks_provider_and_leaves_payload_intact(self):
        payload = self.payload(self.tool_messages('x' * 110000))
        original = copy.deepcopy(payload['messages'])
        self.provider.side_effect = RuntimeError('summary provider unavailable')
        with self.assertRaises(HTTPException) as raised:
            await self.guard(payload)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn('not sent', raised.exception.detail)
        self.assertEqual(payload['messages'], original)

    async def test_failure_reports_safe_stage_code_and_budget_without_provider_text(self):
        payload = self.payload(self.tool_messages('x' * 110000))
        self.provider.side_effect = HTTPException(429, 'sensitive-provider-detail')
        with self.assertLogs(self.cc.log, level='WARNING') as logs:
            with self.assertRaises(HTTPException) as raised:
                await self.guard(payload)
        detail = raised.exception.detail
        self.assertIn('[summary_request_failed]', detail)
        self.assertIn('at tool_results', detail)
        self.assertIn('budget=80000', detail)
        self.assertIn('summary HTTP status=429', detail)
        self.assertIn('not sent', detail)
        self.assertNotIn('sensitive-provider-detail', detail)
        self.assertNotIn('sensitive-provider-detail', '\n'.join(logs.output))
        self.assertIn('code=summary_request_failed', '\n'.join(logs.output))

    async def test_empty_summary_and_final_budget_have_distinct_errors(self):
        self.provider.side_effect = None
        self.provider.return_value = {'choices': [{'message': {'content': ''}}]}
        with self.assertRaises(HTTPException) as empty:
            await self.guard(self.payload(self.tool_messages('x' * 110000)))
        self.assertIn('[summary_empty]', empty.exception.detail)
        self.provider.reset_mock()
        with self.assertRaises(HTTPException) as protected:
            await self.guard(self.payload([{'role': 'user', 'content': 'x' * 90000}]))
        self.assertIn('[final_budget_exceeded]', protected.exception.detail)
        self.assertIn('after=90004', protected.exception.detail)
        self.provider.assert_not_awaited()

    async def test_progress_channel_failure_does_not_abort_or_mask_compaction(self):
        self.events.side_effect = RuntimeError('sensitive-transport-detail')
        payload = self.payload(self.tool_messages('x' * 110000))
        with self.assertLogs(self.cc.log, level='WARNING') as logs:
            await self.guard(payload)
        self.assertLessEqual(self.cc._estimate_messages_tokens(payload['messages']), 80000)
        self.assertTrue(self.provider.await_count)
        self.assertNotIn('sensitive-transport-detail', '\n'.join(logs.output))
        self.provider.side_effect = RuntimeError('sensitive-provider-detail')
        with self.assertLogs(self.cc.log, level='WARNING'):
            with self.assertRaises(HTTPException) as failed:
                await self.guard(self.payload(self.tool_messages('y' * 110000)))
        self.assertIn('[summary_request_failed]', failed.exception.detail)

    async def test_progress_failure_keeps_successful_history_checkpoint(self):
        self.events.side_effect = RuntimeError('transport failed')
        history = [
            {'id': 'old-user', 'role': 'user', 'content': 'Old question'},
            {'id': 'old-answer', 'role': 'assistant', 'content': 'x' * 90000},
            {'id': 'new-user', 'role': 'user', 'content': 'New question'},
        ]
        with self.assertLogs(self.cc.log, level='WARNING'):
            result, summary, compacted = await self.cc.compact_messages_for_request(
                self.request,
                self.user,
                history,
                {'chat_id': 'saved', 'message_id': 'new-answer'},
                'test',
                self.models,
            )
        self.assertTrue(compacted)
        self.assertEqual(result, [history[-1]])
        self.chats.upsert_message_to_chat_by_id_and_message_id.assert_awaited_once_with(
            'saved', 'new-user', {'contextSummary': summary}, touch=False
        )

    async def test_error_object_or_empty_summary_does_not_create_fake_checkpoint(self):
        for response in (
            {'error': {'message': 'too long'}},
            JSONResponse({'detail': 'too long'}, 400),
            {'choices': []},
        ):
            with self.subTest(response=response):
                self.provider.side_effect = None
                self.provider.return_value = response
                with self.assertRaises(ValueError):
                    await self.cc._generate_summary(
                        self.request, self.user, 'test', self.models, [{'role': 'user', 'content': 'old'}], [], None, ''
                    )
        self.chats.upsert_message_to_chat_by_id_and_message_id.assert_not_awaited()

    async def test_protected_user_input_is_never_silently_shortened(self):
        payload = self.payload([{'role': 'user', 'content': 'Important user input' * 10000}])
        original = copy.deepcopy(payload['messages'])
        with self.assertRaises(HTTPException):
            await self.guard(payload)
        self.assertEqual(payload['messages'], original)
        self.provider.assert_not_awaited()

    async def test_tools_and_output_reservation_are_included_in_final_budget(self):
        payload = self.payload(
            [{'role': 'user', 'content': 'x' * 70000}], tools=[{'description': 'schema' * 2000}], max_tokens=10000
        )
        with self.assertRaises(HTTPException):
            await self.guard(payload)
        self.provider.assert_not_awaited()

    async def test_system_prompt_added_by_model_is_reserved(self):
        self.models['test']['info']['params']['system'] = 's' * 85000
        payload = self.payload([{'role': 'user', 'content': 'question'}])
        with self.assertRaises(HTTPException):
            await self.guard(payload)
        await self.cc.enforce_context_budget(self.request, payload, self.user, self.models, bypass_system_prompt=True)
        self.provider.assert_not_awaited()

    async def test_cached_tool_summary_is_reused_in_same_request_loop(self):
        payload = self.payload(self.tool_messages('x' * 110000))
        original = copy.deepcopy(payload['messages'])
        await self.guard(payload)
        count = self.provider.await_count
        payload['messages'] = original
        await self.guard(payload)
        self.assertEqual(self.provider.await_count, count)

    async def test_multiple_multimodal_tool_results_keep_pairing_and_images(self):
        messages = self.tool_messages('')
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}
        messages[-1]['content'] = [{'type': 'text', 'text': 'x' * 110000}, image]
        payload = self.payload(messages)
        await self.guard(payload)
        self.assertEqual(payload['messages'][-1]['content'][1], image)
        self.assertEqual(payload['messages'][-1]['tool_call_id'], 'call-1')

    async def test_disabled_compaction_and_background_tasks_are_unchanged(self):
        for disabled, task in ((True, None), (False, 'title_generation')):
            self.values['chat.context_compaction.enable'] = not disabled
            payload = self.payload(self.tool_messages('x' * 110000))
            if task:
                payload['metadata']['task'] = task
            original = copy.deepcopy(payload)
            await self.guard(payload)
            self.assertEqual(payload, original)
        self.provider.assert_not_awaited()

    async def test_summary_task_is_budgeted_without_recursive_compaction(self):
        payload = self.payload([{'role': 'user', 'content': 'x' * 20000}])
        payload['metadata'].update(task='context_compaction', _context_compaction_input_budget=16000)
        with self.assertRaises(self.cc._SummaryInputTooLarge):
            await self.guard(payload)
        self.provider.assert_not_awaited()

    async def test_summary_restores_parent_provider_flags(self):
        self.request.state.bypass_filter = True
        self.request.state.bypass_system_prompt = True
        self.request.state.model_id = 'original'

        async def changed_flags(request, **kwargs):
            request.state.bypass_filter = False
            request.state.bypass_system_prompt = False
            request.state.model_id = 'summary'
            return {'choices': [{'message': {'content': 'summary'}}]}

        self.provider.side_effect = changed_flags
        await self.cc._generate_summary(
            self.request, self.user, 'test', self.models, [{'role': 'user', 'content': 'history'}], [], None, ''
        )
        self.assertTrue(self.request.state.bypass_filter)
        self.assertTrue(self.request.state.bypass_system_prompt)
        self.assertEqual(self.request.state.model_id, 'original')

    async def test_stale_usage_does_not_hide_freshly_injected_context(self):
        messages = [{'role': 'user', 'content': '中' * 40000, 'usage': {'prompt_tokens': 1}}]
        self.assertTrue(self.cc._exceeds_token_threshold(messages, '', None, 80000))

    async def test_compacted_checkpoint_does_not_repeat_summaries_on_followups(self):
        history = [
            {'id': 'old-u', 'role': 'user', 'content': 'Old question'},
            {'id': 'old-a', 'role': 'assistant', 'content': 'x' * 100000},
            {'id': 'kept-u', 'role': 'user', 'content': 'Recent question'},
            {
                'id': 'kept-a',
                'role': 'assistant',
                'content': 'Recent answer',
                'usage': {'prompt_tokens': 95000, 'completion_tokens': 500},
            },
            {'id': 'new-u', 'role': 'user', 'content': 'Current question'},
        ]
        metadata = {'chat_id': 'saved', 'user_message_id': 'new-u', 'message_id': 'new-a'}
        _, _, compacted = await self.cc.compact_messages_for_request(
            self.request, self.user, copy.deepcopy(history), metadata, 'test', self.models
        )
        self.assertTrue(compacted)
        _, checkpoint, data = self.chats.upsert_message_to_chat_by_id_and_message_id.await_args.args
        replay = copy.deepcopy(history)
        next(message for message in replay if message['id'] == checkpoint).update(data)
        calls = self.provider.await_count
        for turn in range(3):
            _, summary, compacted = await self.cc.compact_messages_for_request(
                self.request, self.user, copy.deepcopy(replay), metadata, 'test', self.models
            )
            self.assertFalse(compacted)
            self.assertTrue(summary)
            self.assertEqual(self.provider.await_count, calls)
            replay.extend(
                [
                    {'id': f'answer-{turn}', 'role': 'assistant', 'content': 'Short answer'},
                    {'id': f'question-{turn}', 'role': 'user', 'content': 'Short follow-up'},
                ]
            )
        self.chats.upsert_message_to_chat_by_id_and_message_id.assert_awaited_once()

    async def test_stale_large_usage_does_not_trigger_compaction_without_checkpoint(self):
        payload = [
            {'role': 'user', 'content': 'Question'},
            {
                'role': 'assistant',
                'content': 'Answer',
                'info': {'usage': {'prompt_tokens': 95000, 'completion_tokens': 1000}},
            },
            {'role': 'user', 'content': 'Follow-up'},
        ]
        _, _, compacted = await self.cc.compact_messages_for_request(
            self.request, self.user, payload, {}, 'test', self.models, persist=False
        )
        self.assertFalse(compacted)
        self.provider.assert_not_awaited()

    async def test_usage_endpoint_counts_active_checkpoint_instead_of_old_receipt(self):
        messages = [
            {'role': 'user', 'content': 'Old content' * 20000},
            {'role': 'user', 'content': 'Current question', 'contextSummary': 'Saved summary'},
            {'role': 'assistant', 'content': 'Short answer', 'usage': {'prompt_tokens': 95000}},
        ]
        self.chats.get_messages_map_by_chat_id = AsyncMock(return_value={'branch': messages})
        with patch.object(self.cc, 'get_message_list', return_value=messages):
            chat = SimpleNamespace(id='saved', current_message_id='leaf', chat={})
            usage = await self.cc.get_chat_context_usage(chat)
        expected = self.cc._estimate_tokens('Saved summary') + self.cc._estimate_messages_tokens(messages[1:])
        self.assertEqual(usage['tokens'], expected)
        self.assertLess(usage['tokens'], 1000)

    async def test_new_active_content_still_triggers_after_saved_checkpoint(self):
        messages = [
            {'role': 'user', 'content': 'Old history'},
            {'role': 'user', 'content': 'Previous question', 'contextSummary': 'Saved summary'},
            {'role': 'assistant', 'content': 'x' * 90000, 'usage': {'prompt_tokens': 1}},
            {'role': 'user', 'content': 'New question'},
        ]
        result, summary, compacted = await self.cc.compact_messages_for_request(
            self.request, self.user, messages, {}, 'test', self.models, persist=False
        )
        self.assertTrue(compacted)
        self.assertTrue(summary)
        self.assertEqual(result, [messages[-1]])

    async def test_history_after_a_completed_tool_batch_keeps_only_current_question(self):
        history = [*self.tool_messages('x' * 90000), {'role': 'user', 'content': 'The next question'}]
        result, summary, compacted = await self.cc.compact_messages_for_request(
            self.request, self.user, history, {}, 'test', self.models, persist=False
        )
        self.assertTrue(compacted)
        self.assertEqual(result, [history[-1]])
        self.assertTrue(summary)

    def test_boundary_does_not_split_pending_parallel_tool_results(self):
        messages = self.tool_messages('first')
        messages[1]['tool_calls'].append({'id': 'call-2', 'function': {'name': 'search'}})
        messages += [
            {'role': 'assistant', 'tool_calls': [{'id': 'call-3'}]},
            {'role': 'tool', 'tool_call_id': 'call-2', 'content': 'second'},
        ]
        self.assertEqual(self.cc._find_compaction_boundary(messages), 1)

    async def test_multipart_current_user_turn_remains_a_valid_history_boundary(self):
        current = {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Read this image'},
                {'type': 'image_url', 'image_url': {'url': 'https://example.com/img.png'}},
            ],
        }
        history = [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'x' * 90000}, current]
        result, _, compacted = await self.cc.compact_messages_for_request(
            self.request, self.user, history, {}, 'test', self.models, persist=False
        )
        self.assertTrue(compacted)
        self.assertEqual(result, [current])

    async def test_global_cap_and_personal_threshold_both_apply(self):
        self.values['chat.context_compaction.token_cap'] = 40000
        payload = self.payload(self.tool_messages('x' * 45000))
        payload['metadata']['params'] = {'compact_token_threshold': 60000}
        await self.guard(payload)
        self.assertLessEqual(self.cc._estimate_messages_tokens(payload['messages']), 40000)
        self.assertTrue(self.summary_requests)

    async def test_oversized_model_summary_is_not_forwarded(self):
        self.provider.side_effect = None
        self.provider.return_value = {'choices': [{'message': {'content': 'x' * 100000}}]}
        with self.assertRaises(HTTPException):
            await self.guard(self.payload(self.tool_messages('x' * 110000)))

    async def test_summary_work_is_bounded_when_input_needs_more_than_64_chunks(self):
        self.values.update({'chat.context_compaction.token_threshold': 2048, 'chat.context_compaction.token_cap': 2048})
        with self.assertRaises(HTTPException):
            await self.guard(self.payload(self.tool_messages('x' * 60000)))
        self.assertLessEqual(self.provider.await_count, 64)

    async def test_small_payload_does_not_call_summarizer(self):
        payload = self.payload(self.tool_messages('short result'))
        original = copy.deepcopy(payload)
        await self.guard(payload)
        self.assertEqual(payload, original)
        self.provider.assert_not_awaited()

    async def test_compaction_progress_finishes_on_success_and_error(self):
        await self.guard(self.payload(self.tool_messages('x' * 110000)))
        self.assertFalse(self.events.await_args_list[0].args[0]['data']['done'])
        self.assertTrue(self.events.await_args_list[-1].args[0]['data']['done'])
        self.assertFalse(self.events.await_args_list[-1].args[0]['data']['error'])
        self.events.reset_mock()
        with self.assertRaises(HTTPException):
            await self.guard(self.payload([{'role': 'user', 'content': 'x' * 110000}]))
        self.assertTrue(self.events.await_args_list[-1].args[0]['data']['error'])
        self.assertTrue(self.events.await_args_list[-1].args[0]['data']['done'])

    async def test_custom_summary_template_is_bounded_and_chunking_is_finite(self):
        self.values['chat.context_compaction.prompt_template'] = (
            '{{COMPACTED_MESSAGES}}\n{{COMPACTED_MESSAGES}}\n{{PREVIOUS_SUMMARY}}\n{{RECENT_MESSAGES}}'
        )
        payload = self.payload(self.tool_messages('x' * 110000))
        await self.guard(payload)
        self.assertTrue(self.summary_requests)
        self.assertLess(len(self.summary_requests), 64)

    async def test_unfit_custom_summary_template_fails_without_provider_request(self):
        self.values['chat.context_compaction.prompt_template'] = (
            'constant instructions' * 3000 + '{{COMPACTED_MESSAGES}}'
        )
        with self.assertRaises(HTTPException):
            await self.guard(self.payload(self.tool_messages('x' * 110000)))
        self.provider.assert_not_awaited()

    async def test_dispatcher_checks_payload_before_contacting_provider(self):
        # Execute the actual dispatcher with provider/access/DB boundaries
        # isolated, so this checks behavior rather than a source-string match.
        source = ast.parse((UTILS / 'chat.py').read_text())
        function = next(
            n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'generate_chat_completion'
        )
        program = ast.Module(
            body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function],
            type_ignores=[],
        )
        outgoing = AsyncMock(return_value={'choices': []})
        namespace = {
            'log': logging.getLogger('test'),
            'BYPASS_MODEL_ACCESS_CONTROL': False,
            'enforce_context_budget': self.cc.enforce_context_budget,
            'generate_openai_chat_completion': outgoing,
        }
        exec(compile(ast.fix_missing_locations(program), str(UTILS / 'chat.py'), 'exec'), namespace)
        self.request.app = SimpleNamespace(state=SimpleNamespace(MODELS=self.models))
        payload = self.payload([{'role': 'user', 'content': 'x' * 110000}])
        with self.assertRaises(HTTPException):
            await namespace['generate_chat_completion'](self.request, payload, self.user)
        outgoing.assert_not_awaited()
        payload = self.payload(self.tool_messages('x' * 110000))
        await namespace['generate_chat_completion'](self.request, payload, self.user)
        outgoing.assert_awaited_once()
        self.assertLessEqual(
            self.cc._estimate_messages_tokens(outgoing.call_args.kwargs['form_data']['messages']), 80000
        )

    async def test_provider_router_rechecks_expanded_model_system_prompt(self):
        source = ast.parse((ROOT / 'backend/open_webui/routers/openai.py').read_text())
        function = next(
            n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'generate_chat_completion'
        )
        function.decorator_list = []
        program = ast.Module(
            body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function],
            type_ignores=[],
        )

        async def expanded_system(system, payload, metadata, user):
            return {
                **payload,
                'messages': [{'role': 'system', 'content': 'expanded variable' * 6000}, *payload['messages']],
            }

        headers = AsyncMock()
        info = SimpleNamespace(
            base_model_id=None, params=SimpleNamespace(model_dump=lambda: {'system': '{{large_variable}}'})
        )
        namespace = {
            'Depends': lambda value: None,
            'get_verified_user': None,
            'BYPASS_MODEL_ACCESS_CONTROL': False,
            'Config': SimpleNamespace(get=AsyncMock(return_value=True)),
            'Models': SimpleNamespace(get_model_by_id=AsyncMock(return_value=info)),
            'apply_model_params_to_body_openai': lambda params, payload: payload,
            'apply_system_prompt_to_body': expanded_system,
            'check_model_access': AsyncMock(),
            'get_openai_connection': AsyncMock(return_value=('https://example.com', 'test-key', {})),
            'get_headers_and_cookies': headers,
        }
        exec(compile(ast.fix_missing_locations(program), 'provider-router', 'exec'), namespace)
        self.request.app = SimpleNamespace(
            state=SimpleNamespace(MODELS=self.models, OPENAI_MODELS={'test': {'urlIdx': 0}})
        )
        with self.assertRaises(HTTPException):
            await namespace['generate_chat_completion'](
                self.request, self.payload([{'role': 'user', 'content': 'question'}]), self.user
            )
        headers.assert_not_awaited()

    async def test_provider_router_forwards_clamped_limits_and_keeps_resolved_model(self):
        path = ROOT / 'backend/open_webui/routers/openai.py'
        function = next(
            node
            for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'generate_chat_completion'
        )
        function.decorator_list = []
        # Stop at provider conversion: exercise the actual preset + guard path.
        end = next(
            index
            for index, node in enumerate(function.body)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'prefix_id' for target in node.targets)
        )
        function.body = [*function.body[:end], ast.Return(value=ast.Name(id='payload', ctx=ast.Load()))]
        program = ast.Module(
            body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function],
            type_ignores=[],
        )
        params = {'max_completion_tokens': 128000, 'custom_params': {'max_tokens': '64000'}}
        info = SimpleNamespace(
            base_model_id='provider-base', params=SimpleNamespace(model_dump=lambda: copy.deepcopy(params))
        )

        async def system_identity(system, payload, metadata, user):
            return payload

        namespace = {
            'Depends': lambda value: None,
            'get_verified_user': None,
            'BYPASS_MODEL_ACCESS_CONTROL': False,
            'Config': SimpleNamespace(get=AsyncMock(return_value=True)),
            'Models': SimpleNamespace(get_model_by_id=AsyncMock(return_value=info)),
            'apply_model_params_to_body_openai': self.actual_payload_helpers()['apply_model_params_to_body_openai'],
            'apply_system_prompt_to_body': system_identity,
            'check_model_access': AsyncMock(),
            'get_openai_connection': AsyncMock(return_value=('https://example.com', '', {})),
        }
        exec(compile(ast.fix_missing_locations(program), str(path), 'exec'), namespace)
        self.request.app = SimpleNamespace(
            state=SimpleNamespace(MODELS=self.models, OPENAI_MODELS={'provider-base': {'urlIdx': 0}})
        )
        payload = self.payload([{'role': 'user', 'content': 'Summarize'}])
        payload['metadata'].update(task='context_compaction', _context_compaction_input_budget=16000)
        result = await namespace['generate_chat_completion'](self.request, payload, self.user)
        self.assertEqual(result['model'], 'provider-base')
        self.assertEqual(result['max_tokens'], 1000)
        self.assertEqual(result['max_completion_tokens'], 1000)
        self.provider.assert_not_awaited()

    def test_unicode_chunks_are_lossless_and_each_fits_budget(self):
        value = ('中文🙂abc\n' * 1000) + '<|endoftext|>'
        chunks = list(self.tokens.split_text_to_budget(value, 257))
        self.assertEqual(''.join(chunks), value)
        self.assertTrue(all(self.tokens.estimate_text_tokens(c) <= 257 for c in chunks))

    def test_tokenizer_margin_and_special_token_text(self):
        encoder = SimpleNamespace(encode=lambda value, **kwargs: list(range(10)))
        with patch.object(self.tokens, '_encoding', return_value=encoder):
            self.assertEqual(self.tokens.estimate_text_tokens('<|endoftext|>'), 12)

    async def test_real_tokenizer_handles_chinese_code_and_special_token_text(self):
        try:
            import tiktoken
        except ImportError:
            self.skipTest('tiktoken is optional for local isolated tests; installed on the remote builder')
        encoder = tiktoken.get_encoding('cl100k_base')
        value = ('搜索结果：代码 def f(x): return x + 1🙂\n' * 500) + '<|endoftext|>'
        with patch.object(self.tokens, '_encoding', return_value=encoder):
            self.assertGreater(self.tokens.estimate_text_tokens(value), len(value) // 4)
            chunks = list(self.tokens.split_text_to_budget(value, 256))
            self.assertEqual(''.join(chunks), value)
            self.assertTrue(all(self.tokens.estimate_text_tokens(c) <= 256 for c in chunks))
            large = '\n'.join(f'{i:08x}: 搜索网页结果与代码 return value_{i} + {i * i}' for i in range(5000))
            self.assertGreater(len(encoder.encode(large)), 100000)
            payload = self.payload(self.tool_messages(large))
            await self.guard(payload)
            self.assertLessEqual(self.cc._estimate_messages_tokens(payload['messages']), 80000)
            self.assertGreater(len(self.summary_requests), 1)


if __name__ == '__main__':
    unittest.main()
