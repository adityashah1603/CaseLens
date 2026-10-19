"""Fetch byte ranges of public MEVA clips (CC BY 4.0) into the local download cache.

MEVA clips are 5-minute H.264 AVI files of 60-250 MB. We only need a minute or two of each, so
we read the AVI ``idx1`` index, pick the video chunks for a keyframe-aligned frame range, and
write ``<AVI header up to movi data> + <those chunks>``. That file decodes like a truncated AVI
whose first frame is the requested keyframe. Small IR clips are fetched whole.

Every cached file is pinned by sha256 in ``sources.json``, so generation is reproducible.
"""

from __future__ import annotations

import struct
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from caselens_data_gen.common import sha256_file

MEVA_BASE_URL = "https://mevadata-public-01.s3.amazonaws.com/"
MEVA_LICENSE = "CC BY 4.0"
MEVA_ATTRIBUTION = (
    "Multiview Extended Video with Activities (MEVA) dataset, Kitware Inc. and IARPA DIVA "
    "program, https://mevadata.org, licensed CC BY 4.0. Footage shows consenting hired actors; "
    "relabeled to a fictional story by CaseLens data_gen."
)
_KEYFRAME_FLAG = 0x10
_HEADER_PROBE = 1 << 16
_TIMEOUT_S = 120


@dataclass(frozen=True)
class AviIndex:
    header: bytes
    """File bytes up to and including the ``movi`` fourcc (start of chunk data)."""
    chunks: list[tuple[int, int, int]]
    """Per video frame: (flags, absolute file offset of the chunk header, payload size)."""

    def is_keyframe(self, frame: int) -> bool:
        return bool(self.chunks[frame][0] & _KEYFRAME_FLAG)

    def byte_range(self, start_frame: int, end_frame: int) -> tuple[int, int]:
        """Inclusive byte range covering video chunks ``start_frame .. end_frame - 1``."""
        _, first, _ = self.chunks[start_frame]
        _, last, size = self.chunks[end_frame - 1]
        return first, last + 8 + size - 1


def meva_url(key: str) -> str:
    if not key.startswith("drops-") or ".." in key:
        raise ValueError(f"not a MEVA clip key: {key!r}")
    return MEVA_BASE_URL + key


def _get(key: str, start: int | None = None, end: int | None = None) -> bytes:
    request = urllib.request.Request(meva_url(key))  # noqa: S310 - fixed https base URL
    if start is not None and end is not None:
        request.add_header("Range", f"bytes={start}-{end}")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
        data: bytes = response.read()
    return data


def _object_size(key: str) -> int:
    request = urllib.request.Request(meva_url(key), method="HEAD")  # noqa: S310
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
        return int(response.headers["Content-Length"])


def _movi_list(head: bytes) -> tuple[int, int]:
    """Return (offset, size) of the top-level ``LIST movi`` chunk found in the header probe."""
    if head[:4] != b"RIFF" or head[8:12] != b"AVI ":
        raise ValueError("not a RIFF AVI file")
    pos = 12
    while pos + 12 <= len(head):
        chunk_id = head[pos : pos + 4]
        (size,) = struct.unpack("<I", head[pos + 4 : pos + 8])
        if chunk_id == b"LIST" and head[pos + 8 : pos + 12] == b"movi":
            return pos, size
        pos += 8 + size + (size & 1)
    raise ValueError("movi list not found in the header probe")


def parse_avi_index(head: bytes, idx1: bytes) -> AviIndex:
    """Parse the AVI header probe and the trailing ``idx1`` chunk into a frame index."""
    pos, _ = _movi_list(head)
    movi_fourcc = pos + 8
    if idx1[:4] != b"idx1":
        raise ValueError("idx1 chunk not found")
    (idx_size,) = struct.unpack("<I", idx1[4:8])
    entries = [
        struct.unpack("<4sIII", idx1[8 + 16 * i : 24 + 16 * i]) for i in range(idx_size // 16)
    ]
    video = [(flags, off, size) for cid, flags, off, size in entries if cid[2:] == b"dc"]
    if not video:
        raise ValueError("no video chunks in idx1")
    base = movi_fourcc if video[0][1] < movi_fourcc else 0
    return AviIndex(
        header=head[: movi_fourcc + 4],
        chunks=[(flags, base + off, size) for flags, off, size in video],
    )


def fetch_index(key: str) -> AviIndex:
    head = _get(key, 0, _HEADER_PROBE - 1)
    pos, size = _movi_list(head)
    idx1 = _get(key, pos + 8 + size, _object_size(key) - 1)
    return parse_avi_index(head, idx1)


def assemble_range(index: AviIndex, key: str, start_frame: int, end_frame: int) -> bytes:
    if not index.is_keyframe(start_frame):
        raise ValueError(f"{key}: frame {start_frame} is not a keyframe")
    first, last = index.byte_range(start_frame, end_frame)
    return index.header + _get(key, first, last)


def ensure_source(
    cache_dir: Path,
    name: str,
    key: str,
    sha256: str,
    frame_range: tuple[int, int] | None,
) -> Path:
    """Return the cached source file, downloading it first if missing; always verify sha256."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{name}.avi"
    if not path.exists():
        if frame_range is None:
            data = _get(key)
        else:
            data = assemble_range(fetch_index(key), key, *frame_range)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    actual = sha256_file(path)
    if actual != sha256:
        raise RuntimeError(
            f"{path}: sha256 {actual} does not match sources.json ({sha256}); delete it and retry"
        )
    return path
