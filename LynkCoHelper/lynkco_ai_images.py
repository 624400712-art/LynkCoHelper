# -*- coding: utf-8 -*-
"""Prepare oversized article images for GLM without changing article data."""

import base64
import io
import ipaddress
import socket
import time
import warnings
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from lynkco_ai_common import CommentGenerationError


MAX_IMAGE_BYTES = 5_000_000
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 36_000_000
MAX_IMAGE_EDGE = 6000
COMPRESSED_EDGE = 1600
IMAGE_CDN_HOST = "app-cdn.lynkco.com"
DOWNLOAD_DEADLINE_SECONDS = 30


def _check_public_host(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CommentGenerationError("动态图片地址无效")
    if parsed.hostname.casefold() != IMAGE_CDN_HOST or parsed.port not in (None, 443):
        raise CommentGenerationError("动态图片不属于受信任的领克图片域名")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise CommentGenerationError("动态图片地址指向非公网地址")
    except (socket.gaierror, ValueError, OverflowError) as exc:
        raise CommentGenerationError("动态图片域名解析失败") from exc


def _download(url: str) -> bytes:
    _check_public_host(url)
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    with requests.Session() as session:
        session.trust_env = False
        try:
            with session.get(url, stream=True, allow_redirects=False, timeout=(5, 15)) as response:
                if 300 <= response.status_code < 400:
                    raise CommentGenerationError("动态图片下载不允许跳转")
                if response.status_code != 200:
                    raise CommentGenerationError(f"动态图片下载失败（HTTP {response.status_code}）")
                size_hint = response.headers.get("Content-Length")
                if size_hint and size_hint.isdecimal() and int(size_hint) > MAX_DOWNLOAD_BYTES:
                    raise CommentGenerationError("动态图片超过下载大小限制")
                content = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if time.monotonic() > deadline:
                        raise CommentGenerationError("动态图片下载超时")
                    content.extend(chunk)
                    if len(content) > MAX_DOWNLOAD_BYTES:
                        raise CommentGenerationError("动态图片超过下载大小限制")
                return bytes(content)
        except requests.RequestException as exc:
            raise CommentGenerationError(f"动态图片下载失败（{type(exc).__name__}）") from None


def _compress(content: bytes, index: int) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise CommentGenerationError("动态图片像素超过处理限制")
                old_size = image.size
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image = ImageOps.exif_transpose(image)
                image.load()
                if len(content) < MAX_IMAGE_BYTES and max(image.size) <= MAX_IMAGE_EDGE:
                    return ""
                image.thumbnail((COMPRESSED_EDGE, COMPRESSED_EDGE), Image.Resampling.LANCZOS)
                if "A" in image.getbands() or "transparency" in image.info:
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=85, optimize=True)
                compressed = output.getvalue()
                if len(compressed) >= MAX_IMAGE_BYTES:
                    raise CommentGenerationError("动态图片压缩后仍超过模型限制")
                print(f"[AI] image {index}: {len(content)} -> {len(compressed)} bytes, "
                      f"{old_size[0]}x{old_size[1]} -> {image.width}x{image.height}", flush=True)
                return "data:image/jpeg;base64," + base64.b64encode(compressed).decode("ascii")
    except CommentGenerationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise CommentGenerationError(f"动态图片解码或压缩失败（{type(exc).__name__}）") from None


def prepare_glm_images(images: list) -> list:
    """Keep compliant URLs; embed only oversized images as compressed JPEGs."""
    prepared = []
    for index, url in enumerate(images, start=1):
        content = _download(url)
        compressed = _compress(content, index)
        prepared.append(compressed or url)
    return prepared
