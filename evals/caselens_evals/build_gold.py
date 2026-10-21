"""Build the 50-question gold set for the SYNTHETIC demo case "The Riverside Lot Homicide".

    python -m caselens_evals.build_gold                 # rebuild from story + answer key + cache
    python -m caselens_evals.build_gold --check         # fail if the committed file is stale
    python -m caselens_evals.build_gold --refresh-sql --case-id <uuid> --user-id <uuid>

Every item is built from story ids (`source_ids`) and answer-key facts: each required citation is
an answer-key locator, looked up by fact id and evidence file, so it is a fact that is actually
located in the generated evidence. The prose (questions, answers, key facts) is written here from
those facts. Structured items name the semantic-layer operation and params; their expected rows
are derived from the story (calls, plate reads, custody items) and their result is computed by
running `caselens_semantic_layer.run_operation` on the dev DB as `caselens_readonly` with
`app.user_id` set (`--refresh-sql`, read-only). Without `--refresh-sql` the expected results are
taken from the committed JSONL, so a rebuild needs no database and is byte-identical.

Ground truth is used here only, under evals/; it never enters the app.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from caselens_data_gen.story import Story, load_story
from caselens_evals.gold import (
    MODALITY_ORDER,
    Category,
    ConflictSpec,
    GoldError,
    GoldItem,
    Operation,
    RecordLocator,
    RequiredCitation,
    SqlExpected,
    SqlSpec,
    check_mix,
    load_gold,
)

STORY_SLUG = "riverside_lot"
REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "data_gen" / "output" / STORY_SLUG
ANSWER_KEY_PATH = DATA_DIR / "answer_key.json"
MANIFEST_PATH = DATA_DIR / "manifest.json"
GOLD_PATH = REPO / "evals" / "gold" / f"{STORY_SLUG}.jsonl"

CiteRef = tuple[str, str] | tuple[str, str, int]
"""(fact_id, evidence basename) or (fact_id, basename, disambiguator): the disambiguator is the
pdf page, the audio/video start_ms or the record row_id, for facts located twice in one file."""


@dataclass(frozen=True)
class SqlPlan:
    operation: Operation
    params: dict[str, str]


@dataclass(frozen=True)
class Spec:
    id: str
    category: Category
    question: str
    answer: str
    key_facts: list[str]
    cites: list[CiteRef]
    source_ids: list[str]
    conflict: tuple[str, str, str] | None = None  # (cf id, side a, side b)
    sql: SqlPlan | None = None


@dataclass
class Truth:
    story: Story
    facts: list[dict[str, Any]]
    manifest: set[str]
    story_ids: set[str] = field(default_factory=set)


def load_truth() -> Truth:
    story = load_story(STORY_SLUG)
    facts = json.loads(ANSWER_KEY_PATH.read_text(encoding="utf-8"))["facts"]
    manifest = {f["path"] for f in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["files"]}
    ids: set[str] = set()
    for group in (
        story.people, story.vehicles, story.phones, story.locations, story.facilities,
        story.cameras, story.events, story.conflicts, story.calls, story.plate_reads,
        story.evidence_plan, story.unanswerable,
    ):  # fmt: skip
        ids.update(x.id for x in group)
    ids.update(i.item_id for i in story.evidence_items)
    return Truth(story=story, facts=facts, manifest=manifest, story_ids=ids)


def _disambiguator(locator: Mapping[str, Any]) -> int:
    kind = locator["kind"]
    if kind == "pdf":
        return int(locator["page"])
    if kind == "record":
        return int(locator["row_id"])
    return int(locator["start_ms"])


def resolve_cite(truth: Truth, ref: CiteRef) -> RequiredCitation:
    fact_id, basename = ref[0], ref[1]
    hits = [
        f
        for f in truth.facts
        if f["fact_id"] == fact_id and PurePosixPath(f["evidence"]).name == basename
    ]
    if len(ref) == 3:
        hits = [f for f in hits if _disambiguator(f["locator"]) == ref[2]]
    if len(hits) != 1:
        raise GoldError(f"citation {ref}: {len(hits)} answer-key facts match (need exactly 1)")
    hit = hits[0]
    if hit["evidence"] not in truth.manifest:
        raise GoldError(f"citation {ref}: {hit['evidence']} is not in the manifest")
    return RequiredCitation.model_validate(
        {"fact_id": hit["fact_id"], "evidence": hit["evidence"], "locator": hit["locator"]},
        strict=False,
    )


# --------------------------------------------------------------------------------------------
# Structured items: expected story rows, derived from the story records.

KEMPER = "+13605550117"
HALE = "+13605550142"
VANCE_PREPAID = "+13605550188"
WEEK_START = "2026-03-07T22:44:00-08:00"  # 7 days before the shooting (PST; DST began 03-08)
SHOOTING = "2026-03-14T22:44:00-07:00"


def _in_window(t: datetime, params: Mapping[str, str]) -> bool:
    start = datetime.fromisoformat(params["window_start"])
    end = datetime.fromisoformat(params["window_end"])
    return start <= t <= end


def story_rows(truth: Truth, plan: SqlPlan) -> list[str]:
    """Story ids (call / plate read / item ids) the operation must return, in result order."""
    s, p = truth.story, plan.params
    number = {ph.id: ph.number for ph in s.phones}
    op = plan.operation
    if op in ("calls_between", "call_count_between", "calls_for_phone"):
        out = []
        for c in sorted(s.calls, key=lambda c: c.started_at):
            ends = {number[c.caller_phone_id], number[c.callee_phone_id]}
            if op == "calls_for_phone":
                hit = p["phone"] in ends
            else:
                hit = ends == {p["phone_a"], p["phone_b"]}
            if hit and _in_window(c.started_at, p):
                out.append(c.id)
        return out
    if op in ("plate_sightings", "plate_sightings_near"):
        camera = {c.id: c.name for c in s.cameras}
        out = []
        for r in sorted(s.plate_reads, key=lambda r: r.read_at):
            if op == "plate_sightings":
                hit = r.plate == p["plate"]
            else:
                hit = p["location_name"].casefold() in camera[r.camera_id].casefold()
            if hit and _in_window(r.read_at, p):
                out.append(r.id)
        return out
    if op == "custody_chain":
        return [i.item_id for i in s.evidence_items if i.item_id == p["item_id"]]
    raise GoldError(f"unknown operation {op}")


def _record_cites(truth: Truth, plan: SqlPlan) -> list[CiteRef]:
    basename = {
        "call": "call_detail_records.csv",
        "pr": "plate_reader_hits.csv",
        "ITEM": "evidence_log.csv",
    }
    refs: list[CiteRef] = []
    for sid in story_rows(truth, plan):
        refs.append((sid, basename[sid.split("_")[0].split("-")[0]]))
    if not refs:
        raise GoldError(f"{plan.operation} {plan.params}: no story rows")
    return refs


# --------------------------------------------------------------------------------------------
# The 50 questions.


def specs() -> list[Spec]:
    single = [
        Spec(
            "q01",
            "single_source",
            "What time did the first officer arrive at the Riverside Lot, and who was it?",
            "Officer Priya Nandakumar (PCPD patrol unit 2-14) arrived on scene at 22:53:10 on "
            "03/14/2026.",
            ["Officer Priya Nandakumar was first on scene", "She arrived at 22:53:10"],
            [("ev_24", "rpt_incident.pdf")],
            ["ev_24", "o_patrol"],
        ),
        Spec(
            "q02",
            "single_source",
            "Who is the victim, and how old was he?",
            "The victim is Marcus Hale, age 41, identified from the driver license in his wallet.",
            ["The victim is Marcus Hale", "He was 41"],
            [("p_victim", "rpt_incident.pdf")],
            ["p_victim", "rpt_incident"],
        ),
        Spec(
            "q03",
            "single_source",
            "Where did the gray sedan park after it pulled into the Riverside Lot?",
            "Per the CAM-A review, the gray sedan entered at 22:41:05 and parked in space C-7, "
            "beside the victim's pickup in C-6.",
            ["The gray sedan parked in space C-7", "C-7 is beside the victim's pickup"],
            [("ev_18", "rpt_supp_01.pdf")],
            ["ev_18", "v_gray_sedan"],
        ),
        Spec(
            "q04",
            "single_source",
            "What does the ALPR export show for plate SYN482I?",
            "One read of SYN482I at Route 9 at Marsh Rd, eastbound, at 22:24:39 on 03/14 with "
            "confidence 0.58: a low-confidence duplicate misread (1 read as I) of the gray "
            "Civic SYN4821, read two seconds earlier.",
            [
                "SYN482I was read at Route 9 at Marsh Rd at 22:24:39",
                "The read has low confidence (0.58)",
                "It is a misread of SYN4821",
            ],
            [("pr_06", "plate_reader_hits.csv")],
            ["pr_06", "v_gray_sedan"],
        ),
        Spec(
            "q05",
            "single_source",
            "Who is the subscriber on the prepaid number +13605550188 in the carrier return?",
            "The carrier return lists +13605550188 as a TideLine Prepaid number with a blank "
            "subscriber field: the subscriber is unknown in the carrier records.",
            ["+13605550188 is a prepaid number", "The subscriber field is blank"],
            [("ph_associate", "rpt_supp_02.pdf")],
            ["ph_associate", "rpt_supp_02"],
        ),
        Spec(
            "q06",
            "single_source",
            "When and where was Travis Kemper arrested?",
            "Travis Kemper was arrested in unit 14 of the Larkspur Court Apartments at 10:20 on "
            "03/16/2026, during the search warrant.",
            [
                "Kemper was arrested at 10:20 on 03/16",
                "He was arrested in unit 14 at Larkspur Court",
            ],
            [("p_suspect", "rpt_supp_03.pdf")],
            ["p_suspect", "ev_29", "loc_kemper_apartment"],
        ),
        Spec(
            "q07",
            "single_source",
            "What did the Civic's passenger buy at Calder Fuel & Go, and how did he pay?",
            "The passenger paid $14.62 cash for two Volt Surge energy drinks and a pack of "
            "Halyard cigarettes at the register at 22:30:58.",
            [
                "He paid $14.62 in cash",
                "He bought two Volt Surge energy drinks",
                "He bought a pack of Halyard cigarettes",
            ],
            [("ev_16", "rpt_supp_03.pdf")],
            ["ev_16", "p_associate"],
        ),
        Spec(
            "q08",
            "single_source",
            "According to Elena Ruiz, who got out of the gray car and what did they do?",
            "Two men got out: the driver, in a dark hoodie with the hood up, walked over to the "
            "man at the back of the white truck; the other, tall with a light cap, stayed by the "
            "back of the car. It looked like they were arguing.",
            [
                "Two men got out of the gray car",
                "The driver wore a dark hoodie and walked to the man at the white truck",
                "The tall one with a light cap stayed by the car",
            ],
            [("ev_19", "ws_ruiz.pdf")],
            ["ev_19", "p_witness_ruiz"],
        ),
        Spec(
            "q09",
            "single_source",
            "What did Gerald Pike hear the men say just before the shot?",
            'Pike heard a man yell "You\'ll get it when I have it!" and the other answer '
            '"Then I\'m done protecting you," then one loud bang.',
            [
                '"You\'ll get it when I have it!"',
                '"Then I\'m done protecting you"',
                "Then one loud bang",
            ],
            [("ev_19", "ws_pike.pdf"), ("ev_20", "ws_pike.pdf")],
            ["ev_19", "ev_20", "p_witness_pike", "ws_pike"],
        ),
        Spec(
            "q10",
            "single_source",
            "Did the dog walker get any part of the fleeing car's plate?",
            "Yes. Gerald Pike saw a gray or silver four-door leave with no headlights; the plate "
            'started "SYN 48" and he did not get the rest.',
            ["The plate started SYN 48", "He did not get the rest of the plate"],
            [("v_gray_sedan", "ws_pike.pdf")],
            ["v_gray_sedan", "p_witness_pike", "ws_pike"],
        ),
        Spec(
            "q11",
            "single_source",
            "Where was Dana Whitfield that night, and how did she leave the area?",
            "She was waiting at the Route 9 bus shelter on the side of the Riverside Lot from a "
            "little after 10 PM, and left on her bus (due at 10:21).",
            ["She waited at the bus shelter beside the Riverside Lot", "She got on her bus"],
            [("loc_riverside_lot", "ws_whitfield.pdf"), ("ev_13", "ws_whitfield.pdf")],
            ["ev_13", "p_witness_whitfield", "loc_riverside_lot"],
        ),
        Spec(
            "q12",
            "single_source",
            "What callback number did the 911 caller give the dispatcher?",
            "She gave her cell number, (360) 555-0129 (+13605550129).",
            ["The callback number is (360) 555-0129", "It is her cell phone"],
            [("ph_witness_ruiz", "aud_911.wav")],
            ["ph_witness_ruiz", "aud_911"],
        ),
        Spec(
            "q13",
            "single_source",
            "How did the 911 caller describe the car that left, and which way did it go?",
            '"A gray car, like a Civic"; it had just left and went right on Calder River Road.',
            ["A gray car like a Civic", "It went right on Calder River Road"],
            [("v_gray_sedan", "aud_911.wav")],
            ["v_gray_sedan", "aud_911"],
        ),
        Spec(
            "q14",
            "single_source",
            "In his interview, what did Kemper say he and Hale arranged on the Friday call?",
            "Kemper said he told Hale he could bring him part of the money, and they agreed on "
            "Saturday night at the river lot, around 10:30.",
            [
                "Kemper offered to bring part of the money",
                "They agreed to meet Saturday night at the river lot around 10:30",
            ],
            [("ev_03", "aud_interview.wav")],
            ["ev_03", "p_suspect", "aud_interview"],
        ),
        Spec(
            "q15",
            "single_source",
            "When does the gray sedan pull in at Calder Fuel & Go on the station's CCTV?",
            "On the Fuel & Go CCTV the gray sedan pulls in and stops at about 22:25:10-22:25:20.",
            ["The gray sedan pulls in at about 22:25"],
            [("ev_14", "vid_04_fuel_stop.mp4")],
            ["ev_14", "v_gray_sedan", "vid_04_fuel_stop"],
        ),
    ]
    multi = [
        Spec(
            "q16",
            "multi_source",
            "Who was in the gray sedan on March 14?",
            "Two men: the driver, Travis Kemper, whose gray 2016 Honda Civic SYN4821 it is, and "
            "the passenger, his cousin Rory Vance. CAM-A shows both occupants get out at the lot.",
            [
                "Travis Kemper was the driver",
                "The car is Kemper's gray Civic SYN4821",
                "Rory Vance was the passenger",
                "Two occupants got out at the lot",
            ],
            [
                ("v_gray_sedan", "rpt_supp_02.pdf"),
                ("p_associate", "rpt_supp_03.pdf"),
                ("ev_19", "vid_02_market_lot_shooting.mp4"),
            ],
            ["v_gray_sedan", "p_suspect", "p_associate", "ev_19"],
        ),
        Spec(
            "q17",
            "multi_source",
            "Where was the gray sedan between 10 and 11 PM on March 14?",
            "Read eastbound at Route 9/Marsh at 22:24:37 and stopped at Calder Fuel & Go "
            "(~22:25); read westbound at Route 9/Marsh at 22:36:20; entered the Riverside Lot at "
            "22:41:05 (ALPR and CAM-A) and left at 22:47:31.",
            [
                "22:24:37 eastbound at Route 9/Marsh",
                "At Calder Fuel & Go around 22:25",
                "22:36:20 westbound at Route 9/Marsh",
                "Entered the Riverside Lot at 22:41:05",
                "Left the Riverside Lot at 22:47:31",
            ],
            [
                ("pr_05", "plate_reader_hits.csv"),
                ("ev_14", "vid_04_fuel_stop.mp4"),
                ("pr_08", "plate_reader_hits.csv"),
                ("pr_09", "plate_reader_hits.csv"),
                ("ev_18", "vid_02_market_lot_shooting.mp4"),
                ("pr_10", "plate_reader_hits.csv"),
            ],
            [
                "v_gray_sedan",
                "ev_14",
                "ev_17",
                "ev_18",
                "ev_22",
                "pr_05",
                "pr_08",
                "pr_09",
                "pr_10",
            ],
        ),
        Spec(
            "q18",
            "multi_source",
            "Trace the gray Civic from when it left Larkspur Court to when it got back.",
            "It left Larkspur Court at 21:52:10 (Larkspur/Bayview ALPR), entered the Riverside "
            "Lot at 22:41:05, exited at 22:47:31, was read inbound at Larkspur/Bayview at "
            "23:02:48 and entered the Larkspur Court garage at 23:03:15.",
            [
                "Left Larkspur Court at 21:52:10",
                "Entered the lot at 22:41:05",
                "Exited the lot at 22:47:31",
                "Back at Larkspur/Bayview at 23:02:48",
                "Entered the garage at 23:03:15",
            ],
            [
                ("pr_01", "plate_reader_hits.csv"),
                ("pr_09", "plate_reader_hits.csv"),
                ("pr_10", "plate_reader_hits.csv"),
                ("pr_11", "plate_reader_hits.csv"),
                ("ev_25", "rpt_supp_03.pdf"),
            ],
            [
                "v_gray_sedan",
                "ev_06",
                "ev_18",
                "ev_22",
                "ev_25",
                "pr_01",
                "pr_09",
                "pr_10",
                "pr_11",
            ],
        ),
        Spec(
            "q19",
            "multi_source",
            "What was Rory Vance's role on the night of the homicide?",
            "Vance, Kemper's cousin, rode as the Civic's passenger: Kemper called his prepaid "
            "phone at 20:50; at Calder Fuel & Go a passenger matching Vance paid $14.62 cash for "
            "energy drinks and cigarettes; the prepaid phone called Kemper at 23:36, about 50 "
            "minutes after the shooting.",
            [
                "Vance is Kemper's cousin",
                "Kemper called the prepaid phone at 20:50",
                "A passenger matching Vance was in the Civic",
                "He paid at Fuel & Go",
                "The prepaid phone called Kemper at 23:36",
            ],
            [
                ("ev_05", "rpt_supp_02.pdf"),
                ("p_associate", "rpt_supp_03.pdf"),
                ("ev_16", "rpt_supp_03.pdf"),
                ("ev_27", "rpt_supp_02.pdf"),
                ("p_associate", "aud_interview.wav"),
            ],
            ["p_associate", "ph_associate", "ev_05", "ev_16", "ev_27"],
        ),
        Spec(
            "q20",
            "multi_source",
            "Could Wes Darnell have shot Marcus Hale? Lay out his movements that night.",
            "Unlikely. His blue Camry SYN2290 entered the lot at 22:10:44 and left at 22:19:02, "
            "was read eastbound at Route 9/Marsh at 22:27:40, and his 22:44:31 call to Hale rang "
            "out unanswered on a tower 9 km east, while the single shot on CAM-A was at 22:44:20.",
            [
                "Camry entered the lot at 22:10:44",
                "Camry left the lot at 22:19:02",
                "Read eastbound at Route 9/Marsh at 22:27:40",
                "His 22:44:31 call went unanswered via a tower 9 km east",
                "The shot was at 22:44:20, after he left",
            ],
            [
                ("pr_03", "plate_reader_hits.csv"),
                ("pr_04", "plate_reader_hits.csv"),
                ("pr_07", "plate_reader_hits.csv"),
                ("ev_21", "rpt_supp_02.pdf"),
                ("ev_20", "rpt_supp_01.pdf"),
            ],
            [
                "p_red_herring",
                "v_blue_camry",
                "ev_10",
                "ev_12",
                "ev_15",
                "ev_20",
                "ev_21",
                "pr_03",
                "pr_04",
                "pr_07",
                "call_13",
            ],
        ),
        Spec(
            "q21",
            "multi_source",
            "Why did Wes Darnell go to the Riverside Lot that night?",
            "He was angry that Hale still had his trailer hitch and owed him $600; he called Hale "
            "around 19:40, and Hale called him back at 22:05 saying \"Come get your hitch, I'm "
            'at the river lot."',
            [
                "The dispute was over a trailer hitch and $600",
                "Darnell called Hale around 19:40",
                'Hale called back: "Come get your hitch, I\'m at the river lot"',
            ],
            [
                ("ev_04", "ws_darnell.pdf"),
                ("ev_09", "ws_darnell.pdf"),
                ("call_12", "call_detail_records.csv"),
            ],
            ["p_red_herring", "ev_04", "ev_09", "call_09", "call_12"],
        ),
        Spec(
            "q22",
            "multi_source",
            "When was the shot fired, and what did the witnesses hear?",
            "CAM-A shows a single muzzle flash at the pickup tailgate at 22:44:20. Elena Ruiz "
            "heard one pop like a firecracker; Gerald Pike heard one loud bang.",
            [
                "A single shot at 22:44:20 on CAM-A",
                "Ruiz heard a pop like a firecracker",
                "Pike heard one loud bang",
            ],
            [("ev_20", "rpt_supp_01.pdf"), ("ev_20", "ws_ruiz.pdf"), ("ev_20", "ws_pike.pdf")],
            ["ev_20", "p_witness_ruiz", "p_witness_pike"],
        ),
        Spec(
            "q23",
            "multi_source",
            "How did the gray sedan leave the lot after the shooting?",
            "Fast with its headlights off, turning right onto Calder River Road: CAM-A shows it "
            "exit at 22:47:30 and the lot ALPR read it exiting at 22:47:31; Ruiz and Pike both "
            "describe it leaving with no lights.",
            [
                "It left with its headlights off",
                "It exited at 22:47:30 on CAM-A",
                "ALPR read it exiting at 22:47:31",
                "It turned right onto Calder River Road",
            ],
            [
                ("ev_22", "rpt_supp_01.pdf"),
                ("pr_10", "plate_reader_hits.csv"),
                ("ev_22", "ws_ruiz.pdf"),
                ("ev_22", "ws_pike.pdf"),
            ],
            ["ev_22", "v_gray_sedan", "pr_10"],
        ),
        Spec(
            "q24",
            "multi_source",
            "What was going on between Kemper and Hale over money?",
            "Hale had lent Kemper $18,000 under a signed loan note (dated 2025-06-02). They "
            "argued about it: a 14-minute call on 03/09 at 19:02, which Kemper admits was an "
            "argument about the money.",
            [
                "Kemper owed Hale $18,000 under a signed loan note",
                "They had a 14-minute call on 03/09",
                "Kemper admits they argued about the money",
            ],
            [
                ("ITEM-032", "evidence_log.csv"),
                ("ev_02", "rpt_supp_02.pdf"),
                ("ev_02", "aud_interview.wav"),
            ],
            ["ITEM-032", "ev_02", "call_03", "p_suspect", "p_victim"],
        ),
        Spec(
            "q25",
            "multi_source",
            "When did Marcus Hale arrive at the Riverside Lot?",
            "His white F-150 SYN7305 was read entering by the lot ALPR at 22:02:31 and is seen "
            "on CAM-A arriving at 22:02:33 and parking in space C-6.",
            [
                "ALPR read SYN7305 entering at 22:02:31",
                "CAM-A shows the pickup at 22:02:33",
                "He parked in space C-6",
            ],
            [
                ("pr_02", "plate_reader_hits.csv"),
                ("ev_08", "vid_01_market_lot_early.mp4"),
                ("ev_08", "rpt_supp_01.pdf"),
            ],
            ["ev_08", "pr_02", "v_victim_pickup", "p_victim"],
        ),
        Spec(
            "q26",
            "multi_source",
            "What did the search of Kemper's apartment and car turn up?",
            "On 03/16 the Civic was seized from garage space 14; the Fuel & Go receipt (22:30:58, "
            "$14.62 cash) was in its console; an empty 9mm pistol box and a box of 9mm "
            "ammunition with 37 of 50 rounds were in the closet. The pistol was not found.",
            [
                "The Civic was seized from garage space 14",
                "The 22:30:58 Fuel & Go receipt was in the car",
                "An empty 9mm pistol box was found",
                "A 9mm ammunition box with 37 of 50 rounds was found",
            ],
            [
                ("ev_29", "rpt_supp_03.pdf"),
                ("ITEM-026", "evidence_log.csv"),
                ("ITEM-029", "evidence_log.csv"),
                ("ITEM-030", "evidence_log.csv"),
            ],
            ["ev_29", "ITEM-023", "ITEM-026", "ITEM-029", "ITEM-030"],
        ),
        Spec(
            "q27",
            "multi_source",
            "Is there physical evidence linking the Civic's occupants to space C-7?",
            "Yes. A Halyard cigarette butt and an empty Volt Surge can were collected behind "
            "C-7; the passenger bought two Volt Surge drinks and Halyard cigarettes at Fuel & Go, "
            "and a Halyard pack was found in the Civic's passenger door pocket.",
            [
                "A Halyard cigarette butt was found at C-7",
                "A Volt Surge can was found at C-7",
                "The passenger bought Volt Surge drinks and Halyard cigarettes",
                "A Halyard pack was in the Civic's passenger door",
            ],
            [
                ("ITEM-008", "evidence_log.csv"),
                ("ITEM-009", "evidence_log.csv"),
                ("ev_16", "rpt_supp_03.pdf"),
                ("ITEM-027", "evidence_log.csv"),
            ],
            ["ITEM-008", "ITEM-009", "ITEM-027", "ev_16", "ev_26"],
        ),
        Spec(
            "q28",
            "multi_source",
            "Who called 911, when, and what did she report?",
            "Elena Ruiz, the Riverside Market night clerk; the call was received at 22:48:40. She "
            "reported a man shot at the Riverside Lot by Riverside Market on Calder River Road.",
            [
                "Elena Ruiz called 911",
                "The call was received at 22:48:40",
                "She reported a man shot at the Riverside Lot",
            ],
            [("ev_23", "rpt_incident.pdf"), ("ev_23", "aud_911.wav"), ("ev_23", "ws_ruiz.pdf")],
            ["ev_23", "p_witness_ruiz", "aud_911"],
        ),
        Spec(
            "q29",
            "multi_source",
            "Is there evidence Kemper arranged to meet Hale at the lot that night?",
            "Yes. On 03/13 at 22:04 Kemper called Hale for 7 minutes; Kemper says they agreed to "
            "meet Saturday night at the river lot around 10:30. On 03/14 at 21:58 he called Hale "
            "again for 62 seconds, six minutes after the Civic left Larkspur Court.",
            [
                "7-minute call on 03/13 at 22:04",
                "They agreed to meet Saturday around 10:30 at the river lot",
                "62-second call on 03/14 at 21:58",
            ],
            [
                ("ev_03", "rpt_supp_02.pdf"),
                ("ev_03", "aud_interview.wav"),
                ("ev_07", "rpt_supp_02.pdf"),
            ],
            ["ev_03", "ev_07", "call_08", "call_11"],
        ),
        Spec(
            "q30",
            "multi_source",
            "What exactly is the gray sedan and who owns it?",
            "A gray 2016 Honda Civic, CA plate SYN4821, with dark aftermarket rims, registered to "
            'Travis Kemper, who calls it "My car. The gray Civic."',
            ["A gray 2016 Honda Civic", "Plate SYN4821", "Registered to Travis Kemper"],
            [
                ("v_gray_sedan", "rpt_supp_01.pdf"),
                ("v_gray_sedan", "rpt_supp_02.pdf"),
                ("v_gray_sedan", "aud_interview.wav"),
            ],
            ["v_gray_sedan", "p_suspect"],
        ),
    ]
    week = {"phone_a": KEMPER, "phone_b": HALE, "window_start": WEEK_START, "window_end": SHOOTING}
    structured = [
        Spec(
            "q31",
            "structured",
            "How many calls were there between the suspect's and the victim's phones in the "
            "week before the homicide?",
            "7 calls between Kemper's +13605550117 and Hale's +13605550142 in the 7 days before "
            "22:44 on 03/14 (03/08 to 03/14).",
            ["7 calls"],
            [],
            ["ph_suspect", "ph_victim"],
            sql=SqlPlan("call_count_between", dict(week)),
        ),
        Spec(
            "q32",
            "structured",
            "List every call between +13605550117 and +13605550142 in the seven days before the "
            "shooting.",
            "7 calls: 03/08 11:05 (Hale to Kemper, 95 s), 03/09 19:02 (842 s), 03/10 08:47 "
            "(12 s), 03/11 21:15 (Hale to Kemper, 310 s), 03/12 17:30 (45 s), 03/13 22:04 "
            "(418 s) and 03/14 21:58 (62 s).",
            ["7 calls", "The longest is 842 s on 03/09", "The last is 03/14 21:58 for 62 s"],
            [],
            ["ph_suspect", "ph_victim"],
            sql=SqlPlan("calls_between", dict(week)),
        ),
        Spec(
            "q33",
            "structured",
            "Who was in phone contact with Marcus Hale's phone on the evening of March 14?",
            "4 calls: Darnell called at 19:40 (183 s); Kemper called at 21:58 (62 s); Hale called "
            "Darnell at 22:05 (75 s); Darnell called at 22:44:31 and it went unanswered (0 s).",
            [
                "4 calls",
                "Darnell 19:40",
                "Kemper 21:58",
                "Hale to Darnell 22:05",
                "Darnell 22:44:31 unanswered",
            ],
            [],
            ["ph_victim"],
            sql=SqlPlan(
                "calls_for_phone",
                {
                    "phone": HALE,
                    "window_start": "2026-03-14T18:00:00-07:00",
                    "window_end": "2026-03-14T23:59:59-07:00",
                },
            ),
        ),
        Spec(
            "q34",
            "structured",
            "What contact was there between Kemper's phone and the prepaid +13605550188 on "
            "March 14 and 15?",
            "4 records: Kemper called it at 20:50 on 03/14 (118 s); it called Kemper at 23:36 "
            "(95 s); Kemper called it at 00:12 on 03/15 (40 s); it texted Kemper at 08:03 on "
            "03/15 (SMS).",
            [
                "4 records",
                "Kemper called at 20:50 on 03/14",
                "The prepaid phone called Kemper at 23:36",
                "Kemper called at 00:12 on 03/15",
                "An SMS at 08:03 on 03/15",
            ],
            [],
            ["ph_suspect", "ph_associate"],
            sql=SqlPlan(
                "calls_between",
                {
                    "phone_a": KEMPER,
                    "phone_b": VANCE_PREPAID,
                    "window_start": "2026-03-14T00:00:00-07:00",
                    "window_end": "2026-03-15T23:59:59-07:00",
                },
            ),
        ),
        Spec(
            "q35",
            "structured",
            "Show every ALPR read of SYN4821 from 9 PM on March 14 to 2 AM on March 15.",
            "7 reads: 21:52:10 Larkspur/Bayview outbound; 22:24:37 Route 9/Marsh eastbound; "
            "22:36:20 Route 9/Marsh westbound; 22:41:05 entering the Riverside Lot; 22:47:31 "
            "exiting; 23:02:48 Larkspur/Bayview inbound; 00:58:12 Route 9/Marsh eastbound.",
            [
                "7 reads",
                "Entering the Riverside Lot at 22:41:05",
                "Exiting at 22:47:31",
                "00:58:12 at Route 9/Marsh",
            ],
            [],
            ["v_gray_sedan"],
            sql=SqlPlan(
                "plate_sightings",
                {
                    "plate": "SYN4821",
                    "window_start": "2026-03-14T21:00:00-07:00",
                    "window_end": "2026-03-15T02:00:00-07:00",
                },
            ),
        ),
        Spec(
            "q36",
            "structured",
            "When did the plate readers pick up Darnell's Camry SYN2290 on the night of March 14?",
            "3 reads: entering the Riverside Lot at 22:10:44, exiting at 22:19:02, and eastbound "
            "at Route 9/Marsh at 22:27:40.",
            [
                "3 reads",
                "Entering the lot 22:10:44",
                "Exiting the lot 22:19:02",
                "Route 9/Marsh eastbound 22:27:40",
            ],
            [],
            ["v_blue_camry"],
            sql=SqlPlan(
                "plate_sightings",
                {
                    "plate": "SYN2290",
                    "window_start": "2026-03-14T21:00:00-07:00",
                    "window_end": "2026-03-14T23:59:59-07:00",
                },
            ),
        ),
        Spec(
            "q37",
            "structured",
            "Which vehicles did the Riverside Lot entrance camera read between 10:30 and 11 PM "
            "on March 14?",
            "Only SYN4821, twice: entering at 22:41:05 and exiting at 22:47:31.",
            ["Only SYN4821", "Entering at 22:41:05", "Exiting at 22:47:31"],
            [],
            ["alpr_riverlot_entrance", "v_gray_sedan"],
            sql=SqlPlan(
                "plate_sightings_near",
                {
                    "location_name": "Riverside",
                    "window_start": "2026-03-14T22:30:00-07:00",
                    "window_end": "2026-03-14T23:00:00-07:00",
                },
            ),
        ),
        Spec(
            "q38",
            "structured",
            "Which plates did the Route 9 at Marsh Rd camera read between 22:20 and 22:40 on "
            "March 14?",
            "4 reads: SYN4821 eastbound 22:24:37, SYN482I 22:24:39 (confidence 0.58, a misread "
            "of SYN4821), SYN2290 eastbound 22:27:40, SYN4821 westbound 22:36:20.",
            [
                "4 reads",
                "SYN4821 at 22:24:37 and 22:36:20",
                "SYN482I is a low-confidence misread",
                "SYN2290 at 22:27:40",
            ],
            [],
            ["alpr_route9_marsh", "v_gray_sedan", "v_blue_camry"],
            sql=SqlPlan(
                "plate_sightings_near",
                {
                    "location_name": "Route 9",
                    "window_start": "2026-03-14T22:20:00-07:00",
                    "window_end": "2026-03-14T22:40:00-07:00",
                },
            ),
        ),
        Spec(
            "q39",
            "structured",
            "What is the chain of custody for the cartridge casing found at the scene?",
            "ITEM-001, the 9mm casing: collected and sealed by ET Kevin Lowell at 23:24 on 03/14; "
            "logged into the property room (locker 12) at 03:10 on 03/15; checked out by Det. "
            "Samuel Brandt at 08:30 on 03/16; received by the crime lab firearms section at 09:05 "
            "on 03/16.",
            [
                "ITEM-001",
                "Collected by Kevin Lowell at 23:24 on 03/14",
                "Property room 03:10 on 03/15",
                "Checked out by Brandt 08:30 on 03/16",
                "Crime lab firearms section 09:05 on 03/16",
            ],
            [],
            ["ITEM-001"],
            sql=SqlPlan("custody_chain", {"item_id": "ITEM-001"}),
        ),
        Spec(
            "q40",
            "structured",
            "Who has handled the gray Civic since it was seized?",
            "ITEM-023: collected by Det. Samuel Brandt at 09:35 on 03/16; towed and sealed at the "
            "impound yard at 10:30; processed under warrant by ET Kevin Lowell at 09:00 on 03/17; "
            "returned to impound bay 5 at 17:00 on 03/17.",
            [
                "ITEM-023",
                "Collected by Brandt 09:35 on 03/16",
                "Impound yard 10:30",
                "Processed by Lowell 09:00 on 03/17",
                "Returned to impound bay 5",
            ],
            [],
            ["ITEM-023", "v_gray_sedan"],
            sql=SqlPlan("custody_chain", {"item_id": "ITEM-023"}),
        ),
    ]
    conflict = [
        Spec(
            "q41",
            "conflict",
            "Do the witnesses agree on when the car left the Riverside Lot and what color it was?",
            "No. Dana Whitfield says a blue car took off after a bang around 10:15 PM, but CAM-A "
            "and the lot ALPR show the gray sedan SYN4821 leaving at 22:47:30-22:47:31 after the "
            "22:44:20 shot. The blue Camry she saw left at 22:18:58 after an argument, before "
            "the gray car arrived.",
            [
                "Whitfield: a blue car left around 10:15 PM after a bang",
                "Video/ALPR: the gray sedan left at 22:47:30-22:47:31",
                "The sources conflict",
            ],
            [
                ("cf_01", "ws_whitfield.pdf"),
                ("cf_01", "rpt_supp_01.pdf"),
                ("pr_10", "plate_reader_hits.csv"),
            ],
            ["cf_01", "p_witness_whitfield", "v_gray_sedan", "v_blue_camry"],
            conflict=(
                "cf_01",
                "Whitfield: around 10:15 PM, after a bang, a blue car took off out of the lot.",
                "CAM-A and ALPR: the gray sedan SYN4821 entered at 22:41:05, single muzzle "
                "flash at 22:44:20, exited at 22:47:30 (ALPR 22:47:31).",
            ),
        ),
        Spec(
            "q42",
            "conflict",
            "Dana Whitfield puts the shooting around 10:15 PM. Does the rest of the evidence "
            "support that time?",
            "No. CAM-A shows no muzzle flash before 22:44:20, when a single shot was fired; the "
            "only blue car, Darnell's Camry, left at 22:18:58 after an argument with no shot. "
            "Her 10:15 account conflicts with the video.",
            [
                "Whitfield says around 10:15 PM",
                "CAM-A shows the single shot at 22:44:20",
                "The sources conflict",
            ],
            [
                ("cf_01", "ws_whitfield.pdf"),
                ("cf_01", "rpt_supp_01.pdf"),
                ("ev_20", "rpt_supp_01.pdf"),
            ],
            ["cf_01", "ev_12", "ev_20", "p_witness_whitfield"],
            conflict=(
                "cf_01",
                "Whitfield: the bang and the blue car leaving were around 10:15 PM.",
                "CAM-A: single muzzle flash at 22:44:20; the blue Camry left at 22:18:58 "
                "with no shot.",
            ),
        ),
        Spec(
            "q43",
            "conflict",
            "Kemper says he never got out of his car and left before 10:40. Does the video agree?",
            'No. Kemper said "I never even got out of the car" and that he left before 10:40, '
            "but CAM-A shows two occupants get out of the gray sedan at 22:42:10, and the car "
            "leaves the lot at 22:47:30.",
            [
                "Kemper: never got out, left before 10:40",
                "CAM-A: two occupants got out at 22:42:10",
                "The car left at 22:47:30",
                "The sources conflict",
            ],
            [
                ("cf_02", "aud_interview.wav", 130008),
                ("cf_02", "aud_interview.wav", 157214),
                ("cf_02", "rpt_supp_03.pdf"),
                ("ev_19", "vid_02_market_lot_shooting.mp4"),
            ],
            ["cf_02", "p_suspect", "ev_19", "ev_22"],
            conflict=(
                "cf_02",
                "Kemper (interview): 'I was by myself. I never even got out of the car.' "
                "He left 'before 10:40'.",
                "CAM-A: two occupants exit the gray sedan at 22:42:10; it exits the lot at "
                "22:47:30.",
            ),
        ),
        Spec(
            "q44",
            "conflict",
            "Was Kemper alone in the gray sedan that night?",
            'No. Kemper said "I was by myself," but the Fuel & Go video shows a passenger in '
            "the Civic, matching Rory Vance, who paid at the register at 22:30:58.",
            [
                "Kemper says he was by himself",
                "The Fuel & Go video shows a passenger who paid at 22:30:58",
                "The passenger matches Rory Vance",
                "The sources conflict",
            ],
            [("cf_03", "aud_interview.wav"), ("cf_03", "rpt_supp_03.pdf")],
            ["cf_03", "p_suspect", "p_associate", "ev_16"],
            conflict=(
                "cf_03",
                "Kemper (interview): 'I was by myself.'",
                "Fuel & Go video: a passenger in the Civic, matching Rory Vance, paid at "
                "the register at 22:30:58.",
            ),
        ),
        Spec(
            "q45",
            "conflict",
            "Do Gerald Pike and Elena Ruiz agree on what make the gray car was?",
            'No. Pike described "a gray or silver four-door, maybe a Toyota"; Ruiz said it was '
            "a gray Honda Civic. ALPR identifies it as SYN4821, a gray 2016 Honda Civic.",
            [
                "Pike: maybe a Toyota",
                "Ruiz: a gray Civic",
                "ALPR: SYN4821 is a gray 2016 Honda Civic",
                "The sources conflict",
            ],
            [
                ("cf_04", "ws_pike.pdf"),
                ("cf_04", "ws_ruiz.pdf"),
                ("v_gray_sedan", "rpt_supp_02.pdf"),
            ],
            ["cf_04", "p_witness_pike", "p_witness_ruiz", "v_gray_sedan"],
            conflict=(
                "cf_04",
                "Pike: 'A gray or silver four-door, maybe a Toyota.'",
                "Ruiz: 'The gray Civic pulled in and parked right next to the white truck.'",
            ),
        ),
    ]
    return single + multi + structured + conflict


UNANSWERABLE_PICK = ("un_01", "un_02", "un_03", "un_05", "un_06")


def unanswerable_specs(truth: Truth) -> list[Spec]:
    by_id = {u.id: u for u in truth.story.unanswerable}
    out = []
    for n, uid in enumerate(UNANSWERABLE_PICK, start=46):
        u = by_id[uid]
        out.append(
            Spec(
                f"q{n}",
                "unanswerable",
                u.question,
                f"Insufficient evidence. {u.reason}",
                ["Says there is insufficient evidence", u.reason],
                [],
                [uid],
            )
        )
    return out


# --------------------------------------------------------------------------------------------
# Assembly, caching and the CLI.


def _item(truth: Truth, spec: Spec, cites: Sequence[CiteRef], sql: SqlSpec | None) -> GoldItem:
    unknown = [s for s in spec.source_ids if s not in truth.story_ids]
    if unknown:
        raise GoldError(f"{spec.id}: unknown story ids {unknown}")
    required = [resolve_cite(truth, ref) for ref in cites]
    cited = {c.modality for c in required}
    modalities = [m for m in MODALITY_ORDER if m in cited]
    conflict = None
    if spec.conflict is not None:
        cf_id, side_a, side_b = spec.conflict
        conflict = ConflictSpec(id=cf_id, sides=[side_a, side_b])
    return GoldItem(
        id=spec.id,
        category=spec.category,
        question=spec.question,
        expected_status="insufficient_evidence" if spec.category == "unanswerable" else "answered",
        expected_answer=spec.answer,
        key_facts=list(spec.key_facts),
        required_citations=required,
        conflict=conflict,
        sql=sql,
        modalities=modalities,
        source_ids=list(spec.source_ids),
    )


def build_items(truth: Truth, expected_sql: Mapping[str, SqlSpec]) -> list[GoldItem]:
    """Build all 50 items; structured results come from `expected_sql` (by item id), which must
    match the spec's operation and params."""
    items = []
    for spec in specs() + unanswerable_specs(truth):
        sql = None
        cites: list[CiteRef] = list(spec.cites)
        if spec.sql is not None:
            cites = _record_cites(truth, spec.sql)
            cached = expected_sql.get(spec.id)
            if (
                cached is None
                or cached.operation != spec.sql.operation
                or cached.params != spec.sql.params
            ):
                raise GoldError(f"{spec.id}: no cached SQL result; run with --refresh-sql")
            sql = cached
        items.append(_item(truth, spec, cites, sql))
    items.sort(key=lambda i: i.id)
    check_mix(items)
    return items


