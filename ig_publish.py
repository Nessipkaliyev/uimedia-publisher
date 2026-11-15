"""Publish posts to Instagram via the Instagram API with Instagram Login.

Flow (per Meta docs): create a media container -> (wait for video) -> media_publish.
Media must be reachable by a public HTTPS URL.

CLI:
  python3 ig_publish.py post.json            # publish
  python3 ig_publish.py post.json --dry-run  # print the API calls only

post.json: {"kind": "image|carousel|reel|story", "media_urls": [...], "caption": "..."}
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Unified login (business portfolio + system user token) uses graph.facebook.com.
DEFAULT_API_BASE = "https://graph.facebook.com/v23.0"
MAX_CAPTION_CHARS = 2200
CAROUSEL_MIN_ITEMS = 2
CAROUSEL_MAX_ITEMS = 10
VIDEO_POLL_SECONDS = 10
VIDEO_POLL_ATTEMPTS = 30
HTTP_TIMEOUT_SECONDS = 60
VALID_KINDS = ("image", "carousel", "reel", "story")

log = logging.getLogger("ig_publish")

HttpFn = Callable[[str, str, dict], dict]


class PublishError(Exception):
    """Raised when a post is invalid or the API rejects it."""


@dataclass(frozen=True)
class Post:
    kind: str
    media_urls: tuple[str, ...]
    caption: str = ""
    extra: dict = field(default_factory=dict)


def validate_post(post: Post) -> None:
    if post.kind not in VALID_KINDS:
        raise PublishError(f"Unknown kind '{post.kind}', expected one of {VALID_KINDS}")
    if not post.media_urls:
        raise PublishError("media_urls is empty")
    bad = [u for u in post.media_urls if not u.startswith("https://")]
    if bad:
        raise PublishError(f"Media must be public HTTPS URLs: {bad}")
    if post.kind == "carousel" and not CAROUSEL_MIN_ITEMS <= len(post.media_urls) <= CAROUSEL_MAX_ITEMS:
        raise PublishError(f"Carousel needs {CAROUSEL_MIN_ITEMS}-{CAROUSEL_MAX_ITEMS} items, got {len(post.media_urls)}")
    if post.kind != "carousel" and len(post.media_urls) != 1:
        raise PublishError(f"'{post.kind}' takes exactly one media URL")
    if len(post.caption) > MAX_CAPTION_CHARS:
        raise PublishError(f"Caption is {len(post.caption)} chars, max {MAX_CAPTION_CHARS}")


def _is_video(url: str) -> bool:
    return urllib.parse.urlparse(url).path.lower().endswith((".mp4", ".mov"))


def _media_params(url: str) -> dict:
    return {"video_url": url} if _is_video(url) else {"image_url": url}


def _with_caption(params: dict, caption: str) -> dict:
    return {**params, "caption": caption} if caption else params


def _create(http: HttpFn, user_id: str, params: dict) -> str:
    response = http("POST", f"{user_id}/media", params)
    container_id = response.get("id")
    if not container_id:
        raise PublishError(f"No container id in response: {response}")
    return container_id


def _wait_until_ready(http: HttpFn, container_id: str, sleep: Callable[[float], None]) -> None:
    for _ in range(VIDEO_POLL_ATTEMPTS):
        status = http("GET", container_id, {"fields": "status_code"}).get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise PublishError(f"Video processing failed for container {container_id}: {status}")
        sleep(VIDEO_POLL_SECONDS)
    raise PublishError(f"Video not processed after {VIDEO_POLL_ATTEMPTS * VIDEO_POLL_SECONDS}s")


def _build_container(post: Post, user_id: str, http: HttpFn, sleep: Callable[[float], None]) -> str:
    url = post.media_urls[0]
    if post.kind == "image":
        return _create(http, user_id, _with_caption(_media_params(url), post.caption))
    if post.kind == "story":
        container_id = _create(http, user_id, {**_media_params(url), "media_type": "STORIES"})
        if _is_video(url):
            _wait_until_ready(http, container_id, sleep)
        return container_id
    if post.kind == "reel":
        params = _with_caption({"media_type": "REELS", "video_url": url}, post.caption)
        container_id = _create(http, user_id, {**params, **post.extra})
        _wait_until_ready(http, container_id, sleep)
        return container_id
    children = [_create(http, user_id, {**_media_params(u), "is_carousel_item": "true"}) for u in post.media_urls]
    for child_id, child_url in zip(children, post.media_urls):
        if _is_video(child_url):
            _wait_until_ready(http, child_id, sleep)
    parent = _with_caption({"media_type": "CAROUSEL", "children": ",".join(children)}, post.caption)
    return _create(http, user_id, parent)


def publish(post: Post, user_id: str, http: HttpFn, sleep: Callable[[float], None] = time.sleep) -> str:
    """Publish one post and return the published media id."""
    validate_post(post)
    container_id = _build_container(post, user_id, http, sleep)
    response = http("POST", f"{user_id}/media_publish", {"creation_id": container_id})
    media_id = response.get("id")
    if not media_id:
        raise PublishError(f"Publish returned no media id: {response}")
    log.info("Published %s as media %s", post.kind, media_id)
    return media_id


def make_http(token: str, api_base: str = DEFAULT_API_BASE) -> HttpFn:
    """Real HTTP client. Token goes in the Authorization header, never in logs."""

    def call(method: str, path: str, params: dict) -> dict:
        url = f"{api_base}/{path}"
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if method == "GET":
            url = f"{url}?{urllib.parse.urlencode(params)}"
        else:
            data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")
            raise PublishError(f"{method} {path} -> HTTP {err.code}: {body}") from err
        except urllib.error.URLError as err:
            raise PublishError(f"{method} {path} -> network error: {err.reason}") from err

    return call


def make_dry_run_http() -> HttpFn:
    counter = {"n": 0}

    def call(method: str, path: str, params: dict) -> dict:
        log.info("DRY-RUN %s %s %s", method, path, params)
        counter["n"] += 1
        return {"id": f"dry-{counter['n']}", "status_code": "FINISHED"}

    return call


def load_env(path: Path) -> dict:
    """Minimal .env reader (KEY=VALUE lines) so no extra dependency is needed."""
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def load_post(path: Path, resolve_file: Callable[[Path], str] | None = None) -> Post:
    """Read a post JSON. Local "media_files" (relative to the JSON) are uploaded via resolve_file."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"Cannot read post file {path}: {err}") from err
    files = raw.get("media_files", [])
    if files and resolve_file is None:
        raise PublishError("Post has media_files but no uploader is configured")
    uploaded = tuple(resolve_file((path.parent / f).resolve()) for f in files) if files else ()
    return Post(
        kind=raw.get("kind", ""),
        media_urls=tuple(raw.get("media_urls", [])) + uploaded,
        caption=raw.get("caption", ""),
        extra=raw.get("extra", {}),
    )


