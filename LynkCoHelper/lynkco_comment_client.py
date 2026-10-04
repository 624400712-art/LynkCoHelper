# -*- coding: utf-8 -*-
"""Publish a comment using the native gateway signature and live post metadata."""

from datetime import datetime, timedelta, timezone
import json
from urllib.parse import quote

import requests

from lynkco_common import (
    DEFAULT_TIMEOUT, MAX_COMMENT_CHARS, NATIVE_BASE_URL,
    build_ios_signature, build_native_app_headers,
)


COMMENT_PATH = "/app/explore/home-page/comment/create"
CHINA_TIME = timezone(timedelta(hours=8))


class CommentPostError(RuntimeError):
    """The server rejected a comment or returned an invalid confirmation."""


class CommentPostUncertain(CommentPostError):
    """The server may have accepted a POST without confirmable result; do not retry."""


def _required_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


class CommentClient:
    def __init__(self, token: str, account_id: str, device_id: str, session=None):
        token = _required_string(token, "token")
        self.token = token if token.lower().startswith("bearer") else f"bearer{token}"
        self.account_id = _required_string(account_id, "account_id")
        self.device_id = _required_string(device_id, "device_id")
        self.session = session if session is not None else requests.Session()

    def publish(self, post: dict, comment: str) -> dict:
        """Send one POST, returning only a confirmed comment ID.

        A connection failure or timeout is ambiguous: the caller must stop the
        run instead of issuing a second POST for the same post.
        """
        if not isinstance(post, dict):
            raise ValueError("post must be a mapping")
        post_id = _required_string(post.get("id"), "post.id")
        relation_type = _required_string(post.get("relation_type"), "post.relation_type")
        if relation_type != "content":
            raise ValueError("unsupported article relation type")
        kind = _required_string(post.get("kind"), "post.kind")
        if kind != "article":
            raise ValueError("only verified articles may be published")
        author_id = _required_string(post.get("author_id"), "post.author_id")
        published_at = post.get("published_at")
        if not isinstance(published_at, datetime) or published_at.tzinfo is None or published_at.utcoffset() is None:
            raise ValueError("post.published_at must be timezone-aware")
        comment = _required_string(comment, "comment")
        if len(comment) > MAX_COMMENT_CHARS:
            raise ValueError(f"comment exceeds {MAX_COMMENT_CHARS} characters")

        query = {
            "pageType": "2",
            "isPublishLocation": "false",
            "logicalParentId": "0",
            "relaCode": post_id,
            "displayParentId": "0",
            "relaTypeCode": relation_type,
        }
        base_type = 1
        cover_image = post.get("cover_image") or ""
        if not isinstance(cover_image, str):
            raise ValueError("post.cover_image must be a string")
        inside_message = {
            "accountId": author_id,
            "param": {
                "contentType": "文章",
                "contentTitle": comment,
                "baseContent": cover_image,
                "baseType": base_type,
            }
        }
        jump_url = ("pages/exploration/article/index.js?pageCode=LYNKCO_APP_1019&id="
                    + quote(post_id, safe=""))
        body = {
            "content": comment,
            "insideMessage": inside_message,
            "insideMessageJumpUrl": jump_url,
            "atInsideMessage": {"param": {"baseContent": cover_image, "baseType": base_type}},
            "atUserList": [],
            "interactContent": json.dumps(
                [{"type": "span", "value": comment, "data": None}],
                ensure_ascii=False, separators=(",", ":"),
            ),
            "skipDunshanFlag": 0,
        }
        body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        post_time = published_at.astimezone(CHINA_TIME).strftime("%Y-%m-%d %H:%M:%S")
        risk_request_info = json.dumps({
            "commentTopicType": 1,
            "commentTopicID": post_id,
            "commentTopicTime": post_time,
            "commentTargetType": 1,
            "commentTargetID": post_id,
            "commentTargetTime": post_time,
            "openTimeStamp": datetime.now(CHINA_TIME).strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, separators=(",", ":"))

        headers = build_ios_signature(
            "POST", COMMENT_PATH, token=self.token, query=query,
            accept="application/json", content_type="application/json; charset=UTF-8",
            body=body_bytes,
        )
        headers.update(build_native_app_headers(
            device_id=self.device_id,
            token=self.token,
            account_id=self.account_id,
        ))
        headers.update({
            "gl_dev_id": self.device_id,
            "gl_user_id": self.account_id,
            "risk_type": "2",
            "risk_request_info": risk_request_info,
        })

        try:
            response = self.session.post(
                NATIVE_BASE_URL + COMMENT_PATH, params=query, data=body_bytes,
                headers=headers, timeout=DEFAULT_TIMEOUT, allow_redirects=False,
            )
        except requests.exceptions.RequestException:
            raise CommentPostUncertain("comment transport result is uncertain; do not retry") from None
        if response.status_code != 200:
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                raise CommentPostError(f"comment HTTP status {response.status_code}")
            raise CommentPostUncertain("comment HTTP result is uncertain; do not retry")
        try:
            payload = response.json()
        except ValueError:
            raise CommentPostUncertain("comment response could not confirm success; do not retry") from None
        if not isinstance(payload, dict):
            raise CommentPostUncertain("comment response could not confirm success; do not retry")
        if payload.get("code") != "success":
            raise CommentPostError("comment was rejected by the server")
        data = payload.get("data")
        comment_id = data.get("commentId") if isinstance(data, dict) else None
        if not isinstance(comment_id, (str, int)) or isinstance(comment_id, bool) or not str(comment_id).strip():
            raise CommentPostUncertain("comment response did not confirm a commentId; do not retry")
        return {"commentId": str(comment_id)}
