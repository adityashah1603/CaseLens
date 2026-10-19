"""Turn a digital SYNTHETIC PDF into a noisy, image-only "scanned" PDF (no text layer).

Every page is rasterised with PyMuPDF, degraded deterministically (contrast loss, coffee ring on
page 1, fold line, faint fax lines, skew, blur, sensor noise) and re-embedded as a JPEG. Facts are
located on the digital version first; ``map_bbox`` carries a bbox through the same skew so it lands
on the scanned page (approximate: the skew is a pure rotation about the page centre).
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pymupdf
from PIL import Image, ImageFilter

from caselens_data_gen.common import SYNTHETIC_NOTICE
from caselens_data_gen.documents.pdfio import close, insert_jpeg, new_pdf, open_pdf, to_bytes

SCAN_DPI = 150
_SCALE = SCAN_DPI / 72.0


@dataclass(frozen=True)
class ScanResult:
    pdf: bytes
    angles: list[float]  # degrees counter-clockwise, per page
    page_sizes: list[tuple[float, float]]  # points


def _seed(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")


def page_angle(index: int) -> float:
    """Skew per page: 2.5 deg on page 1 (per the story's rendering note), 1-3 deg after."""
    return 2.5 if index == 0 else (-1.4, 1.8, -2.2)[(index - 1) % 3]


def _degrade(
    gray: npt.NDArray[np.uint8], index: int, rng: np.random.Generator
) -> npt.NDArray[np.uint8]:
    h, w = gray.shape
    img = gray.astype(np.float32)
    img = 34.0 + img * (200.0 / 255.0)  # contrast loss: black -> 34, white -> 234
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img -= 6.0 * (xx / w)  # uneven scanner lamp
    if index == 0:  # coffee ring, upper right
        cx, cy, radius = w * 0.78, h * 0.2, w * 0.085
        dist = np.sqrt((xx - cx) ** 2 + ((yy - cy) * 1.08) ** 2)
        theta = np.arctan2(yy - cy, xx - cx)
        strength = 0.6 + 0.4 * np.sin(theta * 3.0 + 0.7)
        ring = np.exp(-(((dist - radius) / (w * 0.006)) ** 2)) * 46.0 * strength
        inner = (dist < radius).astype(np.float32) * 9.0
        img -= ring + inner
    fold_y = h * 0.5 + rng.uniform(-8, 8)  # fold line from being mailed / folded
    img -= np.exp(-(((yy - fold_y) / 2.2) ** 2)) * 38.0
    img -= np.exp(-(((yy - fold_y) / 30.0) ** 2)) * 7.0
    for row in rng.integers(0, h, size=5):  # faint fax lines
        img[int(row) : int(row) + 2, :] -= 16.0
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), mode="L")
    out = out.rotate(
        page_angle(index), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=198
    )
    out = out.filter(ImageFilter.GaussianBlur(radius=0.9))
    arr = np.asarray(out, dtype=np.float32)
    arr += rng.normal(0.0, 7.5, size=arr.shape).astype(np.float32)
    specks = rng.random(arr.shape) < 0.0009
    arr[specks] = rng.uniform(20, 90, size=int(specks.sum()))
    return np.clip(arr, 0, 255).astype(np.uint8)


def scan_pdf(digital: bytes, seed_key: str) -> ScanResult:
    """Return an image-only scanned rendition of ``digital`` (deterministic for a seed key)."""
    rng = np.random.default_rng(_seed(seed_key))
    src = open_pdf(digital)
    out = new_pdf()
    angles: list[float] = []
    sizes: list[tuple[float, float]] = []
    for index in range(src.page_count):
        page = src[index]
        pix = page.get_pixmap(dpi=SCAN_DPI, colorspace=pymupdf.csGRAY, alpha=False)
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        degraded = _degrade(gray, index, rng)
        buf = io.BytesIO()
        Image.fromarray(degraded, mode="L").save(buf, format="JPEG", quality=72, dpi=(150, 150))
        width, height = page.rect.width, page.rect.height
        new = out.new_page(width=width, height=height)
        insert_jpeg(new, buf.getvalue())
        angles.append(page_angle(index))
        sizes.append((width, height))
    out.set_metadata(
        {
            "title": "Scanned statement (SYNTHETIC)",
            "author": "CaseLens data_gen",
            "subject": SYNTHETIC_NOTICE,
            "keywords": "SYNTHETIC",
            "creator": "CaseLens data_gen scanner simulation",
            "producer": "CaseLens data_gen",
            "creationDate": "D:20260301000000Z",
            "modDate": "D:20260301000000Z",
        }
    )
    data = to_bytes(out)
    close(src)
    close(out)
    return ScanResult(pdf=data, angles=angles, page_sizes=sizes)


def map_bbox(
    bbox: tuple[float, float, float, float], angle_deg: float, page_size: tuple[float, float]
) -> tuple[float, float, float, float]:
    """Carry a digital-page bbox (points) through the scan rotation; returns an axis-aligned "
    "bbox."""
    width, height = page_size
    cx, cy = width * _SCALE / 2.0, height * _SCALE / 2.0
    rad = math.radians(angle_deg)
    cos, sin = math.cos(rad), math.sin(rad)
    xs: list[float] = []
    ys: list[float] = []
    x0, y0, x1, y1 = bbox
    for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        dx, dy = px * _SCALE - cx, py * _SCALE - cy
        xs.append((cx + dx * cos + dy * sin) / _SCALE)
        ys.append((cy - dx * sin + dy * cos) / _SCALE)
    return (
        round(max(0.0, min(xs)), 1),
        round(max(0.0, min(ys)), 1),
        round(min(width, max(xs)), 1),
        round(min(height, max(ys)), 1),
    )
