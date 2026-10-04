# -*- coding: utf-8 -*-
"""GLM provider using the same OpenAI-compatible request contract."""

import requests

from lynkco_chatanywhere import CommentGenerationError, generate_comment as _generate_comment


API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


class _EndpointSession:
    """Route the existing generator's request to the GLM endpoint."""

    def __init__(self, session=None):
        self._session = session or requests.Session()
        self.endpoint = API_URL

    def post(self, _url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Content-Type", "application/json")
        kwargs["headers"] = headers
        return self._session.post(API_URL, **kwargs)


def generate_comment(post: dict, api_key: str, model: str = "glm-4v-flash", session=None) -> str:
    """Generate a comment through GLM while preserving the existing contract."""
    return _generate_comment(
        post,
        api_key,
        model=model,
        session=_EndpointSession(session),
    )


__all__ = ["API_URL", "CommentGenerationError", "generate_comment"]
