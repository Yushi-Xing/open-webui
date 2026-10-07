import { describe, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import { writable } from 'svelte/store';

vi.mock('$lib/stores', async () => {
	const { writable } = await import('svelte/store');
	return { config: writable({}), pyodideWorker: writable(null) };
});
vi.mock('svelte-sonner', () => ({ toast: { error: vi.fn() } }));
vi.mock('$lib/utils', () => ({
	copyToClipboard: vi.fn(),
	initMermaid: vi.fn(),
	renderMermaidDiagram: vi.fn(),
	renderVegaVisualization: vi.fn(),
	unescapeHtml: (value: string) => value
}));
vi.mock('$lib/apis/utils', () => ({ executeCode: vi.fn() }));
vi.mock('$lib/pyodide/createPyodideWorker', () => ({ createPyodideWorker: vi.fn() }));
vi.mock('$lib/components/common/CodeEditor.svelte', () => ({
	default: () => {
		throw new Error('A reply code block must not instantiate an editable CodeEditor');
	}
}));
vi.mock('$lib/components/common/Tooltip.svelte', async () => ({
	default: (await import('./fixtures/Tooltip.svelte')).default
}));
vi.mock('$lib/components/common/SVGPanZoom.svelte', () => ({ default: vi.fn() }));
vi.mock('$lib/components/chat/Messages/DiffBlock.svelte', () => ({ default: vi.fn() }));

import CodeBlock from '../../src/lib/components/chat/Messages/CodeBlock.svelte';

const renderCode = (lang: string, code: string) =>
	render(CodeBlock, {
		props: { lang, code, run: false },
		context: new Map([['i18n', writable({ t: (value: string) => value })]])
	}).body;

describe('read-only reply code blocks', () => {
	it('renders highlighted code and the copy button without an editable control', () => {
		const html = renderCode('python', 'print("hello")');
		expect(html).toContain('<pre');
		expect(html).toContain('hljs');
		expect(html).toContain('copy-code-button');
		expect(html).not.toMatch(/contenteditable|textarea|cm-editor/);
	});

	it('escapes HTML in unrecognized code instead of creating live elements', () => {
		const html = renderCode('unknown-language', '<script>alert("test")</script>');
		expect(html).toMatch(/&lt;script(?:>|&gt;)/);
		expect(html).not.toContain('<script>');
		expect(html).not.toMatch(/contenteditable|textarea|cm-editor/);
	});
});
