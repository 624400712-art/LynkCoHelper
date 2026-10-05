# -*- coding: utf-8 -*-
"""GLM provider using the shared OpenAI-compatible request contract."""

from lynkco_ai_common import CommentGenerationError, generate_comment as _generate_comment
from lynkco_ai_images import prepare_glm_images


API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def generate_comment(post: dict, api_key: str, model: str = "glm-4v-flash", session=None) -> str:
    """Generate a comment through GLM."""
    return _generate_comment(post, api_key, endpoint=API_URL, model=model, session=session,
                             headers={"Content-Type": "application/json"},
                             prepare_images=prepare_glm_images)


__all__ = ["API_URL", "CommentGenerationError", "generate_comment"]
