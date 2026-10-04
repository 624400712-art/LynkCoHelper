# -*- coding: utf-8 -*-
"""Restore the newest GitHub Actions comment state artifact, failing closed."""

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit
import zipfile

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lynkco_comment import DEFAULT_STATE, save_state_atomic, validate_state


ARTIFACT_NAME = "lynkco-comment-state"
API_ROOT = "https://api.github.com"
MAX_STATE_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ARTIFACTS = 1000


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except (AttributeError, ValueError):
        pass
    raise ValueError("artifact timestamp is invalid")


def _preserve_headers(request):
    """Override session/netrc auth without changing explicit request headers."""
    return request


def _get(session, url, headers, **kwargs):
    try:
        return session.get(url, headers=headers, auth=_preserve_headers,
                           timeout=30, allow_redirects=False, **kwargs)
    except requests.exceptions.RequestException:
        raise RuntimeError("artifact request failed") from None


def _newest_artifact(repo, session, headers, branch=None):
    url = f"{API_ROOT}/repos/{repo}/actions/artifacts"
    artifacts = []
    total = None
    page = 1
    while total is None or len(artifacts) < total:
        response = _get(session, url, headers, params={"name": ARTIFACT_NAME, "per_page": 100, "page": page})
        if response.status_code != 200:
            raise RuntimeError("artifact listing failed")
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("artifact listing is invalid") from None
        if not isinstance(payload, dict) or type(payload.get("total_count")) is not int or \
                payload["total_count"] < 0 or not isinstance(payload.get("artifacts"), list):
            raise ValueError("artifact listing is invalid")
        if total is None:
            total = payload["total_count"]
            if total > MAX_ARTIFACTS:
                raise ValueError("artifact listing is too large")
        elif total != payload["total_count"]:
            raise ValueError("artifact listing changed during pagination")
        batch = payload["artifacts"]
        if total == 0:
            if batch:
                raise ValueError("artifact listing is inconsistent")
            return None
        if not batch or len(batch) > 100 or len(artifacts) + len(batch) > total:
            raise ValueError("artifact listing is incomplete")
        artifacts.extend(batch)
        page += 1

    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") != ARTIFACT_NAME or \
                type(artifact.get("id")) is not int or artifact["id"] <= 0 or \
                type(artifact.get("expired")) is not bool:
            raise ValueError("artifact metadata is invalid")
        artifact["_created_at"] = _timestamp(artifact.get("created_at"))
        artifact["_expires_at"] = _timestamp(artifact.get("expires_at"))
        if artifact["id"] in seen:
            raise ValueError("artifact listing has duplicate IDs")
        seen.add(artifact["id"])
    if branch is not None:
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError("workflow ref is invalid")
        artifacts = [artifact for artifact in artifacts
                     if isinstance(artifact.get("workflow_run"), dict) and
                     artifact["workflow_run"].get("head_branch") == branch]
    return max(artifacts, key=lambda artifact: (artifact["_created_at"], artifact["id"])) if artifacts else None


def _download_artifact(repo, artifact, session, headers):
    url = f"{API_ROOT}/repos/{repo}/actions/artifacts/{artifact['id']}/zip"
    response = _get(session, url, headers)
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        parsed = urlsplit(location)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("artifact download redirect is invalid")
        # The None override removes a session-level bearer header before signing the URL request.
        response = _get(session, location, {"Authorization": None})
    if response.status_code != 200:
        raise RuntimeError("artifact download failed")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_ARCHIVE_BYTES:
                raise ValueError("comment state artifact archive is too large")
        except (TypeError, ValueError):
            if isinstance(content_length, str) and content_length.isdigit():
                raise
            raise ValueError("artifact download size is invalid") from None
    chunks = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("comment state artifact archive is too large")
            chunks.append(chunk)
    except requests.exceptions.RequestException:
        raise RuntimeError("artifact download failed") from None
    return b"".join(chunks)


def restore_latest_state(repo: str, token: str, path: Path, session=None) -> bool:
    """Restore newest state, or return False only when no named artifact exists."""
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("GitHub repository is invalid")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("GitHub token is missing")
    session = session if session is not None else requests.Session()
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    artifact = _newest_artifact(repo, session, headers, os.environ.get("LYNKCO_COMMENT_REF"))
    if artifact is None:
        return False
    if artifact["expired"] or artifact["_expires_at"] <= datetime.now(timezone.utc):
        raise ValueError("newest comment state artifact is expired")
    content = _download_artifact(repo, artifact, session, headers)
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != ".comment_state.json" or \
                    members[0].is_dir() or members[0].file_size > MAX_STATE_BYTES or \
                    stat.S_IFMT(members[0].external_attr >> 16) not in (0, stat.S_IFREG) or \
                    members[0].flag_bits & 1:
                raise ValueError("comment state artifact has unexpected contents")
            with archive.open(members[0]) as source:
                raw = source.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("comment state artifact is too large")
        state = validate_state(json.loads(raw.decode("utf-8")))
    except (zipfile.BadZipFile, OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("comment state artifact is invalid") from None
    save_state_atomic(Path(path), state)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restore the latest comment state artifact")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    try:
        restored = restore_latest_state(
            os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""), args.state,
        )
    except (OSError, ValueError, RuntimeError):
        parser.exit(1, "Comment state restore failed; inspect the artifact before publishing.\n")
    print("Comment state restored." if restored else "No prior comment state artifact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
