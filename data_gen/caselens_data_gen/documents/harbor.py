"""Document text for the SYNTHETIC "Harbor Street Burglary" case (the small second case)."""

from __future__ import annotations

from collections.abc import Callable

from caselens_data_gen.documents.common_blocks import (
    officer_signoff,
    persons_section,
    property_section,
    report_fields,
)
from caselens_data_gen.documents.facts import StoryView, mdy, officer_label, stamp, stamp_s
from caselens_data_gen.documents.layout import (
    Block,
    DocSpec,
    Fields,
    Heading,
    Para,
    Signature,
    Table,
)


def _spec(
    sv: StoryView, plan_id: str, form_title: str, blocks: list[Block], facts: list[tuple[str, str]]
) -> DocSpec:
    return DocSpec(
        plan_id=plan_id,
        form_title=form_title,
        doc_title=sv.plan_item(plan_id).title,
        case_number=sv.story.case_number,
        agency=sv.story.agency,
        blocks=blocks,
        facts=facts,
    )


def rpt_hs_incident(sv: StoryView) -> DocSpec:
    plan = sv.plan_item("rpt_hs_incident")
    s = sv.story
    owner = sv.person("p_hs_victim")
    hollis = sv.person("o_hs_patrol")
    park = sv.person("o_hs_det")
    ashe = sv.person("p_hs_suspect")
    van = sv.vehicle("v_hs_van")
    store = sv.locations["loc_hs_store"]
    suspect_phone = sv.phone("ph_hs_suspect")
    contact = sv.phone("ph_hs_contact")
    ev07 = sv.events["hs_ev_07"]
    calls = sorted(s.calls, key=lambda c: c.started_at)
    phones = {p.id: p.number for p in s.phones}
    blocks: list[Block] = [
        report_fields(
            [
                ("Case number", s.case_number),
                ("Report type", "Original incident report"),
                (
                    "Offense",
                    "Commercial burglary, forced entry (offense code 220; synthetic coding)",
                ),
                ("Occurred", "03/21/2026 after close - 03/22/2026 07:10 hours"),
                ("Reported", f"{mdy(ev07.t_start)} 07:12 hours by the owner"),
                ("Location", f"{store.name}, {store.address}"),
                ("Reporting officer", officer_label(hollis)),
                ("Report written", stamp(plan.recorded_at)),
            ]
        ),
        Heading("Narrative"),
        Para(
            f"On {mdy(ev07.t_start)} I, {officer_label(hollis)}, responded at 07:25 hours to "
            f"{store.name}, {store.address}, on a burglary report. The owner, {owner.name}, age "
            f"{owner.age}, stated she arrived to open the shop at 07:10 hours and found the rear "
            "service door pried open. The latch side of the door frame had fresh pry marks."
        ),
        Para(
            f"{owner.name} stated three outboard motors were missing from the rear stock room: two "
            "9.9 hp motors and one 20 hp motor, total value approximately $14,500. Nothing else "
            "was reported missing. The shop has no alarm. A camera over the rear door covers the "
            "alley."
        ),
        Para(
            f"{officer_label(park)} responded to process the scene. Between 08:00 and 09:30 hours "
            "she collected a cast of the pry marks, a black LED headlamp dropped in the alley "
            "gravel, and the rear alley CCTV export. She also took a statement from the night "
            "guard at the boatyard next door and a copy of his log."
        ),
        Para(
            "Review of the rear alley camera shows a white high-roof van back up to the door at "
            "01:10:30 hours. A man in a black beanie and gray coveralls pries the rear door with "
            "a bar at about 01:12 and carries three outboard motors to the van by 01:31. The van "
            "leaves at 01:33."
        ),
        Heading("Stolen property"),
        Table(
            ["Qty", "Description", "Value (USD)"],
            [
                ["2", "Outboard motor, 9.9 hp, shop stock", "approx. 7,600"],
                ["1", "Outboard motor, 20 hp, shop stock", "approx. 6,900"],
                ["", "Total", "approx. 14,500"],
            ],
            [10, 60, 30],
        ),
    ]
    blocks += property_section(sv, ["ITEM-001", "ITEM-002", "ITEM-003", "ITEM-004"])
    blocks += persons_section(
        sv,
        [
            ("V-1", "p_hs_victim", "Owner / victim"),
            ("W-1", "p_hs_witness", "Night guard, Gullwing Boatyard"),
        ],
    )
    blocks.append(officer_signoff(sv, "o_hs_patrol", stamp(plan.recorded_at)))
    blocks += [
        Heading(f"Supplement 03/24/2026 - {officer_label(park)}"),
        Para(
            f"The ALPR at Harbor St and Pier Rd read a white Ford Transit, CA plate {van.plate}, "
            "southbound at 01:09:40 and northbound at 01:33:40 hours. The van is registered to "
            f"{ashe.name}, age {ashe.age}. Carrier records for {ashe.name}'s phone "
            f"{suspect_phone.number} were obtained on 03/24."
        ),
        Para(
            f"On 03/21 at 18:05 hours {suspect_phone.number} called {contact.number} for 3 "
            "minutes, about seven hours before the burglary. On 03/22 at 01:47 hours, 14 minutes "
            f"after the van left Harbor Street, it called {contact.number} again for 2 minutes. "
            f"The number {contact.number} is a prepaid line ({contact.carrier}) with a blank "
            "subscriber field; the subscriber is unknown."
        ),
        Table(
            ["Started", "From", "To", "Type", "Duration (s)"],
            [
                [
                    stamp_s(c.started_at),
                    phones[c.caller_phone_id],
                    phones[c.callee_phone_id],
                    c.call_type,
                    str(c.duration_s),
                ]
                for c in calls
            ],
            [22, 22, 22, 12, 14],
        ),
        Signature([(f"Supplement by {officer_label(park)}", f"/s/ {park.name}")]),
    ]
    facts = [
        ("hs_ev_01", f"On 03/21 at 18:05 hours {suspect_phone.number} called {contact.number}"),
        ("hs_ev_03", "pries the rear door with a bar at about 01:12"),
        ("hs_ev_06", "On 03/22 at 01:47 hours"),
        ("hs_ev_07", "arrived to open the shop at 07:10 hours"),
        ("hs_ev_08", "Between 08:00 and 09:30 hours she collected a cast of the pry marks"),
        ("p_hs_victim", f"The owner, {owner.name}"),
        ("loc_hs_store", f"{store.name}, {store.address}, on a burglary report"),
        ("v_hs_van", f"CA plate {van.plate}"),
        ("ph_hs_contact", f"The number {contact.number} is a prepaid line"),
    ]
    return _spec(sv, "rpt_hs_incident", "INCIDENT REPORT", blocks, facts)


