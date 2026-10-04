# -*- coding: utf-8 -*-
from __future__ import annotations
"""Read recent text/image posts from the exploration feed without guessing IDs."""

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from lynkco_share import EP_EXPLORE_HOME_V3_LIST


UTC = timezone.utc
RELATION_TYPES = {"content", "article", "image", "text"}
VIDEO_TYPES = {"video", "视频", "短视频"}
ARTICLE_DETAIL_PATH = "/app/explore/home-page/article/content/"


class _ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.images = []
        self.has_video = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ("video", "iframe") or (tag == "source" and
                (str(attributes.get("type") or "").startswith("video/") or
                 str(attributes.get("src") or "").lower().split("?", 1)[0].endswith(
                     (".mp4", ".mov", ".m3u8", ".webm")))):
            self.has_video = True
        if tag == "img":
            image = _https_url(attributes.get("src"))
            if image and image not in self.images:
                self.images.append(image)

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())


def _utc_time(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value >= 10 ** 11 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        if isinstance(value, str):
            if value.isdecimal():
                return _utc_time(int(value))
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (OverflowError, ValueError, OSError):
        return None
    return None


def _https_url(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return value if parsed.scheme.lower() == "https" and parsed.netloc else ""


def _image_urls(raw):
    result = []
    for image in raw if isinstance(raw, list) else []:
        url = image if isinstance(image, str) else image.get("url") if isinstance(image, dict) else None
        url = _https_url(url)
        if url and url not in result:
            result.append(url)
    return result


def _attachment_images(raw):
    if not isinstance(raw, list):
        return []
    return _image_urls([item.get("fileUrl") for item in raw
                        if isinstance(item, dict) and item.get("type") == "picture"])


def _article_card(raw):
    if not isinstance(raw, dict) or str(raw.get("id") or "") != str(raw.get("newId") or "") or \
            not raw.get("id") or raw.get("shelf") is not True or raw.get("auditStatus") != 2:
        return None
    return normalize_post({
        "origin": 1, "id": raw["id"], "authorId": raw.get("authorId"),
        "publishTime": raw.get("publishTime"), "title": raw.get("title"),
        "summary": raw.get("desc"), "coverImage": raw.get("coverUrl"),
    })


def normalize_post(raw: dict) -> dict | None:
    """Return a post with known comment relation and analyzable content."""
    if not isinstance(raw, dict):
        return None
    origin = raw.get("origin")
    if "origin" in raw and (isinstance(origin, bool) or origin not in (1, 2)):
        return None
    if origin == 1:
        identifier = raw.get("id")
        title = raw.get("title")
        text = raw.get("summary")
        cover_image = _https_url(raw.get("coverImage"))
        images = [cover_image] if cover_image else []
        relation_type = "content"
        kind = "article"
    elif origin == 2:
        identifier = raw.get("ugcId")
        title = ""
        text = raw.get("content")
        images = _image_urls([url for url in raw.get("contentImage", []) if isinstance(url, str)]
                              if isinstance(raw.get("contentImage"), list) else [])
        cover_image = images[0] if images else ""
        relation_type = "content"
        kind = "ugc"
    else:
        identifier = raw.get("dynamicId") or raw.get("contentId") or raw.get("articleId")
        title = raw.get("title")
        text = raw.get("content") if isinstance(raw.get("content"), str) else raw.get("summary")
        attachments = raw.get("dynamicAttachFileDtos")
        if isinstance(attachments, list) and any(
            isinstance(item, dict) and item.get("type") not in (None, "picture")
            for item in attachments
        ):
            return None
        images = _image_urls(raw.get("images")) + _attachment_images(attachments)
        cover_image = _https_url(raw.get("coverImage"))
        if not images and cover_image:
            images = [cover_image]
        relation_type = raw.get("relaTypeCode")
        kind = "article" if raw.get("articleId") else "ugc"
    if not isinstance(relation_type, str) or relation_type not in RELATION_TYPES:
        return None
    if any(isinstance(raw.get(key), str) and raw[key].strip().lower() in VIDEO_TYPES
           for key in ("contentType", "contentTypeCode")):
        return None
    if raw.get("videoUrl") or raw.get("videoList") or raw.get("videos"):
        return None
    if not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or not str(identifier).strip():
        return None
    published_at = _utc_time(raw.get("publishTime"))
    if published_at is None:
        return None
    title = title if isinstance(title, str) else ""
    text = text if isinstance(text, str) else ""
    if not (text.strip() or images or (origin is None and title.strip())):
        return None
    author_id = raw.get("authorId")
    return {
        "id": str(identifier).strip(),
        "relation_type": relation_type,
        "published_at": published_at,
        "title": title.strip(),
        "text": text.strip(),
        "images": images,
        "kind": kind,
        "cover_image": cover_image or (images[0] if images else ""),
        "author_id": str(author_id) if isinstance(author_id, (str, int)) and not isinstance(author_id, bool) else "",
        "jump_url": _https_url(raw.get("jumpUrl")),
    }


def _feed_items(payload):
    data = payload.get("data")
    if isinstance(data, dict):
        query_result = data.get("queryResult")
        items = query_result.get("items") if isinstance(query_result, dict) else None
        components = data.get("pageDetailList")
        if items is not None and not isinstance(items, list):
            raise ValueError("v3 feed response shape is unknown")
        cards = []
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, dict) or str(component.get("cptCode")) != "1009":
                    continue
                container = component.get("data")
                groups = container.get("data") if isinstance(container, dict) else None
                if isinstance(groups, list):
                    for group in groups:
                        card = group.get("data") if isinstance(group, dict) else None
                        if isinstance(card, dict):
                            cards.append(card)
        if isinstance(items, list) or isinstance(components, list):
            return items or [], cards
    raise ValueError("v3 feed response shape is unknown")


def fetch_article_detail(share_client, post):
    """Re-read a listed article and return bounded, validated model content."""
    if post.get("kind") != "article" or not post.get("author_id"):
        raise ValueError("article identity is incomplete")
    identifier = post["id"]
    if not isinstance(identifier, str) or not identifier.isdecimal():
        raise ValueError("article ID is invalid")
    response = share_client._request("GET", ARTICLE_DETAIL_PATH + identifier)
    if response.status_code != 200:
        raise ValueError("article detail HTTP request failed")
    payload = response.json()
    outer = payload.get("data") if isinstance(payload, dict) and payload.get("code") == "success" else None
    detail = outer.get("data") if isinstance(outer, dict) and outer.get("code") == "success" else None
    if not isinstance(detail, dict):
        raise ValueError("article detail business request failed")
    if str(detail.get("id") or "") != identifier or \
            str(detail.get("authorId") or "") != post["author_id"] or \
            _utc_time(detail.get("publishTime")) != post["published_at"] or \
            detail.get("shelf") is not True or detail.get("auditStatus") != 2 or \
            detail.get("commentTypeCode") != "content-comment":
        raise ValueError("article detail identity or availability changed")
    if any(isinstance(detail.get(key), str) and detail[key].strip().lower() in VIDEO_TYPES
           for key in ("contentType", "contentTypeCode")) or \
            any(detail.get(key) for key in ("videoUrl", "videoList", "videos")):
        raise ValueError("article detail includes video")
    html = detail.get("detail")
    if not isinstance(html, str):
        raise ValueError("article detail body is missing")
    parser = _ArticleHTML()
    parser.feed(html)
    if parser.has_video:
        raise ValueError("article detail includes video")
    title = detail.get("title") if isinstance(detail.get("title"), str) else ""
    description = detail.get("desc") if isinstance(detail.get("desc"), str) else ""
    text = re.sub(r"\s+", " ", " ".join([description, *parser.text])).strip()[:3000]
    images = _image_urls([detail.get("coverUrl"), *parser.images])[:3]
    if not (title.strip() or text or images):
        raise ValueError("article detail has no analyzable content")
    return {**post, "title": title.strip()[:200], "text": text,
            "images": images, "cover_image": _https_url(detail.get("coverUrl"))}


def fetch_recent_posts(share_client, pages=1) -> list[dict]:
    """Fetch bounded v3 pages, retaining only explicitly shaped posts."""
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 20:
        raise ValueError("pages must be between 1 and 20")
    posts = []
    seen = set()
    for page_no in range(1, pages + 1):
        response = share_client._request("GET", EP_EXPLORE_HOME_V3_LIST,
                                         params={"pageNo": page_no, "pageSize": 20,
                                                 "pageCode": "LYNKCO_APP_1046"})
        if response.status_code != 200:
            raise ValueError("v3 feed HTTP request failed")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != "success":
            raise ValueError("v3 feed business request failed")
        items, cards = _feed_items(payload)
        if not items and not cards:
            break
        usable = 0
        for raw, is_card in [(raw, False) for raw in items] + [(raw, True) for raw in cards]:
            post = _article_card(raw) if is_card else normalize_post(raw)
            if post is not None:
                usable += 1
                if post["id"] not in seen:
                    posts.append(post)
                    seen.add(post["id"])
        if not usable:
            raise ValueError("v3 feed contains no usable posts; verify publication time and content metadata")
    return posts


def eligible_posts(posts, seen_ids, now, max_age_hours=48) -> list[dict]:
    """Select fresh, not-yet-commented posts from newest to oldest."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = now.astimezone(UTC) - timedelta(hours=max_age_hours)
    eligible = (post for post in posts if post["id"] not in seen_ids
                and cutoff <= post["published_at"] <= now.astimezone(UTC))
    return sorted(eligible, key=lambda post: post["published_at"], reverse=True)
