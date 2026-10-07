import { mount } from 'svelte';
import { EditorView } from '@codemirror/view';
import App from './App.svelte';
import '../../../src/tailwind.css';
import '../../../src/app.css';
import { keepCustomStylesLast } from '../../../src/lib/utils/custom-styles.js';

keepCustomStylesLast();
mount(App, { target: document.getElementById('app') });
window.previewEditor = () => EditorView.findFromDOM(document.querySelector('.cm-editor'));
window.previewDarkTheme = () => window.previewEditor().state.facet(EditorView.darkTheme);
