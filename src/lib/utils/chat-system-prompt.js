export const CODE_OUTPUT_RULES = `Additional rules for code output
1. Generated program code, HTML, CSS, JavaScript, Python, Shell, SQL, configuration files, text templates, and similar content must be returned directly in the chat reply. Use Markdown code blocks with the correct language identifier.
2. If the user asks you to "generate a file", create the file and also print its contents in the terminal.`;

/** @param {string | null | undefined} [systemPrompt] */
export const appendCodeOutputRules = (systemPrompt = '') => {
	const prompt = systemPrompt ?? '';
	if (prompt.includes(CODE_OUTPUT_RULES)) return prompt;
	return prompt ? `${prompt}\n\n${CODE_OUTPUT_RULES}` : CODE_OUTPUT_RULES;
};
