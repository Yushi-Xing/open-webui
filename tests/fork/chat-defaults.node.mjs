import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { appendCodeOutputRules, CODE_OUTPUT_RULES } from '../../src/lib/utils/chat-system-prompt.js';
import { DEFAULT_TEXT_SCALE, getDefaultTextScale } from '../../src/lib/utils/interface-defaults.js';
import { getRichTextLinkOptions } from '../../src/lib/utils/rich-text-link-options.js';

const promptOptions = { link: false, messageInput: true, richText: true, autoFormat: true };
const source = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8');

test('chat rich text supports HTML links without automatic linking or navigation', () => {
	assert.deepEqual(getRichTextLinkOptions(promptOptions), {
		autolink: false,
		linkOnPaste: false,
		openOnClick: false
	});
});

test('turning off automatic formatting still keeps the link schema', () => {
	assert.notEqual(getRichTextLinkOptions({ ...promptOptions, autoFormat: false }), false);
});

test('plain text prompt editors retain their existing schema', () => {
	assert.equal(getRichTextLinkOptions({ ...promptOptions, richText: false }), false);
});

test('notes retain explicit link support and automatic linking', () => {
	assert.deepEqual(
		getRichTextLinkOptions({ ...promptOptions, messageInput: false, link: true }),
		{ autolink: true, linkOnPaste: true, openOnClick: true }
	);
});

test('other rich text editors do not acquire links implicitly', () => {
	assert.equal(getRichTextLinkOptions({ ...promptOptions, messageInput: false }), false);
});

test('interface defaults to 1.2, including null and unset settings', () => {
	assert.equal(DEFAULT_TEXT_SCALE, 1.2);
	assert.equal(getDefaultTextScale(), 1.2);
	assert.equal(getDefaultTextScale({ textScale: null }), 1.2);
});

test('explicit personal and administrator scales take precedence', () => {
	assert.equal(getDefaultTextScale({ textScale: 1 }), 1);
	assert.equal(getDefaultTextScale({ textScale: 1.4 }), 1.4);
});

test('empty system prompts receive the English code output rules', () => {
	for (const prompt of ['', undefined, null]) {
		assert.equal(appendCodeOutputRules(prompt), CODE_OUTPUT_RULES);
	}
});

test('rules are appended after the complete existing system prompt', () => {
	const original = 'Answer in Chinese.\nPreserve {{CURRENT_DATE}} and user preferences.';
	assert.equal(appendCodeOutputRules(original), `${original}\n\n${CODE_OUTPUT_RULES}`);
});

test('repeated submissions do not duplicate the appended rules', () => {
	const prompt = appendCodeOutputRules('Existing instructions');
	assert.equal(appendCodeOutputRules(prompt), prompt);
});

test('the English rules require chat code blocks, a file, and terminal output', () => {
	assert.match(CODE_OUTPUT_RULES, /Markdown code blocks with the correct language identifier/);
	assert.match(CODE_OUTPUT_RULES, /create the file and also print its contents in the terminal/);
});

test('all prompt editor surfaces use the shared link configuration', () => {
	assert.match(source('src/lib/components/common/RichTextInput.svelte'), /PromptLink\.configure\(/);
	assert.match(source('src/lib/components/common/RichTextInput.svelte'), /getRichTextLinkOptions\(/);
	for (const path of [
		'src/lib/components/chat/MessageInput.svelte',
		'src/lib/components/channel/MessageInput.svelte',
		'src/lib/components/common/InputModal.svelte'
	]) {
		assert.match(source(path), /messageInput=\{true\}/);
	}
});

test('assistant rendering and execution details explicitly disable code editing', () => {
	assert.match(source('src/lib/components/chat/Messages/ResponseMessage.svelte'), /editCodeBlock=\{false\}/);
	assert.match(source('src/lib/components/chat/Messages/CodeExecutionModal.svelte'), /edit=\{false\}/);
	assert.match(source('src/lib/components/chat/Messages/CodeBlock.svelte'), /export let edit = false/);
	assert.match(source('src/lib/components/chat/Messages/CodeBlock.svelte'), /readOnly=\{!edit\}/);
});

test('chat requests include the supplemented system prompt', () => {
	assert.match(source('src/lib/components/chat/Chat.svelte'), /content: appendCodeOutputRules\(params\?\.system \?\? \$settings\?\.system\)/);
});

test('default Markdown styling is bundled and loaded by the page', () => {
	const css = source('static/static/custom.css');
	assert.match(css, /\.markdown-prose/);
	assert.match(css, /--final-code-font:/);
	assert.match(css, /line-height: 1\.95 !important/);
	assert.match(source('src/app.html'), /href="\/static\/custom\.css"/);
	assert.ok(source('src/app.html').indexOf('id="custom-stylesheet"') > source('src/app.html').indexOf('%sveltekit.head%'));
});