def ws_hs_ferrante(sv: StoryView) -> DocSpec:
    plan = sv.plan_item("ws_hs_ferrante")
    ferrante = sv.person("p_hs_witness")
    park = sv.person("o_hs_det")
    yard = sv.locations["loc_hs_boatyard"]
    line = plan.key_lines[0].text
    blocks: list[Block] = [
        Fields(
            [
                ("Name", ferrante.name),
                ("Age", str(ferrante.age)),
                ("Occupation", ferrante.occupation or "-"),
                ("Date / time taken", stamp(plan.recorded_at)),
                ("Place taken", f"{yard.name} guard booth, {yard.address}"),
                ("Taken by", officer_label(park)),
            ]
        ),
        Heading("Statement"),
        Para(
            f"My name is {ferrante.name}. I am the night guard at {yard.name}, right next to "
            "Harbor Street Marine Supply. My booth looks over the alley behind the shop. I work "
            "10 PM to 6 AM."
        ),
        Para(
            f"{line} The van's back doors were open. The man had a little light on his head, a "
            "headlamp, and a dark hat. I could not see his face."
        ),
        Para(
            "I thought it was a delivery. Sometimes they get parts late. I wrote it in my log as "
            "a delivery and kept walking my round. When I came back by the booth the van was "
            "gone. I did not see which way it went and I did not see the plate."
        ),
        Para("I gave the detective a copy of my log. I did not hear any breaking or any alarm."),
        Para(
            "I have read this statement of 1 page. It is true and correct to the best of my "
            "knowledge.",
            style="small",
        ),
        Signature(
            [
                (f"Signature of person giving statement ({ferrante.name})", ferrante.name),
                (f"Witnessing officer: {officer_label(park)}", f"/s/ {park.name}"),
            ]
        ),
    ]
    facts = [
        ("hs_ev_03", "a guy with a headlamp loading something heavy"),
        ("hs_ev_04", "I wrote it in my log as a delivery"),
        ("hs_cf_01", "Around one in the morning"),
        ("p_hs_witness", f"My name is {ferrante.name}"),
        ("v_hs_van", "a white van backed up to the Marine Supply door"),
    ]
    return _spec(sv, "ws_hs_ferrante", "VOLUNTARY WITNESS STATEMENT", blocks, facts)


BUILDERS: dict[str, Callable[[StoryView], DocSpec]] = {
    "rpt_hs_incident": rpt_hs_incident,
    "ws_hs_ferrante": ws_hs_ferrante,
}
