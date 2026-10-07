/**
 * Keep pasted HTML links in prompt editors without changing literal URLs or
 * turning a pasted URL into a link on the current selection.
 * @param {{ link: boolean, messageInput: boolean, richText: boolean, autoFormat: boolean }} options
 */
export const getRichTextLinkOptions = ({ link, messageInput, richText, autoFormat }) => {
	if (!link && !(messageInput && richText)) return false;

	return {
		autolink: autoFormat && !messageInput,
		linkOnPaste: autoFormat && !messageInput,
		openOnClick: !messageInput
	};
};
