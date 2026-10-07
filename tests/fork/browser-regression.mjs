import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { chromium } from 'playwright';

const project = fileURLToPath(new URL('../../', import.meta.url));
const fixture = fileURLToPath(new URL('./browser/', import.meta.url));
const alias = (find, replacement) => ({ find, replacement: fixture + replacement });
const server = await createServer({
	configFile: false,
	root: fixture,
	publicDir: project + 'static',
	plugins: [svelte({ configFile: false, preprocess: vitePreprocess() })],
	resolve: { alias: [
		alias(/^\$lib\/stores$/, 'stores.js'),
		alias(/^\$lib\/utils$/, 'utils.js'),
		alias(/^\$lib\/apis\/utils$/, 'services.js'),
		alias(/^\$lib\/pyodide\/createPyodideWorker$/, 'services.js'),
		alias(/^svelte-sonner$/, 'toast.js'),
		{ find: /^\$lib\/components\/common\/Tooltip.svelte$/, replacement: project + 'tests/fork/fixtures/Tooltip.svelte' },
		{ find: /^\$lib\/components\/common\/SVGPanZoom.svelte$/, replacement: project + 'tests/fork/fixtures/Tooltip.svelte' },
		{ find: '$lib', replacement: project + 'src/lib' }
	] },
	server: { host: '127.0.0.1', port: 4177, strictPort: true, fs: { allow: [project] } }
});
let browser;
let page;
const results = [];
const check = (name, value) => { assert.ok(value, name); results.push(name); console.log('PASS:', name); };
await mkdir('artifacts', { recursive: true });
try {
	await server.listen();
	browser = await chromium.launch();
	const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'], viewport: { width: 1100, height: 700 } });
	page = await context.newPage();
	const errors = [];
	page.on('pageerror', error => errors.push(error.message));
	await page.goto('http://127.0.0.1:4177');
	await page.waitForSelector('.cm-line span');
	await page.waitForFunction(() => document.querySelector('.cm-content')?.getAttribute('aria-readonly') === 'true');
	const stats = () => page.evaluate(() => {
		const body = getComputedStyle(document.querySelector('.markdown-prose p'));
		const code = getComputedStyle(document.querySelector('.cm-content'));
		return { bodySize: body.fontSize, bodyHeight: body.lineHeight, codeSize: code.fontSize, codeHeight: code.lineHeight,
			background: getComputedStyle(document.querySelector('.cm-editor')).backgroundColor,
			header: getComputedStyle(document.querySelector('.sticky')).backgroundColor,
			editable: document.querySelector('.cm-content').getAttribute('contenteditable') };
	});
	let light = await stats();
	check('original body typography wins over scaled app defaults', light.bodySize === '16px' && light.bodyHeight === '31.2px');
	check('original code typography is retained', light.codeSize === '13px' && light.codeHeight === '20.15px');
	check('original light code background and toolbar are retained', light.background === 'rgb(242, 242, 242)' && light.header === 'rgb(248, 248, 248)');
	check('CodeMirror has line numbers and fold controls', await page.locator('.cm-lineNumbers').count() === 1 && await page.locator('.cm-foldGutter').count() === 1);
	check('reply code is not contenteditable', light.editable === 'false');
	const original = await page.evaluate(() => window.previewEditor().state.doc.toString());
	await page.locator('.cm-content').focus();
	await page.keyboard.type('UNWANTED EDIT');
	await page.keyboard.press('Backspace');
	await page.keyboard.press('Tab');
	await page.keyboard.press('Control+Shift+f');
	check('typing, deleting, indenting and formatting do not modify replies', await page.evaluate(() => window.previewEditor().state.doc.toString()) === original);
	await page.evaluate(() => {
		const view = window.previewEditor();
		view.dispatch({ selection: { anchor: 0, head: view.state.doc.length } });
		view.focus();
	});
	await page.keyboard.press('Control+x');
	await page.evaluate(() => navigator.clipboard.writeText('PASTED EDIT'));
	await page.keyboard.press('Control+v');
	check('cut and paste do not modify replies', await page.evaluate(() => window.previewEditor().state.doc.toString()) === original);
	await page.locator('.copy-code-button').click();
	check('copy still returns the original reply code', await page.evaluate(() => navigator.clipboard.readText()) === original);
	await page.evaluate(() => window.updatePreviewCode('print("streamed update")'));
	await page.waitForFunction(() => window.previewEditor().state.doc.toString() === 'print("streamed update")');
	check('streamed programmatic code updates remain visible', true);
	await page.evaluate(() => {
		const appStyle = [...document.querySelectorAll('style[data-vite-dev-id]')].find(style => style.dataset.viteDevId.endsWith('/src/app.css'));
		if (!appStyle) throw new Error('Actual app stylesheet was not loaded');
		document.head.appendChild(appStyle);
	});
	await page.waitForFunction(() => getComputedStyle(document.querySelector('.markdown-prose p')).fontSize === '16px');
	check('later app stylesheet loading cannot override custom body size', (await stats()).bodySize === '16px');
	await page.screenshot({ path: 'artifacts/markdown-light-readonly.png', fullPage: true });
	await page.evaluate(() => document.documentElement.classList.replace('light', 'dark'));
	await page.waitForSelector('.cm-editor.cm-dark');
	const dark = await stats();
	check('dark mode uses its original code and toolbar backgrounds', dark.background === 'rgb(32, 32, 32)' && dark.header === 'rgb(41, 41, 41)');
	check('dark mode preserves read-only typography', dark.editable === 'false' && dark.codeSize === '13px' && dark.bodySize === '16px');
	await page.screenshot({ path: 'artifacts/markdown-dark-readonly.png', fullPage: true });
	check('no browser runtime errors', errors.length === 0);
	await writeFile('artifacts/browser-regression.json', JSON.stringify({ results, light, dark }, null, 2));
} catch (error) {
	if (page) await page.screenshot({ path: 'artifacts/markdown-browser-failure.png', fullPage: true }).catch(() => {});
	throw error;
} finally {
	await browser?.close();
	await server.close();
}
