import { describe, expect, it } from 'vitest';
import { estimateActiveContextUsage } from '../../src/lib/utils/context-usage';

describe('active context usage', () => {
	it('counts the checkpoint branch rather than old provider usage', () => {
		const messages = [
			{ role: 'user', content: 'x'.repeat(100000) },
			{ role: 'user', content: 'Question', contextSummary: 'Saved facts' },
			{ role: 'assistant', content: 'Short answer', usage: { prompt_tokens: 95000 } }
		];
		const usage = estimateActiveContextUsage(messages, 'System', 90000);
		expect(usage.tokens).toBeLessThan(1000);
		expect(usage).toEqual(
			estimateActiveContextUsage(
				messages.map(({ usage, ...message }) => message),
				'System',
				90000
			)
		);
	});

	it('counts short follow-ups without restoring dropped history', () => {
		const messages = [
			{ role: 'user', content: 'x'.repeat(100000) },
			{ role: 'user', content: 'Question', contextSummary: 'Saved facts' }
		];
		const before = estimateActiveContextUsage(messages, '', 90000).tokens;
		const after = estimateActiveContextUsage(
			[
				...messages,
				{ role: 'assistant', content: 'Small reply' },
				{ role: 'user', content: 'Next' }
			],
			'',
			90000
		).tokens;
		expect(after).toBeGreaterThan(before);
		expect(after).toBeLessThan(1000);
	});

	it('replays tool-loop output checkpoints', () => {
		const usage = estimateActiveContextUsage(
			[
				{ role: 'user', content: 'Current question' },
				{
					role: 'assistant',
					output: [
						{ type: 'function_call', call_id: 'old', arguments: '{}' },
						{ type: 'function_call_output', call_id: 'old', output: 'x'.repeat(100000) },
						{ type: 'function_call', call_id: 'new', arguments: '{}' },
						{ type: 'function_call_output', call_id: 'new', output: 'Small result' },
						{ type: 'open_webui:context_compaction', call_id: 'new', summary: 'Saved tool facts' }
					]
				}
			],
			'',
			90000
		);
		expect(usage.tokens).toBeLessThan(1000);
	});

	it('keeps genuine large active content and supports a disabled threshold', () => {
		const usage = estimateActiveContextUsage(
			[{ role: 'user', content: 'x'.repeat(400000) }],
			'',
			90000
		);
		expect(usage.percent).toBeGreaterThan(100);
		expect(
			estimateActiveContextUsage([{ role: 'user', content: 'Hi' }], '', null).percent
		).toBeNull();
	});
});
