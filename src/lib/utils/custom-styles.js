// Keep the administrator's stylesheet after styles loaded during navigation.
export const keepCustomStylesLast = (head = document.head) => {
	const custom = head.querySelector('#custom-stylesheet');
	if (!custom) return () => {};
	const reorder = () => {
		let next = custom.nextElementSibling;
		while (next) {
			if (next.matches('style, link[rel~="stylesheet"]')) {
				head.appendChild(custom);
				break;
			}
			next = next.nextElementSibling;
		}
	};
	reorder();
	const observer = new MutationObserver(reorder);
	observer.observe(head, { childList: true });
	return () => observer.disconnect();
};