def render(items: Sequence[GoldItem]) -> str:
    lines = [json.dumps(i.to_json_dict(), ensure_ascii=False) for i in items]
    return "\n".join(lines) + "\n"


def cached_sql(path: Path = GOLD_PATH) -> dict[str, SqlSpec]:
    if not path.exists():
        return {}
    return {i.id: i.sql for i in load_gold(path, check=False) if i.sql is not None}


def _libpq(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}" if sep else url


def _row_id(cite: RequiredCitation) -> int:
    if not isinstance(cite.locator, RecordLocator):
        raise GoldError(f"{cite.fact_id}: not a record citation")
    return cite.locator.row_id


def refresh_sql(truth: Truth, case_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, SqlSpec]:
    """Run every structured item's operation on the dev DB (read-only role, RLS as `user_id`)
    and check the cited rows are exactly the story rows the item expects."""
    import psycopg
    from psycopg import sql as pgsql

    from caselens_semantic_layer import run_operation

    url = os.environ.get("CASELENS_DATABASE_URL")
    if not url:
        raise GoldError("CASELENS_DATABASE_URL is not set")
    out: dict[str, SqlSpec] = {}
    with psycopg.connect(_libpq(url), autocommit=True) as conn:
        conn.execute(pgsql.SQL("SET ROLE {}").format(pgsql.Identifier("caselens_readonly")))
        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(user_id),))
        for spec in specs():
            if spec.sql is None:
                continue
            result = run_operation(conn, case_id, spec.sql.operation, spec.sql.params)
            want = sorted(_row_id(resolve_cite(truth, r)) for r in _record_cites(truth, spec.sql))
            got = sorted(s.row_id for s in result.sources)
            if got != want or result.truncated:
                raise GoldError(f"{spec.id}: DB rows {got} != story rows {want}")
            out[spec.id] = SqlSpec(
                operation=spec.sql.operation,
                params=dict(spec.sql.params),
                expected=SqlExpected(row_count=result.row_count, rows_match=list(result.rows)),
            )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m caselens_evals.build_gold")
    parser.add_argument("--out", type=Path, default=GOLD_PATH)
    parser.add_argument("--check", action="store_true", help="fail if --out is stale")
    parser.add_argument(
        "--refresh-sql",
        action="store_true",
        help="recompute structured results on the dev DB (read-only)",
    )
    parser.add_argument("--case-id", type=uuid.UUID)
    parser.add_argument("--user-id", type=uuid.UUID)
    args = parser.parse_args(argv)

    truth = load_truth()
    if args.refresh_sql:
        if args.case_id is None or args.user_id is None:
            parser.error("--refresh-sql needs --case-id and --user-id")
        expected = refresh_sql(truth, args.case_id, args.user_id)
    else:
        expected = cached_sql(args.out)
    text = render(build_items(truth, expected))
    if args.check:
        current = args.out.read_bytes() if args.out.exists() else b""
        if current != text.encode("utf-8"):
            print(f"{args.out} is stale; rebuild it", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(text.encode("utf-8"))
    print(f"wrote {args.out} (50 items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
