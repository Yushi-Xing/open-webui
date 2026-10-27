"""Token estimates and bounded text chunks without loading application state."""

import logging
import math
import os
from functools import lru_cache

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken

        # Standard images populate this encoding's cache during the image build.
        return tiktoken.get_encoding(os.getenv('TIKTOKEN_ENCODING_NAME', 'cl100k_base'))
    except Exception:
        log.warning('Context tokenizer unavailable; using conservative UTF-8 byte counts')
        return None


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    encoding = _encoding()
    if encoding is None:
        return len(text.encode('utf-8', errors='replace'))
    # This is an estimate for other providers, not their exact tokenizer. Keep
    # headroom and treat special-token-looking user text as ordinary text.
    return math.ceil(len(encoding.encode(text, disallowed_special=())) * 1.15)


def split_text_to_budget(text: str, budget: int):
    """Yield lossless Unicode chunks; every chunk fits the supplied estimate."""
    if budget < 4:
        raise ValueError('Context budget is too small for text chunks')
    offset = 0
    while offset < len(text):
        end = min(len(text), offset + budget * 4)
        if estimate_text_tokens(text[offset:end]) > budget:
            low, high = offset + 1, end
            while low < high:
                mid = (low + high + 1) // 2
                if estimate_text_tokens(text[offset:mid]) <= budget:
                    low = mid
                else:
                    high = mid - 1
            end = low
        chunk = text[offset:end]
        if estimate_text_tokens(chunk) > budget:
            raise ValueError('Context budget is too small for one character')
        yield chunk
        offset = end
