# -*- coding: utf-8 -*-
"""ChatAnywhere provider for short, grounded comments."""

from lynkco_ai_common import CommentGenerationError, MAX_COMMENT_CHARS, SYSTEM_PROMPT
from lynkco_ai_common import generate_comment as _generate_comment


API_URL = "https://api.chatanywhere.tech/v1/chat/completions"


def generate_comment(post: dict, api_key: str, model: str = "gpt-4o-mini", session=None) -> str:
    """Generate a comment through ChatAnywhere."""
    return _generate_comment(post, api_key, endpoint=API_URL, model=model, session=session)


__all__ = ["API_URL", "MAX_COMMENT_CHARS", "CommentGenerationError", "generate_comment"]
