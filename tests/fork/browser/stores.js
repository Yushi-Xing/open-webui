import { writable } from 'svelte/store';
export const config = writable({});
export const user = writable({ role: 'admin' });
export const pyodideWorker = writable(null);
