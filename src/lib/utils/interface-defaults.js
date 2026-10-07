export const DEFAULT_TEXT_SCALE = 1.2;

/** @param {{ textScale?: number | null }} [settings] */
export const getDefaultTextScale = (settings = {}) => settings.textScale ?? DEFAULT_TEXT_SCALE;
