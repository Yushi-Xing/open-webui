import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

export const codeEditorReadOnlyExtensions = (readOnly: boolean) => [
	EditorState.readOnly.of(readOnly),
	EditorView.editable.of(!readOnly),
	EditorView.contentAttributes.of(
		readOnly ? { 'aria-readonly': 'true', tabindex: '0' } : { 'aria-readonly': 'false' }
	)
];
