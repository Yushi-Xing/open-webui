// Historical provider usage includes context removed by later checkpoints.
// Estimate the currently active branch instead; this is a display estimate.
type Message = Record<string, any>;

const estimateTokens = (value: unknown): number => {
	if (value === null || value === undefined || value === '') return 0;
	if (typeof value !== 'string') {
		try {
			value = JSON.stringify(value);
		} catch {
			value = String(value);
		}
	}
	return Math.max(1, Math.floor(String(value).length / 4));
};

const estimateMessagesTokens = (messages: Message[]): number =>
	messages.reduce((total, message) => {
		let next = total + 4 + estimateTokens(message.content);
		next += estimateTokens(message.output) + estimateTokens(message.tool_calls);
		if (message.files?.length) {
			next += estimateTokens(
				JSON.stringify(message.files).replace(/data:[\w/+.;=%-]*;base64,[A-Za-z0-9+/=]*/g, '')
			);
		}
		return next;
	}, 0);

const activeBranch = (messages: Message[]): { messages: Message[]; summary: string } => {
	for (let index = messages.length - 1; index >= 0; index--) {
		const message = messages[index];
		const summary = message.contextSummary ?? message.context_summary;
		if (typeof summary === 'string' && summary.trim()) {
			return { messages: messages.slice(index), summary };
		}
		const marker = [...(message.output ?? [])]
			.reverse()
			.find((item) => item.type === 'open_webui:context_compaction');
		if (!marker) continue;
		for (let cut = index; cut >= 0; cut--) {
			const output = messages[cut].output ?? [];
			let start = output.findIndex(
				(item) => item.type === 'function_call' && item.call_id === marker.call_id
			);
			if (start < 0) continue;
			while (start > 0 && output[start - 1].type !== 'function_call_output') start--;
			return {
				messages: [
					...messages.slice(Math.max(0, cut - 1), cut),
					{
						...messages[cut],
						output: output
							.slice(start)
							.filter((item) => item.type !== 'open_webui:context_compaction')
					},
					...messages.slice(cut + 1)
				],
				summary: typeof marker.summary === 'string' ? marker.summary : ''
			};
		}
	}
	return { messages, summary: '' };
};

export const estimateActiveContextUsage = (
	messages: Message[],
	system: string,
	threshold: number | null
) => {
	const active = activeBranch(messages);
	const tokens =
		estimateTokens(system) +
		estimateTokens(active.summary) +
		estimateMessagesTokens(active.messages);
	return {
		tokens,
		estimated_tokens: tokens,
		threshold,
		percent:
			threshold !== null && threshold > 0
				? Math.max(0, Math.round((tokens / threshold) * 100))
				: null,
		source: 'estimated'
	};
};
