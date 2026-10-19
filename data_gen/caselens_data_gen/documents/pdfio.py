"""Typed wrappers over the few PyMuPDF calls we use (several of its methods are unannotated)."""

from __future__ import annotations

from typing import cast

import pymupdf

Word = tuple[float, float, float, float, str]


def open_pdf(data: bytes) -> pymupdf.Document:
    return pymupdf.Document(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]


def new_pdf() -> pymupdf.Document:
    return pymupdf.Document()  # type: ignore[no-untyped-call]


def close(doc: pymupdf.Document) -> None:
    doc.close()  # type: ignore[no-untyped-call]


def page_words(page: pymupdf.Page) -> list[Word]:
    raw = cast(list[tuple[object, ...]], page.get_text("words", sort=False))  # type: ignore[no-untyped-call]
    return [
        (
            float(cast(float, w[0])),
            float(cast(float, w[1])),
            float(cast(float, w[2])),
            float(cast(float, w[3])),
            str(w[4]),
        )
        for w in raw
    ]


def page_text(page: pymupdf.Page, clip: tuple[float, float, float, float] | None = None) -> str:
    rect = pymupdf.Rect(*clip) if clip else None  # type: ignore[no-untyped-call]
    return str(page.get_text("text", clip=rect))  # type: ignore[no-untyped-call]


def insert_jpeg(page: pymupdf.Page, jpeg: bytes) -> None:
    page.insert_image(page.rect, stream=jpeg)  # type: ignore[no-untyped-call]


def to_bytes(doc: pymupdf.Document) -> bytes:
    return bytes(doc.tobytes(garbage=4, deflate=True, no_new_id=True))  # type: ignore[no-untyped-call]
