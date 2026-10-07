import { DEFAULT_TEXT_SCALE } from './interface-defaults';

export const setTextScale = (scale: number = DEFAULT_TEXT_SCALE) => {
	if (typeof document === 'undefined') {
		return;
	}

	document.documentElement.style.setProperty('--app-text-scale', `${scale}`);
};

export const normalizeAppFontFamily = (fontFamily?: string | null) =>
	typeof fontFamily === 'string' ? fontFamily.trim().replace(/[\u0000-\u001f\u007f]/g, '') : '';

export const setAppFontFamily = (fontFamily?: string | null) => {
	if (typeof document === 'undefined') {
		return;
	}

	const normalized = normalizeAppFontFamily(fontFamily);
	if (!normalized) {
		document.documentElement.style.removeProperty('--app-font-family');
		return;
	}

	document.documentElement.style.setProperty(
		'--app-font-family',
		`"${normalized.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
	);
};
