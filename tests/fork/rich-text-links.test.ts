// @vitest-environment jsdom
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import TurndownService from 'turndown';
import { getRichTextLinkOptions } from '../../src/lib/utils/rich-text-link-options.js';

const editors: Editor[] = [];
const options = { link: false, messageInput: true, richText: true, autoFormat: true };

// jsdom has no ClipboardEvent constructor. ProseMirror's paste helpers use it
// to tag an otherwise real DOM/schema/transaction paste operation.
beforeAll(() => {
	vi.stubGlobal(
		'ClipboardEvent',
		class extends Event {
			clipboardData = null;
		}
	);
});
afterAll(() => vi.unstubAllGlobals());

const createEditor = (content: any = '', linkOptions = getRichTextLinkOptions(options)) => {
	const element = document.createElement('div');
	document.body.appendChild(element);
	const editor = new Editor({
		element,
		extensions: [StarterKit.configure({ link: linkOptions })],
		content
	});
	editors.push(editor);
	return editor;
};

const markdown = (editor: Editor) => {
	const service = new TurndownService();
	service.escape = (text) => text;
	return service.turndown(editor.getHTML());
};

afterEach(() => {
	editors.splice(0).forEach((editor) => editor.destroy());
	document.body.replaceChildren();
});

describe('rich text link paste and prompt serialization', () => {
	it('reproduces the original loss when the link extension is disabled', () => {
		const editor = createEditor('', false);
		editor.view.pasteHTML('<a href="https://example.com/article">Article title</a>');
		expect(markdown(editor)).toBe('Article title');
		expect(editor.getHTML()).not.toContain('href');
	});

	it('preserves a titled HTML link in the submitted Markdown', () => {
		const editor = createEditor();
		editor.view.pasteHTML('<a href="https://example.com/article">Article title</a>');
		expect(markdown(editor)).toBe('[Article title](https://example.com/article)');
		expect(editor.getJSON().content?.[0].content?.[0].marks?.[0].attrs?.href).toBe(
			'https://example.com/article'
		);
	});

	it('retains query parameters, fragments and multiple links in surrounding text', () => {
		const editor = createEditor();
		editor.view.pasteHTML(
			'<p>Read <a href="https://example.com/a?x=1&amp;y=2#section">first</a> and <a href="https://example.org/b">second</a>.</p>'
		);
		expect(markdown(editor)).toBe(
			'Read [first](https://example.com/a?x=1&y=2#section) and [second](https://example.org/b).'
		);
	});

	it('keeps a bare pasted URL literal', () => {
		const editor = createEditor();
		editor.view.pasteText('https://example.com/article');
		expect(markdown(editor)).toBe('https://example.com/article');
		expect(editor.getHTML()).not.toContain('<a');
	});

	it('replaces selected text with the pasted URL instead of linking the selection', () => {
		const editor = createEditor('<p>Title</p>');
		editor.commands.setTextSelection({ from: 1, to: 6 });
		editor.view.pasteText('https://example.com/article');
		expect(markdown(editor)).toBe('https://example.com/article');
		expect(editor.getHTML()).not.toContain('<a');
	});

	it('preserves links across the JSON handoff to the expanded prompt editor', () => {
		const mainEditor = createEditor();
		mainEditor.view.pasteHTML('<a href="https://example.com/article">Title</a>');
		const expandedEditor = createEditor(mainEditor.getJSON());
		expandedEditor.commands.insertContent(' more');
		mainEditor.commands.setContent(expandedEditor.getJSON());
		expect(markdown(mainEditor)).toContain('[Title](https://example.com/article)');
	});
});