def _make_uploader(env: dict) -> Callable[[Path], str] | None:
    """Cloudinary uploader if configured; None keeps URL-only posts working without it."""
    from cloud_upload import CloudConfig, UploadError, upload

    try:
        config = CloudConfig.from_env(env)
    except UploadError:
        return None

    def resolve(path: Path) -> str:
        try:
            return upload(path, config)
        except UploadError as err:
            raise PublishError(str(err)) from err

    return resolve


def check_connection(env: dict) -> int:
    """Read-only call: confirms the token works and shows which account it controls."""
    user_id, token = env.get("IG_USER_ID"), env.get("IG_ACCESS_TOKEN")
    if not user_id or not token:
        raise PublishError("IG_USER_ID and IG_ACCESS_TOKEN must be set in publisher/.env")
    http = make_http(token, env.get("IG_API_BASE") or DEFAULT_API_BASE)
    info = http("GET", user_id, {"fields": "username,media_count"})
    log.info("Connected to @%s (%s posts)", info.get("username"), info.get("media_count"))
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Publish a post to Instagram")
    parser.add_argument("post_file", type=Path, nargs="?")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true", help="verify token without publishing")
    args = parser.parse_args(argv)

    env = {**load_env(Path(__file__).with_name(".env")), **os.environ}
    try:
        if args.check:
            return check_connection(env)
        if args.post_file is None:
            raise PublishError("post_file is required unless --check is used")
        post = load_post(args.post_file, resolve_file=None if args.dry_run else _make_uploader(env))
        if args.dry_run:
            publish(post, env.get("IG_USER_ID", "IG_USER_ID"), make_dry_run_http(), sleep=lambda _: None)
            return 0
        user_id, token = env.get("IG_USER_ID"), env.get("IG_ACCESS_TOKEN")
        if not user_id or not token:
            raise PublishError("IG_USER_ID and IG_ACCESS_TOKEN must be set in publisher/.env")
        publish(post, user_id, make_http(token, env.get("IG_API_BASE") or DEFAULT_API_BASE))
        return 0
    except PublishError as err:
        log.error("Publish failed: %s", err)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
