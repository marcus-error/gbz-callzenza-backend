"""
Call History v2 — Recording proxy service (ENABLE_CALL_HISTORY_V2).

Streams real audio with HTTP Range/206 support.
Provider URLs (Twilio CDN) are proxied server-side.
Storage paths are validated against the DB row — never trusted from client input.
No full-file buffering in memory.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import urllib.request
from pathlib import Path
from typing import AsyncGenerator, Optional, Tuple

import httpx
from loguru import logger

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "static" / "recordings"
CHUNK_SIZE = 64 * 1024  # 64 KB chunks


def _guess_content_type(url_or_path: str) -> str:
    if url_or_path.endswith(".mp3"):
        return "audio/mpeg"
    if url_or_path.endswith(".ogg"):
        return "audio/ogg"
    return "audio/wav"


def _validate_local_path(recording_url: str) -> Optional[Path]:
    """Resolve a /recordings/... URL to an absolute Path, reject path traversal."""
    if not recording_url.startswith("/recordings/"):
        return None
    fname = recording_url.split("/recordings/")[-1]
    # Block any path traversal attempts
    if ".." in fname or "/" in fname or "\\" in fname:
        logger.warning(f"[RECORDING_PROXY] Rejected path traversal attempt: {fname}")
        return None
    resolved = (RECORDINGS_DIR / fname).resolve()
    # Ensure it stays under RECORDINGS_DIR
    try:
        resolved.relative_to(RECORDINGS_DIR.resolve())
    except ValueError:
        logger.warning(f"[RECORDING_PROXY] Path escape detected: {resolved}")
        return None
    return resolved if resolved.exists() else None


async def stream_local_file(
    path: Path,
    range_start: int = 0,
    range_end: Optional[int] = None,
) -> Tuple[AsyncGenerator, int, int, int, str]:
    """
    Yields (generator, start, end, total_size, content_type).
    Caller must use the generator in a StreamingResponse.
    """
    file_size = path.stat().st_size
    content_type = _guess_content_type(path.name)
    start = max(0, range_start)
    end = min(file_size - 1, range_end) if range_end is not None else file_size - 1
    if start > end:
        start, end = 0, file_size - 1

    async def _gen():
        remaining = end - start + 1
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return _gen(), start, end, file_size, content_type


async def stream_twilio_url(
    twilio_url: str,
    range_start: int = 0,
    range_end: Optional[int] = None,
    twilio_sid: Optional[str] = None,
    twilio_token: Optional[str] = None,
) -> Tuple[AsyncGenerator, int, int, Optional[int], str]:
    """
    Proxy a Twilio recording URL to the client.
    Returns (generator, start, end, total_or_None, content_type).
    Provider credentials never reach the browser.
    """
    headers: dict = {}
    if range_start or range_end is not None:
        end_str = str(range_end) if range_end is not None else ""
        headers["Range"] = f"bytes={range_start}-{end_str}"

    auth = None
    if twilio_sid and twilio_token:
        auth = (twilio_sid, twilio_token)

    content_type = _guess_content_type(twilio_url)

    async def _gen():
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                async with client.stream(
                    "GET", twilio_url, headers=headers, auth=auth
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                        yield chunk
        except Exception as exc:
            logger.error(f"[RECORDING_PROXY] Twilio stream error: {exc}")
            return

    # Try to get content-length from HEAD
    total_size = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            head = await client.head(twilio_url, auth=auth)
            cl = head.headers.get("content-length")
            if cl:
                total_size = int(cl)
    except Exception:
        pass

    end = (total_size - 1) if total_size else (range_end or 0)
    return _gen(), range_start, end, total_size, content_type


def get_recording_source(recording_url: str) -> str:
    """Returns 'local', 'twilio', or 'none'."""
    if not recording_url:
        return "none"
    if recording_url.startswith("http"):
        return "twilio"
    if recording_url.startswith("/recordings/"):
        path = _validate_local_path(recording_url)
        return "local" if path else "none"
    return "none"
