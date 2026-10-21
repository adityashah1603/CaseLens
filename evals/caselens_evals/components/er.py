"""Entity-resolution precision / recall against the story's people, vehicles, phones and places.

Gold clusters: each DB entity is assigned to at most one story entity by rules that use only the
ground truth and the entity's own surface forms (canonical name, `attributes.aliases`,
`normalized_key`, `attributes.plate/make/model`), never the predicted links:

* phone: normalized number equals a story phone.
* vehicle: plate (normalized_key or attributes.plate) equals a story plate, a story plate-read
  text (misreads such as SYN482I map to the true vehicle), or is a 4+ character prefix of exactly
  one story plate; else a name equals a story vehicle alias; else make+model (or a unique model)
  equal one story vehicle's.
* person: a name equals a story name/alias; else full-name similarity >= 0.8 (OCR/ASR typos such
  as "Elina Ruiz", "Geraid Pike"), or same first name and similarity >= 0.7 ("Rory Vats"); else a
  single-token name equal to a story person's surname or first name that is unique in the story.
* location: a name equals a story name/alias or the address before its first comma (after
  rd->road, ct->court, ave->avenue); else the entity name contains a multi-word story name.

Entities matching two story entities are ambiguous and left out. Predicted clusters: connected
components of active entity_links. Pairwise precision / recall / F1 over pairs of mapped entities.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz.distance import Levenshtein

from caselens_data_gen.story import Story
from caselens_evals.components.metrics import PairwiseScore, connected_components, pairwise
from caselens_evals.components.sources import CaseSource
from caselens_workers.resolve import normalize_name, normalize_phone, normalize_plate

_ABBREV = {"rd": "road", "ct": "court", "ave": "avenue", "st": "street", "grey": "gray"}


def norm(text: str) -> str | None:
    base = normalize_name(text)
    if base is None:
        return None
    return " ".join(_ABBREV.get(w, w) for w in base.split())


def similarity(a: str, b: str) -> float:
    return float(Levenshtein.normalized_similarity(a, b))


@dataclass
class StoryIndex:
    """Lookup tables built from the story (ground truth) only."""

    names: dict[str, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(dict))
    full_names: dict[str, str] = field(default_factory=dict)  # person id -> normalized name
    tokens: dict[str, str] = field(default_factory=dict)  # unique first/last name -> person id
    plates: dict[str, str] = field(default_factory=dict)
    make_model: dict[tuple[str, str], str] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    phones: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # story id -> display name

    def add_name(self, etype: str, sid: str, raw: str) -> None:
        if (n := norm(raw)) is not None:
            self.names[etype].setdefault(n, set()).add(sid)


def build_index(story: Story) -> StoryIndex:
    ix = StoryIndex()
    token_owners: dict[str, set[str]] = defaultdict(set)
    for p in story.people:
        ix.labels[p.id] = p.name
        for raw in (p.name, *p.aliases):
            ix.add_name("person", p.id, raw)
        full = norm(p.name) or p.id
        ix.full_names[p.id] = full
        parts = full.split()
        for tok in {parts[0], parts[-1]}:
            token_owners[tok].add(p.id)
    ix.tokens = {t: next(iter(o)) for t, o in token_owners.items() if len(o) == 1}
    for v in story.vehicles:
        ix.labels[v.id] = f"{v.color} {v.make} {v.model} {v.plate}"
        for raw in v.aliases:
            ix.add_name("vehicle", v.id, raw)
        if (plate := normalize_plate(v.plate)) is not None:
            ix.plates[plate] = v.id
        ix.make_model[(v.make.lower(), v.model.lower())] = v.id
        ix.models[v.model.lower()] = v.id
    for r in story.plate_reads:
        if r.vehicle_id and (plate := normalize_plate(r.plate)) is not None:
            ix.plates.setdefault(plate, r.vehicle_id)
    for ph in story.phones:
        ix.labels[ph.id] = ph.number
        if (num := normalize_phone(ph.number)) is not None:
            ix.phones[num] = ph.id
    for loc in story.locations:
        ix.labels[loc.id] = loc.name
        for raw in (loc.name, *loc.aliases, loc.address.split(",")[0]):
            ix.add_name("location", loc.id, raw)
    return ix


def _surface(name: str, attributes: dict[str, Any]) -> list[str]:
    aliases = attributes.get("aliases") or []
    return [n for raw in (name, *aliases) if isinstance(raw, str) and (n := norm(raw))]


def _one(found: set[str]) -> str | None:
    """The single story id, None for no match; raises nothing - ambiguity is reported as '?'."""
    if len(found) == 1:
        return next(iter(found))
    return "?" if found else None


def assign(
    ix: StoryIndex,
    etype: str,
    name: str,
    key: str | None,
    attributes: dict[str, Any],
    story: Story,
) -> str | None:
    """Story id for one DB entity, None when it is no story entity, '?' when ambiguous."""
    names = _surface(name, attributes)
    exact = {sid for n in names for sid in ix.names[etype].get(n, set())}
    if etype == "phone":
        num = normalize_phone(key or name)
        return ix.phones.get(num) if num else None
    if etype == "vehicle":
        plates = {
            p
            for raw in (key, attributes.get("plate"), name)
            if isinstance(raw, str)
            and (p := normalize_plate(re.sub(r"\(.*?\)", "", raw).strip())) is not None
        }
        hits = {ix.plates[p] for p in plates if p in ix.plates}
        if not hits:
            for p in (p for p in plates if len(p) >= 4):
                owners = {sid for sp, sid in ix.plates.items() if sp.startswith(p)}
                if len(owners) == 1:
                    hits |= owners
        if hits:
            return _one(hits)
        if exact:
            return _one(exact)
        make = str(attributes.get("make", "")).lower()
        model = str(attributes.get("model", "")).lower()
        if (make, model) in ix.make_model:
            return ix.make_model[(make, model)]
        return ix.models.get(model)
    if etype == "person":
        if exact:
            return _one(exact)
        fuzzy: set[str] = set()
        for n in names:
            for pid, full in ix.full_names.items():
                sim = similarity(n, full)
                same_first = n.split()[0] == full.split()[0] and len(n.split()) > 1
                if sim >= 0.8 or (same_first and sim >= 0.7):
                    fuzzy.add(pid)
        if fuzzy:
            return _one(fuzzy)
        single = {ix.tokens[n] for n in names if " " not in n and n in ix.tokens}
        return _one(single)
    if etype == "location":
        if exact:
            return _one(exact)
        contained = {
            loc.id
            for loc in story.locations
            if (ln := norm(loc.name)) and " " in ln
            for n in names
            if re.search(rf"\b{re.escape(ln)}\b", n)
        }
        return _one(contained)
    return None


def evaluate(src: CaseSource, story: Story, worst: int = 8) -> dict[str, Any]:
    ix = build_index(story)
    entities = src.entities()
    info = {eid: (etype, name) for eid, etype, name, _, _ in entities}
    gold: dict[str, str] = {}
    ambiguous: list[str] = []
    for eid, etype, name, key, attrs in entities:
        sid = assign(ix, etype, name, key, attrs, story)
        if sid == "?":
            ambiguous.append(f"{etype}: {name}")
        elif sid is not None:
            gold[eid] = sid
    links = src.active_links()
    comp = connected_components(info, [(a, b) for a, b, _ in links])

    def label(eid: str) -> str:
        return f"{info[eid][1]} [{gold.get(eid, '-')}]"

    scores: dict[str, PairwiseScore] = {"all": pairwise(gold, comp)}
    for etype in ("person", "vehicle", "phone", "location"):
        sub = {e: s for e, s in gold.items() if info[e][0] == etype}
        scores[etype] = pairwise(sub, comp)

    # Worst false merges: predicted components that join several story entities.
    members: dict[str, list[str]] = defaultdict(list)
    for eid, c in comp.items():
        members[c].append(eid)
    false_merges = []
    for eids in members.values():
        mapped = [e for e in eids if e in gold]
        wrong = sum(1 for a, b in itertools.combinations(mapped, 2) if gold[a] != gold[b])
        if wrong:
            false_merges.append({"wrong_pairs": wrong, "component": sorted(label(e) for e in eids)})
    false_merges.sort(key=lambda m: -int(m["wrong_pairs"]))  # type: ignore[call-overload]
    # Story-entity merges with entities that match no story entity (informational).
    with_unmapped = [
        sorted(label(e) for e in eids)
        for eids in members.values()
        if len(eids) > 1 and any(e in gold for e in eids) and any(e not in gold for e in eids)
    ]
    # Missed merges: story entities split over several predicted components.
    by_story: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for eid, sid in gold.items():
        by_story[sid][comp[eid]].append(info[eid][1])
    missed = []
    for sid, comps in by_story.items():
        if len(comps) > 1:
            sizes = [len(v) for v in comps.values()]
            pairs = (sum(sizes) ** 2 - sum(s * s for s in sizes)) // 2
            missed.append(
                {
                    "story_entity": f"{sid} ({ix.labels.get(sid, sid)})",
                    "missed_pairs": pairs,
                    "components": sorted(sorted(v) for v in comps.values()),
                }
            )
    missed.sort(key=lambda m: -int(m["missed_pairs"]))  # type: ignore[call-overload]
    covered = {sid for sid in gold.values()}
    story_ids = (
        [p.id for p in story.people]
        + [v.id for v in story.vehicles]
        + [p.id for p in story.phones]
        + [loc.id for loc in story.locations]
    )
    return {
        "pairwise": {
            k: {
                "precision": round(v.precision, 4) if v.tp + v.fp else None,
                "recall": round(v.recall, 4) if v.tp + v.fn else None,
                "f1": round(v.f1, 4) if v.tp + v.fp and v.tp + v.fn else None,
                "tp": v.tp,
                "fp": v.fp,
                "fn": v.fn,
            }
            for k, v in scores.items()
        },
        "entities": len(entities),
        "mapped_entities": len(gold),
        "active_links": len(links),
        "story_entities_found": f"{len(covered & set(story_ids))}/{len(story_ids)}",
        "story_entities_missing": sorted(set(story_ids) - covered),
        "ambiguous": ambiguous,
        "worst_false_merges": false_merges[:worst],
        "story_merged_with_unmapped": with_unmapped[:worst],
        "worst_missed_merges": missed[:worst],
        "method": __doc__,
    }
