// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { setTextScale } from '../../src/lib/utils/text-scale';
import { getDefaultTextScale } from '../../src/lib/utils/interface-defaults.js';

afterEach(() => document.documentElement.style.removeProperty('--app-text-scale'));

describe('interface scale initialization and reset', () => {
	it('applies the 1.2 default to the actual document', () => {
		setTextScale();
		expect(document.documentElement.style.getPropertyValue('--app-text-scale')).toBe('1.2');
	});

	it('preserves a saved personal scale', () => {
		setTextScale(getDefaultTextScale({ textScale: 1.4 }));
		expect(document.documentElement.style.getPropertyValue('--app-text-scale')).toBe('1.4');
	});

	it('reset restores the effective administrator default', () => {
		setTextScale(1.4);
		setTextScale(getDefaultTextScale({ textScale: 1.1 }));
		expect(document.documentElement.style.getPropertyValue('--app-text-scale')).toBe('1.1');
	});

	it('reset without an override restores 1.2', () => {
		setTextScale(1.4);
		setTextScale(getDefaultTextScale({}));
		expect(document.documentElement.style.getPropertyValue('--app-text-scale')).toBe('1.2');
	});
});
