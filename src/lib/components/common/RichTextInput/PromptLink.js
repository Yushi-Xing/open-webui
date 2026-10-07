import Link from '@tiptap/extension-link';

// Link's URL paste rule is independent of autolink and linkOnPaste. Prompt
// editors only need the link schema for existing HTML anchors; converting
// literal text URLs can change user input and selected-text replacement.
export const PromptLink = Link.extend({
	addPasteRules() {
		return [];
	}
});
