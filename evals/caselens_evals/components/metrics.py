"""Pure metric math for the component evals (no DB, no models, cheap to import and test).

* CER / WER: Levenshtein edit distance (rapidfuzz) over characters / word lists, divided by the
  reference length.
* Text normalization for a second, formatting-insensitive number (case, whitespace,
  punctuation, numbers spelled out).
* Diarization accuracy: best one-to-one speaker mapping over a confusion matrix.
* Box IoU, greedy IoU matching, VOC-style average precision.
* Pairwise clustering precision / recall / F1.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

Box = tuple[float, float, float, float]

# ------------------------------------------------------------------------------------------
# Error rates
# ------------------------------------------------------------------------------------------


def error_rate(reference: Sequence[Hashable], hypothesis: Sequence[Hashable]) -> float:
    """Edit distance / len(reference). Empty reference: 0.0 if hypothesis is empty, else 1.0."""
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return Levenshtein.distance(reference, hypothesis) / len(reference)


def cer(reference: str, hypothesis: str) -> float:
    return error_rate(reference, hypothesis)


def wer(reference: Sequence[str], hypothesis: Sequence[str]) -> float:
    return error_rate(list(reference), list(hypothesis))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_ocr(text: str) -> str:
    """Case- and whitespace-insensitive form: lowercase, runs of whitespace -> one space."""
    return collapse_ws(text).lower()


def strip_ws(text: str) -> str:
    """Lowercase with all whitespace removed (OCR word splitting / joining no longer counts)."""
    return re.sub(r"\s+", "", text).lower()


_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _under_100(n: int) -> list[str]:
    if n < 20:
        return [_ONES[n]]
    tens, ones = divmod(n, 10)
    return [_TENS[tens]] + ([_ONES[ones]] if ones else [])


def number_words(digits: str) -> list[str]:
    """Spoken form of a digit string, the way a narrator reads it.

    1-2 digits: cardinal ("42" -> forty two). 3 digits: "911" -> nine eleven, "200" -> two
    hundred. 4 digits: pairs ("1214" -> twelve fourteen, "1200" -> twelve hundred, "2026" ->
    twenty twenty six). Longer strings and leading zeros: digit by digit.
    """
    if not digits.isdigit():
        return [digits]
    if len(digits) > 4 or (len(digits) > 1 and digits.startswith("0")):
        return [_ONES[int(d)] for d in digits]
    n = int(digits)
    if n < 100:
        return _under_100(n)
    if len(digits) == 3:
        head, tail = divmod(n, 100)
        return [_ONES[head], "hundred"] if tail == 0 else [_ONES[head], *_tail_pair(tail)]
    head, tail = divmod(n, 100)
    if tail == 0:
        return (
            [*_under_100(head), "hundred"] if head % 10 else [*_under_100(head // 10), "thousand"]
        )
    return [*_under_100(head), *_tail_pair(tail)]


def _tail_pair(n: int) -> list[str]:
    return ["oh", _ONES[n]] if n < 10 else _under_100(n)


_ORDINALS = {
    "one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
    "nine": "ninth", "twelve": "twelfth",
}  # fmt: skip


def _ordinal(words: list[str]) -> list[str]:
    last = words[-1]
    if last in _ORDINALS:
        last = _ORDINALS[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return [*words[:-1], last]


_TOKEN = re.compile(r"\d+(?:st|nd|rd|th)?|[a-z]+(?:'[a-z]+)*")


def normalize_words(text: str, spell_numbers: bool = True) -> list[str]:
    """Lowercase word tokens without punctuation; digits spelled out when `spell_numbers`.

    Apostrophes inside words are dropped ("ma'am" -> maam) so "man's" == "mans"; hyphens and
    colons split ("2:10" -> two ten, "four-door" -> four door).
    """
    out: list[str] = []
    for tok in _TOKEN.findall(text.lower()):
        if tok[0].isdigit():
            digits = tok.rstrip("stndrh")
            if not spell_numbers:
                out.append(tok)
                continue
            words = number_words(digits)
            out.extend(_ordinal(words) if digits != tok else words)
        else:
            out.append(tok.replace("'", ""))
    return out


def raw_words(text: str) -> list[str]:
    """Lowercase, punctuation-stripped words; digits kept as written (the strict WER)."""
    return normalize_words(text, spell_numbers=False)


# ------------------------------------------------------------------------------------------
# Diarization
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeakerMapping:
    accuracy: float  # weight on the mapped diagonal / total weight
    mapping: dict[str, str]  # predicted speaker -> gold speaker
    total: float


def best_speaker_mapping(pairs: Iterable[tuple[str, str, float]]) -> SpeakerMapping:
    """Best one-to-one mapping of predicted to gold speakers.

    `pairs` = (gold speaker, predicted speaker, weight) per scored unit (a word, weighted by its
    duration or 1). Exhaustive over permutations for up to 8 speakers per side (always the case
    here), greedy on the largest cells beyond that.
    """
    matrix: dict[tuple[str, str], float] = defaultdict(float)
    for gold, pred, weight in pairs:
        matrix[(gold, pred)] += weight
    total = sum(matrix.values())
    golds = sorted({g for g, _ in matrix})
    preds = sorted({p for _, p in matrix})
    if not matrix:
        return SpeakerMapping(accuracy=0.0, mapping={}, total=0.0)
    best: dict[str, str] = {}
    best_score = -1.0
    if max(len(golds), len(preds)) <= 8:
        small, large, small_is_pred = (
            (preds, golds, True) if len(preds) <= len(golds) else (golds, preds, False)
        )
        for perm in itertools.permutations(large, len(small)):
            pairing = (
                {p: g for p, g in zip(small, perm, strict=True)}
                if small_is_pred
                else {p: g for g, p in zip(small, perm, strict=True)}
            )
            score = sum(matrix.get((g, p), 0.0) for p, g in pairing.items())
            if score > best_score:
                best_score, best = score, pairing
    else:
        used_g: set[str] = set()
        for (g, p), _w in sorted(matrix.items(), key=lambda kv: -kv[1]):
            if p not in best and g not in used_g:
                best[p] = g
                used_g.add(g)
        best_score = sum(matrix.get((g, p), 0.0) for p, g in best.items())
    return SpeakerMapping(accuracy=best_score / total if total else 0.0, mapping=best, total=total)


# ------------------------------------------------------------------------------------------
# Boxes
# ------------------------------------------------------------------------------------------


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def interval_iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


@dataclass(frozen=True)
class ScoredBox:
    image: str  # frame key: detections are only matched to labels of the same frame
    box: Box
    score: float = 1.0


def match_greedy(
    labels: Sequence[ScoredBox], preds: Sequence[ScoredBox], threshold: float = 0.5
) -> list[bool]:
    """Greedy matching in descending score order; returns per-prediction TP flags (same order
    as `preds`). Each label matches at most one prediction (COCO / VOC rule)."""
    taken: set[int] = set()
    flags = [False] * len(preds)
    for i in sorted(range(len(preds)), key=lambda k: -preds[k].score):
        best, best_iou = -1, threshold
        for j, lab in enumerate(labels):
            if j in taken or lab.image != preds[i].image:
                continue
            v = iou(lab.box, preds[i].box)
            if v >= best_iou:
                best, best_iou = j, v
        if best >= 0:
            taken.add(best)
            flags[i] = True
    return flags


def average_precision(
    labels: Sequence[ScoredBox], preds: Sequence[ScoredBox], threshold: float = 0.5
) -> float:
    """All-point interpolated AP (VOC 2010+) at an IoU threshold. 0.0 when there are no labels."""
    if not labels:
        return 0.0
    order = sorted(range(len(preds)), key=lambda k: -preds[k].score)
    flags = match_greedy(labels, preds, threshold)
    tp = fp = 0
    points: list[tuple[float, float]] = []  # (recall, precision)
    for i in order:
        if flags[i]:
            tp += 1
        else:
            fp += 1
        points.append((tp / len(labels), tp / (tp + fp)))
    ap, prev_recall = 0.0, 0.0
    for k, (recall, _) in enumerate(points):
        if recall > prev_recall:
            ap += (recall - prev_recall) * max(p for _, p in points[k:])
            prev_recall = recall
    return ap


# ------------------------------------------------------------------------------------------
# Clustering
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PairwiseScore:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def pairwise(gold: Mapping[str, str], pred: Mapping[str, str]) -> PairwiseScore:
    """Pairwise precision / recall over items present in both maps (item -> cluster label)."""
    items = sorted(set(gold) & set(pred))
    tp = fp = fn = 0
    for a, b in itertools.combinations(items, 2):
        same_gold, same_pred = gold[a] == gold[b], pred[a] == pred[b]
        tp += same_gold and same_pred
        fp += same_pred and not same_gold
        fn += same_gold and not same_pred
    return PairwiseScore(tp=tp, fp=fp, fn=fn)


def connected_components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> dict[str, str]:
    """node -> component label (the smallest node id in the component), via union-find."""
    parent: dict[str, str] = {n: n for n in nodes}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {n: find(n) for n in list(parent)}
