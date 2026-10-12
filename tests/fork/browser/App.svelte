<script>
	import { setContext } from 'svelte';
	import { writable } from 'svelte/store';
	import CodeBlock from '$lib/components/chat/Messages/CodeBlock.svelte';
	import ModelControls from '$lib/components/admin/Settings/Models/ModelControls.svelte';
	setContext('i18n', writable({ t: (value) => value }));
	let controls;
	let saves = 0;
	window.modelControlsPreview = () => ({ controls, saves });
	let code = '# app.py\nfrom flask import Flask\n\napp = Flask(__name__)\n\n@app.route("/")\ndef index():\n    return "Hello, Flask!"';
	window.updatePreviewCode = (value) => { code = value; };
</script>

<main class="markdown-prose" style="max-width: 900px; margin: 24px auto;">
	<h2>Markdown rendering regression</h2>
	<p>正文固定 16px，行距 1.95。CodeMirror 保留行号、折叠、高亮和复制。</p>
	<CodeBlock id="readonly-preview" lang="python" {code} edit={false} run={false} />
</main>

<form id="model-controls-parent" on:submit|preventDefault={() => saves++}>
	<input aria-label="Model name" required value="Test model" />
	<ModelControls bind:controls />
	<button type="submit">Save model</button>
</form>
