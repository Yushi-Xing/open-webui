import { fileURLToPath, URL } from 'node:url';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [svelte({ configFile: false, preprocess: vitePreprocess() })],
	resolve: {
		alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) }
	},
	test: {
		include: ['tests/fork/*.test.ts'],
		environment: 'node',
		pool: 'forks',
		poolOptions: { forks: { singleFork: true } }
	}
});
