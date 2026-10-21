"""Render the component-eval results as a markdown report."""

from __future__ import annotations

from typing import Any


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def summary_rows(results: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if ocr := results.get("ocr"):
        t = ocr["total"]
        rows += [
            ("OCR ws_pike.pdf", "CER raw (whole pages)", _fmt(t["cer_raw"])),
            ("OCR ws_pike.pdf", "CER case/whitespace-normalized", _fmt(t["cer_normalized"])),
            ("OCR ws_pike.pdf", "CER ignoring whitespace", _fmt(t["cer_no_whitespace"])),
            ("OCR ws_pike.pdf", "CER narrative body, order-free", _fmt(t["cer_body_order_free"])),
        ]
    if asr := results.get("asr"):
        for clip, e in asr["clips"].items():
            rows += [
                (f"ASR {clip}", "WER raw (chunks)", _fmt(e["chunks"]["wer_raw"])),
                (f"ASR {clip}", "WER normalized (chunks)", _fmt(e["chunks"]["wer_normalized"])),
                (f"ASR {clip}", "diarization accuracy", _fmt(e["diarization"]["accuracy"])),
            ]
    if vid := results.get("video"):
        for cls, d in vid["detection"].items():
            rows += [
                (f"Detection {cls}", "recall @IoU0.5", _fmt(d["recall"])),
                (
                    f"Detection {cls}",
                    "precision @IoU0.5 (lower bound)",
                    _fmt(d["precision_lower_bound"]),
                ),
                (f"Detection {cls}", "AP@0.5 (lower bound)", _fmt(d["ap50_lower_bound"])),
            ]
        rows.append(("Detection", "mAP@0.5 (lower bound)", _fmt(vid["map50_lower_bound"])))
        for cls, d in vid["tracking"]["by_class"].items():
            rows.append((f"Tracking {cls}", "labeled tracks found", f"{d['found']}/{d['labels']}"))
    if er := results.get("er"):
        for k, d in er["pairwise"].items():
            rows.append(
                (
                    f"ER {k}",
                    "pairwise P / R / F1",
                    f"{_fmt(d['precision'])} / {_fmt(d['recall'])} / {_fmt(d['f1'])}"
                    f" (tp {d['tp']}, fp {d['fp']}, fn {d['fn']})",
                )
            )
    return rows


def render(results: dict[str, Any]) -> str:
    lines = [
        "# Component evals (SYNTHETIC case)",
        "",
        f"Case: {results['case']} - run {results['run_at']}",
        "",
        "| Component | Metric | Value |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in summary_rows(results)]
    for name in ("ocr", "asr", "video", "er"):
        if name in results:
            method = str(results[name].get("method", "")).strip()
            lines += ["", f"## {name.upper()}: how it was measured", "", method]
        if name in results.get("errors", {}):
            lines += ["", f"## {name.upper()}: FAILED", "", results["errors"][name]]
    if vid := results.get("video"):
        lines += [
            "",
            "### Labeled tracks",
            "",
            "| Clip | Label track | Class | Found | Pred track | IoU | Temporal IoU |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for t in vid["tracking"]["tracks"]:
            lines.append(
                f"| {t['clip']} | {t['label_track']} | {t['class']} | {t['found']} | "
                f"{t.get('pred_track', '-')} | {_fmt(t['best_iou'])} | "
                f"{_fmt(t.get('temporal_iou'))} |"
            )
    if er := results.get("er"):
        lines += ["", "### Worst false merges", ""]
        lines += [
            f"- {m['wrong_pairs']} wrong pairs: {', '.join(m['component'])}"
            for m in er["worst_false_merges"]
        ] or ["- none"]
        lines += ["", "### Story entities merged with non-story entities", ""]
        lines += [f"- {', '.join(c)}" for c in er["story_merged_with_unmapped"]] or ["- none"]
        lines += ["", "### Worst missed merges", ""]
        for m in er["worst_missed_merges"]:
            parts = " | ".join(", ".join(c) for c in m["components"])
            lines.append(
                f"- {m['story_entity']}: {m['missed_pairs']} missed pairs; split as {parts}"
            )
        if not er["worst_missed_merges"]:
            lines.append("- none")
    lines.append("")
    return "\n".join(lines)
