"""Document text for the SYNTHETIC "Riverside Lot Homicide" case.

Police-report voice for reports, first person for statements. Names, plates, phone numbers, times
and quotes come from the story; the narrative glue is written here. Each spec lists the key phrase
that carries every event / entity / conflict it covers, so the answer key can point at it.
"""

from __future__ import annotations

from collections.abc import Callable

from caselens_data_gen.documents.common_blocks import (
    officer_signoff,
    persons_section,
    property_section,
    report_fields,
    vehicles_section,
)
from caselens_data_gen.documents.facts import (
    StoryView,
    duration,
    hms,
    mdy,
    officer_label,
    pretty_phone,
    stamp,
    stamp_s,
)
from caselens_data_gen.documents.layout import (
    Block,
    DocSpec,
    Fields,
    Heading,
    PageBreak,
    Para,
    Signature,
    Table,
)


def _spec(
    sv: StoryView,
    plan_id: str,
    form_title: str,
    blocks: list[Block],
    facts: list[tuple[str, str]],
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


def rpt_incident(sv: StoryView) -> DocSpec:
    plan = sv.plan_item("rpt_incident")
    s = sv.story
    hale = sv.person("p_victim")
    ruiz = sv.person("p_witness_ruiz")
    nanda = sv.person("o_patrol")
    brandt = sv.person("o_det_lead")
    lowell = sv.person("o_csi")
    pickup = sv.vehicle("v_victim_pickup")
    lot = sv.locations["loc_riverside_lot"]
    market = sv.locations["loc_riverside_market"]
    ev20, ev23, ev24 = sv.events["ev_20"], sv.events["ev_23"], sv.events["ev_24"]
    ruiz_phone = sv.phone("ph_witness_ruiz")
    blocks: list[Block] = [
        report_fields(
            [
                ("Case number", s.case_number),
                ("Report type", "Original incident report"),
                (
                    "Offense",
                    "Homicide - willful killing, firearm (offense code 09A; synthetic coding)",
                ),
                ("Occurred", f"{mdy(ev20.t_start)} approx. {ev20.t_start:%H:%M} hours (Saturday)"),
                (
                    "Reported",
                    f"{mdy(ev23.t_start)} 22:48:40 hours, 911 call from cell phone "
                    f"{pretty_phone(ruiz_phone.number)}",
                ),
                ("Dispatched / arrived", f"22:49:30 / {hms(ev24.t_start)} hours"),
                ("Location", f"{lot.name}, {lot.address}"),
                ("Reporting officer", f"{officer_label(nanda)}, patrol unit 2-14"),
                ("Report written", stamp(plan.recorded_at)),
                ("Case status", f"Open - assigned to {officer_label(brandt)}"),
            ]
        ),
        Heading("Narrative"),
        Para(
            f"On {mdy(ev23.t_start)} at 22:49:30 hours I, {officer_label(nanda)}, assigned to "
            "patrol unit 2-14, was dispatched by the Port Calder Emergency Communications Center "
            f"to the {lot.name}, {lot.address}, on a 911 call reporting a man shot. The call was "
            f"received at 22:48:40 hours from the reporting party (R/P), {ruiz.name}, the night "
            f"clerk at {market.name}, {market.address}, which faces the lot entrance."
        ),
        Para(
            f"I arrived on scene at {hms(ev24.t_start)} hours. The {lot.name} is an unattended "
            "public surface lot beside the Calder River levee with rows A through F. The sodium "
            "light over row C was out. I observed an adult male lying on the pavement between a "
            f"white {pickup.year} {pickup.make} {pickup.model} pickup, CA plate {pickup.plate}, "
            "parked in space C-6 nose toward the levee, and the empty space C-7. The R/P was "
            "kneeling beside him holding her phone."
        ),
        Para(
            "The male was unresponsive with a single apparent gunshot wound to the center of the "
            "chest. He was wearing a tan canvas work jacket and jeans. I checked for a pulse and "
            "found none and began CPR. Port Calder Fire Medic 6 arrived and took over care. Medic "
            "6 pronounced the male dead at 23:01 hours. The male was identified from the driver "
            f"license in his wallet as {hale.name}, age {hale.age}. No weapon was found near the "
            "victim."
        ),
        Para(
            "I observed a single brass cartridge casing on the pavement east of the pickup's rear "
            "bumper and a black smartphone partly under the victim's left hip. I did not move "
            "either item and I protected them until the evidence technician arrived."
        ),
        Heading("Reporting party"),
        Para(
            f"I spoke with the R/P, {ruiz.name}, age {ruiz.age}, at the scene. She stated she was "
            f"working the register at {market.name} and was watching the lot through the front "
            "window. She stated that at 10:41 PM a gray car pulled into the lot and parked beside "
            "the white pickup and that two men got out of it."
        ),
        Para(plan.key_lines[0].text, style="quote"),
        Para(
            "The R/P described the suspect vehicle as 'a gray car, like a Civic' and stated it "
            "left toward Calder River Road with its headlights off, turning right out of the lot. "
            "She did not see a plate. She estimated the shot at about 22:44 hours. After the car "
            "left she went outside, found the victim on the ground and called 911. A written "
            "statement was taken from the R/P at the market counter at 23:40 hours (see attached "
            "witness statement)."
        ),
        Heading("Scene security and notifications"),
        Para(
            "I secured the scene with crime scene tape across the lot entrance on Calder River "
            "Road and started a scene log. My patrol unit dashcam was recording on arrival. "
            f"{officer_label(brandt)} was notified at 23:05 hours. Evidence Technician "
            f"{lowell.name} ({lowell.badge}) arrived at 23:18 hours and took custody of the scene "
            "evidence listed below. No suspect was located. The case was turned over to "
            f"{officer_label(brandt)} for follow-up."
        ),
    ]
    blocks += persons_section(
        sv,
        [
            ("V-1", "p_victim", "Victim (deceased)"),
            ("RP/W-1", "p_witness_ruiz", "Reporting party, witness"),
        ],
    )
    blocks += [
        Heading("Vehicles"),
        Table(
            ["Code", "Description", "Plate", "Status"],
            [
                [
                    "VEH-1",
                    f"White {pickup.year} {pickup.make} {pickup.model}, black ladder rack",
                    f"{pickup.plate} (CA)",
                    f"At scene, space C-6. Registered to {hale.name}.",
                ],
                [
                    "VEH-2",
                    "Suspect vehicle: gray four-door sedan, possibly a Honda Civic (per R/P)",
                    "Unknown",
                    "Left the lot toward Calder River Road, headlights off, approx. 22:47.",
                ],
            ],
            [8, 40, 14, 38],
        ),
    ]
    blocks += property_section(sv, ["ITEM-001", "ITEM-002", "ITEM-003", "ITEM-004", "ITEM-005"])
    blocks.append(officer_signoff(sv, "o_patrol", stamp(plan.recorded_at)))
    facts = [
        ("ev_20", "a pop like a firecracker"),
        ("ev_23", "received at 22:48:40 hours from the reporting party"),
        ("ev_24", f"I arrived on scene at {hms(ev24.t_start)} hours"),
        ("p_victim", f"{hale.name}, age {hale.age}"),
        ("p_witness_ruiz", f"I spoke with the R/P, {ruiz.name}"),
        ("loc_riverside_lot", f"to the {lot.name}, {lot.address}"),
        ("v_victim_pickup", f"CA plate {pickup.plate}"),
        ("v_gray_sedan", "a gray car, like a Civic"),
    ]
    return _spec(sv, "rpt_incident", "INCIDENT REPORT", blocks, facts=facts)


def rpt_supp_01(sv: StoryView) -> DocSpec:
    plan = sv.plan_item("rpt_supp_01")
    s = sv.story
    hale = sv.person("p_victim")
    darnell = sv.person("p_red_herring")
    pike = sv.person("p_witness_pike")
    whit = sv.person("p_witness_whitfield")
    ruiz = sv.person("p_witness_ruiz")
    brandt = sv.person("o_det_lead")
    camry = sv.vehicle("v_blue_camry")
    pickup = sv.vehicle("v_victim_pickup")
    lot = sv.locations["loc_riverside_lot"]
    blocks: list[Block] = [
        report_fields(
            [
                ("Case number", s.case_number),
                ("Report type", "Supplemental report 1 - scene, canvass and video review"),
                ("Offense", "Homicide - willful killing, firearm (09A; synthetic coding)"),
                ("Author", officer_label(brandt)),
                ("Report written", stamp(plan.recorded_at)),
                ("Related", "Incident report by Officer Nandakumar; witness statements 1-4"),
            ]
        ),
        Heading("Scene"),
        Para(
            f"I responded to the {lot.name}, {lot.address}, at approximately 23:20 hours on "
            "03/14/2026. Scene processing ran from 23:20 to 02:10 hours with Evidence Technician "
            "Lowell. The lot has 60 spaces in rows A-F lit by sodium lights on four poles; the "
            "light over row C was out. The only vehicle entrance and exit is on Calder River "
            "Road, where an ALPR camera reads plates in both directions. A chain-link fence runs "
            "along Alder Row to the south and the Route 9 bus shelter sits on the east edge of "
            "the lot."
        ),
        Para(
            f"The victim, {hale.name}, lay between his white pickup ({pickup.plate}) in space C-6 "
            "and the empty space C-7. Eight evidence placards were set. One 9mm cartridge casing "
            "was recovered 2.1 m east of the pickup's rear bumper. A Halyard brand cigarette butt "
            "and an empty Volt Surge energy drink can were found at the rear of space C-7, where "
            "a partial tire impression was cast. The victim's wallet with $212 cash was intact, "
            "so robbery is considered unlikely."
        ),
        Table(
            ["Placard", "Item", "Description"],
            [
                ["1", "ITEM-001", "9mm cartridge casing, headstamp 'SYN 9MM'"],
                ["2", "ITEM-002", "Victim's black smartphone, under left hip"],
                ["3", "ITEM-004", "Key ring with Ford key fob, by pickup driver door"],
                ["4", "ITEM-005", "Blood swab, pool between C-6 and C-7"],
                ["5", "ITEM-006", "Tire impression cast, front of space C-7"],
                ["6", "ITEM-008", "Halyard cigarette butt, behind C-7"],
                ["7", "ITEM-009", "Volt Surge can, 16 oz, rear of C-7"],
                ["8", "ITEM-010", "Work-boot footwear impression, pickup tailgate"],
            ],
            [10, 14, 76],
        ),
        Heading("Video review - Riverside Market CAM-A"),
        Para(
            "On 03/15/2026 I exported Riverside Market CAM-A (roof camera facing the lot) and "
            "CAM-B (front door) for 22:00-23:00 hours with the clerk and the manager present "
            "(ITEM-015). The DVR clock was checked against network time and is within one second. "
            "My review of CAM-A found the following:"
        ),
        Table(
            ["Time", "Observation (CAM-A)"],
            [
                ["22:02:33", "White F-150 enters and parks in C-6, nose toward the levee"],
                ["22:10:44", "Dark blue Camry enters and stops behind the pickup"],
                [
                    "22:11:30",
                    "Two men argue at the tailgate; one lifts a trailer hitch into the Camry",
                ],
                ["22:18:58", "Blue Camry leaves the lot quickly"],
                [
                    "22:41:05",
                    "Gray sedan enters the lot; parks in C-7 beside the pickup at 22:41:40",
                ],
                ["22:42:10", "Two occupants get out of the gray sedan"],
                ["22:44:20", "Single muzzle flash at the pickup tailgate; one figure drops"],
                ["22:47:30", "Gray sedan exits the lot with its headlights off"],
            ],
            [14, 86],
        ),
        Para(
            "Hale's pickup is seen arriving at 22:02:33 and parking in C-6. "
            f"{plan.key_lines[0].text} The sedan's front plate area is empty (no front plate). The "
            "driver wears a dark hooded top with the hood up and walks to the victim at the "
            "tailgate; the passenger, taller, in a light cap, stays near the sedan's rear bumper. "
            "At 22:44:20 there is a single muzzle flash and the victim falls. Both men get back "
            "in the sedan, which reverses, idles dark, and exits the lot with its headlights off "
            "at 22:47:30."
        ),
        Heading("Canvass"),
        Para(
            f"{ruiz.name} (Riverside Market clerk) gave a written statement to Officer Nandakumar "
            f"at 23:40 hours on 03/14. {pike.name}, who lives on Alder Row behind the lot, was "
            "located during the morning canvass on 03/15; he was walking his dog along the fence, "
            "heard raised voices and one gunshot, and saw 'a gray or silver four-door' leave with "
            "no headlights, partial plate 'SYN 48'. He gave a handwritten statement at 10:15 "
            "hours."
        ),
        Para(
            f"{whit.name}, a bus rider, was located on 03/15 through the transit tip line and "
            "interviewed at 16:00 hours. She states that around 10:15 PM, after a bang, a blue "
            "car left the lot. Her account conflicts with the CAM-A video: there is no muzzle "
            "flash before 22:44:20, and the only blue car, the Camry, leaves at 22:18:58 after an "
            "argument with no shot. She states she boarded the 10:21 PM Route 9 bus, before the "
            "gray sedan arrived. I believe she saw the Camry argument and has merged it with the "
            "homicide she later heard about. Her statement is not relied on for the time of the "
            "shooting."
        ),
        Heading("Blue Camry / Wes Darnell"),
        Para(
            f"The blue Camry was identified by the lot entrance ALPR as CA {camry.plate}, a "
            f"{camry.year} {camry.make} {camry.model} registered to {darnell.name}, age "
            f"{darnell.age}, the victim's brother-in-law. I interviewed {darnell.name} at his "
            "residence on 03/15 at 13:30 hours. He states he argued with the victim by phone at "
            "19:40 hours over a trailer hitch and $600, met him at the lot after 22:10, took the "
            "hitch (surrendered, ITEM-039) and left before 22:20. His account matches CAM-A. See "
            "Supplemental 2 for phone and ALPR records on his movements."
        ),
        Heading("Findings"),
        Para(
            "The homicide occurred at 22:44:20 hours. The suspect vehicle is a gray four-door "
            "sedan consistent with a 2016 Honda Civic with dark aftermarket rims and no front "
            "plate, carrying two men. Follow-up: identify the gray sedan through ALPR and the "
            "victim's call records."
        ),
    ]
    blocks.append(officer_signoff(sv, "o_det_lead", stamp(plan.recorded_at)))
    facts = [
        ("ev_04", "argued with the victim by phone at 19:40 hours"),
        ("ev_08", "pickup is seen arriving at 22:02:33"),
        ("ev_10", "Dark blue Camry enters and stops behind the pickup"),
        ("ev_11", "Two men argue at the tailgate"),
        ("ev_18", "Gray sedan enters the lot; parks in C-7"),
        ("ev_20", "Single muzzle flash at the pickup tailgate"),
        ("ev_22", "exits the lot with its headlights off at 22:47:30"),
        ("ev_26", "Scene processing ran from 23:20 to 02:10 hours"),
        ("cf_01", "Her account conflicts with the CAM-A video"),
        ("v_gray_sedan", plan.key_lines[0].text),
        ("v_blue_camry", f"CA {camry.plate}"),
        ("v_victim_pickup", f"white pickup ({pickup.plate})"),
        ("p_red_herring", f"registered to {darnell.name}"),
        ("loc_riverside_lot", f"I responded to the {lot.name}"),
    ]
    return _spec(sv, "rpt_supp_01", "SUPPLEMENTAL REPORT 1", blocks, facts=facts)


def rpt_supp_02(sv: StoryView) -> DocSpec:
    plan = sv.plan_item("rpt_supp_02")
    s = sv.story
    iverson = sv.person("o_det_records")
    kemper = sv.person("p_suspect")
    hale = sv.person("p_victim")
    darnell = sv.person("p_red_herring")
    vance = sv.person("p_associate")
    civic = sv.vehicle("v_gray_sedan")
    camry = sv.vehicle("v_blue_camry")
    ph = {k: sv.phone(k) for k in ("ph_suspect", "ph_victim", "ph_associate", "ph_red_herring")}
    cams = {c.id: c.name for c in s.cameras}
    owners = {
        "ph_suspect": kemper.name,
        "ph_victim": hale.name,
        "ph_associate": "Unknown (prepaid)",
        "ph_red_herring": darnell.name,
    }
    reads = [r for r in s.plate_reads if r.plate.startswith(("SYN482", "SYN229"))]
    read_rows = [
        [
            r.plate,
            stamp_s(r.read_at),
            cams[r.camera_id],
            r.direction,
            f"{r.confidence:.2f}",
        ]
        for r in sorted(reads, key=lambda r: r.read_at)
    ]
    call_rows = [
        [
            stamp_s(c.started_at),
            ph[c.caller_phone_id].number,
            ph[c.callee_phone_id].number,
            duration(c.duration_s) if c.call_type == "voice" else "SMS",
            c.cell_tower_id or "-",
        ]
        for c in sorted(s.calls, key=lambda c: c.started_at)
        if c.caller_phone_id in ph and c.callee_phone_id in ph
    ]
    blocks: list[Block] = [
        report_fields(
            [
                ("Case number", s.case_number),
                ("Report type", "Supplemental report 2 - call detail records and ALPR analysis"),
                ("Author", officer_label(iverson)),
                ("Report written", stamp(plan.recorded_at)),
                (
                    "Sources",
                    "PCPD ALPR system export (ITEM-021); carrier subpoena return (ITEM-022)",
                ),
            ]
        ),
        Heading("Subscriber summary"),
        Table(
            ["Number", "Carrier", "Subscriber"],
            [[ph[k].number, ph[k].carrier, owners[k]] for k in ph],
            [25, 35, 40],
        ),
        Heading("ALPR analysis"),
        Para(
            f"The ALPR system was queried for the gray sedan seen on CAM-A. Plate {civic.plate} "
            f"(CA), a gray {civic.year} {civic.make} {civic.model} registered to {kemper.name}, "
            f"age {kemper.age}, of Larkspur Court Apartments unit 14, was the only gray sedan read "
            "at the lot entrance in the window. The reads place the car as follows."
        ),
        Para(
            f"{civic.plate} was read leaving Larkspur Court at the Larkspur/Bayview camera at "
            "21:52:10 hours, then eastbound at Route 9 and Marsh Road at 22:24:37 hours, near "
            "Calder Fuel & Go. A second read at 22:24:39 (SYN482I, confidence 0.58) is a misread "
            "of the same car. It was read westbound at Route 9/Marsh at 22:36:20 hours, back "
            f"toward the river. {plan.key_lines[0].text} It was read inbound at Larkspur/Bayview "
            "at 23:02:48 hours, and again eastbound at Route 9/Marsh at 00:58:12 hours on 03/15; "
            "the purpose of that late trip is not known."
        ),
        Para(
            f"The blue Camry {camry.plate} ({darnell.name}) was read exiting the lot at 22:19:02 "
            "hours and eastbound at Route 9/Marsh, 4 km east, at 22:27:40 hours, more than 15 "
            "minutes before the shooting."
        ),
        Table(["Plate", "Read at", "Camera", "Direction", "Conf."], read_rows, [12, 18, 40, 15, 9]),
        Heading("Call detail records"),
        Para(
            f"The carrier return covers {ph['ph_suspect'].number} ({kemper.name}), "
            f"{ph['ph_victim'].number} ({hale.name}), {ph['ph_associate'].number} (prepaid) and "
            f"{ph['ph_red_herring'].number} ({darnell.name}) from 02/28 to 03/15/2026. There are 7 "
            "voice calls between Kemper's and Hale's phones from 03/08 to 03/14, plus one earlier "
            "call on 03/02 at 18:20 hours lasting 4 minutes, outside the 7-day window."
        ),
        Para(
            "On 03/09 at 19:02 hours Kemper's phone called Hale's phone for 14 minutes, the "
            "longest call between them. On 03/13 at 22:04 hours Kemper called Hale for 7 minutes, "
            "the night before the homicide. On 03/14 at 21:58 hours Kemper called Hale for 62 "
            "seconds, six minutes after the Civic left Larkspur Court."
        ),
        Para(
            f"The prepaid number {ph['ph_associate'].number} ({ph['ph_associate'].carrier}) has a "
            "blank subscriber field. Kemper called it on 03/14 at 20:50 hours for 2 minutes, "
            "about an hour before the Civic left Larkspur Court, and it called Kemper at 23:36 "
            "hours for 95 seconds, about 50 minutes after the shooting. The contact continues "
            "after the homicide."
        ),
        Para(
            f"Darnell's phone {ph['ph_red_herring'].number} called Hale at 19:40 hours (3 minutes) "
            "and Hale called Darnell from the lot at 22:05 hours. Darnell's call to Hale at "
            "22:44:31 hours rang out unanswered (0 s) and was served by tower TWR-PC-EAST-07, 9 "
            "km east of the lot. This supports his statement that he was home at the time of the "
            "shooting."
        ),
        Table(["Started", "From", "To", "Duration", "Tower"], call_rows, [20, 22, 22, 14, 22]),
        Heading("Request"),
        Para(
            "Based on the above I request a search warrant for Larkspur Court Apartments unit 14, "
            f"its garage space 14, and the gray {civic.make} {civic.model} {civic.plate}."
        ),
        Heading("Addendum 03/17/2026"),
        Para(
            f"On 03/17 {vance.name}, Kemper's cousin, surrendered a prepaid flip phone with "
            f"consent (ITEM-034). It is {ph['ph_associate'].number}. The prepaid number is "
            f"therefore linked to {vance.name}."
        ),
    ]
    blocks.append(officer_signoff(sv, "o_det_records", stamp(plan.recorded_at)))
    facts = [
        ("ev_01", "earlier call on 03/02 at 18:20 hours lasting 4 minutes"),
        ("ev_02", "On 03/09 at 19:02 hours Kemper's phone called Hale's phone for 14 minutes"),
        ("ev_03", "On 03/13 at 22:04 hours Kemper called Hale for 7 minutes"),
        ("ev_05", "Kemper called it on 03/14 at 20:50 hours for 2 minutes"),
        ("ev_06", "read leaving Larkspur Court at the Larkspur/Bayview camera at 21:52:10"),
        ("ev_07", "On 03/14 at 21:58 hours Kemper called Hale for 62 seconds"),
        ("ev_14", "eastbound at Route 9 and Marsh Road at 22:24:37 hours"),
        ("ev_15", "eastbound at Route 9/Marsh, 4 km east, at 22:27:40 hours"),
        ("ev_18", plan.key_lines[0].text),
        ("ev_21", "22:44:31 hours rang out unanswered"),
        ("ev_25", "inbound at Larkspur/Bayview at 23:02:48 hours"),
        ("ev_27", "it called Kemper at 23:36 hours for 95 seconds"),
        ("ev_28", "eastbound at Route 9/Marsh at 00:58:12 hours"),
        ("ph_suspect", f"The carrier return covers {ph['ph_suspect'].number}"),
        ("ph_victim", f"{ph['ph_victim'].number} ({hale.name})"),
        ("ph_associate", f"The prepaid number {ph['ph_associate'].number}"),
        ("ph_red_herring", f"Darnell's phone {ph['ph_red_herring'].number}"),
        ("v_gray_sedan", f"Plate {civic.plate} (CA), a gray {civic.year}"),
        ("v_blue_camry", f"The blue Camry {camry.plate}"),
    ]
    return _spec(sv, "rpt_supp_02", "SUPPLEMENTAL REPORT 2", blocks, facts=facts)


def rpt_supp_03(sv: StoryView) -> DocSpec:
    plan = sv.plan_item("rpt_supp_03")
    s = sv.story
    brandt = sv.person("o_det_lead")
    kemper = sv.person("p_suspect")
    vance = sv.person("p_associate")
    civic = sv.vehicle("v_gray_sedan")
    apt = sv.locations["loc_kemper_apartment"]
    fuel = sv.locations["loc_fuel_stop"]
    blocks: list[Block] = [
        report_fields(
            [
                ("Case number", s.case_number),
                ("Report type", "Supplemental report 3 - search warrant, arrest, interview"),
                ("Author", officer_label(brandt)),
                ("Report written", stamp(plan.recorded_at)),
                ("Arrestee", f"{kemper.name}, age {kemper.age}, {apt.name} unit 14"),
            ]
        ),
        Heading("Calder Fuel & Go video"),
        Para(
            f"On 03/15 Det. Iverson collected the {fuel.name} ({fuel.address}) CCTV export for "
            "22:23-22:38 hours (ITEM-017) and the register journal (ITEM-018). The gray Civic "
            f"{civic.plate} pulls up to pump 4 at about 22:26. A passenger matching {vance.name} "
            "(tall, red hair, light gray cap, olive-green bomber jacket, white sneakers) goes "
            "into the store and pays $14.62 cash for two Volt Surge energy drinks and a pack of "
            "Halyard cigarettes at the register at 22:30:58 hours. The driver, in a dark hooded "
            "jacket, pumps $20 of gas and stays by the car. The Civic leaves at about 22:36."
        ),
        Heading("Larkspur Court garage video"),
        Para(
            f"The {apt.name} garage gate camera (ITEM-019) shows the Civic enter the garage at "
            "23:03:15 hours. Kemper gets out carrying a bundled dark jacket under his arm and "
            f"goes to the stairwell. The passenger, {vance.name}, walks out the pedestrian gate at "
            "23:05:40 hours."
        ),
        Heading("Search warrant and arrest"),
        Para(
            f"On 03/16/2026 at 09:20 hours I served a search warrant at {apt.name} unit 14 "
            f"({apt.address}) with ET Lowell. The gray {civic.year} {civic.make} {civic.model} "
            f"{civic.plate} was seized from garage space 14 at 09:35 hours (ITEM-023). The Fuel & "
            "Go receipt for 22:30:58 was in the center console and a Halyard pack was in the "
            "passenger door pocket. In the bedroom closet we found an empty 9mm pistol box and a "
            "box of 9mm ammunition with 37 of 50 rounds, and in the desk a signed $18,000 loan "
            f"note. {kemper.name} was arrested in unit 14 at 10:20 hours. The pistol and the dark "
            "hooded jacket were not found."
        ),
    ]
    blocks += property_section(
        sv,
        [
            "ITEM-023",
            "ITEM-026",
            "ITEM-027",
            "ITEM-028",
            "ITEM-029",
            "ITEM-030",
            "ITEM-031",
            "ITEM-032",
        ],
        title="Property seized",
    )
    blocks += [
        Heading("Interview summary"),
        Para(
            f"On 03/16 at 14:10 hours I interviewed {kemper.name} in PCPD headquarters interview "
            "room 2 after a Miranda advisement; the interview was recorded (ITEM-036). Kemper "
            "admitted he owed the victim $18,000 and that 'we argued about the money'. He "
            "admitted he drove to the Riverside Lot to meet the victim. He stated: 'I was by "
            "myself. I never even got out of the car.' He said he left 'before 10:40'."
        ),
        Para(
            "Kemper's account is contradicted by the evidence. CAM-A shows two occupants get out "
            "of the gray sedan at 22:42:10 and the car leave the lot at 22:47:30, and ALPR reads "
            "it exiting at 22:47:31. The Fuel & Go video shows a passenger in the Civic who paid "
            f"at the register at 22:30:58; the passenger matches {vance.name}, Kemper's cousin, "
            "who is also seen leaving the Larkspur garage at 23:05:40."
        ),
    ]
    blocks += persons_section(
        sv,
        [
            ("S-1", "p_suspect", "Suspect, arrested 03/16"),
            ("A-1", "p_associate", "Passenger, Kemper's cousin"),
        ],
    )
    blocks += vehicles_section(sv, [("VEH-1", "v_gray_sedan", "Seized 03/16, impound yard")])
    blocks.append(officer_signoff(sv, "o_det_lead", stamp(plan.recorded_at)))
    facts = [
        ("ev_16", "pays $14.62 cash for two Volt Surge energy drinks"),
        ("ev_25", "shows the Civic enter the garage at 23:03:15 hours"),
        ("ev_29", "at 09:20 hours I served a search warrant"),
        ("ev_30", "I never even got out of the car"),
        ("cf_02", "two occupants get out of the gray sedan at 22:42:10"),
        ("cf_03", "The Fuel & Go video shows a passenger in the Civic"),
        ("p_suspect", "was arrested in unit 14 at 10:20 hours"),
        ("p_associate", f"A passenger matching {vance.name}"),
        ("v_gray_sedan", f"{civic.plate} was seized from garage space 14"),
        ("loc_kemper_apartment", f"search warrant at {apt.name} unit 14"),
        ("loc_fuel_stop", f"collected the {fuel.name}"),
    ]
    return _spec(sv, "rpt_supp_03", "SUPPLEMENTAL REPORT 3", blocks, facts=facts)


def _statement_fields(sv: StoryView, plan_id: str, place: str, address: str, phone: str) -> Fields:
    plan = sv.plan_item(plan_id)
    subject = sv.person(plan.subject_ids[0])
    taker = sv.person(plan.author_id or "")
    return Fields(
        [
            ("Name", subject.name),
            ("Age", str(subject.age)),
            ("Occupation", subject.occupation or "-"),
            ("Address", address),
            ("Phone", phone),
            ("Date / time taken", stamp(plan.recorded_at)),
            ("Place taken", place),
            ("Taken by", officer_label(taker)),
        ]
    )


def _attestation(sv: StoryView, plan_id: str, pages: int) -> list[Block]:
    plan = sv.plan_item(plan_id)
    subject = sv.person(plan.subject_ids[0])
    taker = sv.person(plan.author_id or "")
    return [
        Para(
            f"I have read this statement of {pages} page{'s' if pages > 1 else ''}. It is true "
            "and correct to the best of "
            "my knowledge. I gave it voluntarily. No threats or promises were made to me.",
            style="small",
        ),
        Signature(
            [
                (f"Signature of person giving statement ({subject.name})", subject.name),
                (
                    f"Witnessing officer: {officer_label(taker)}  {stamp(plan.recorded_at)}",
                    f"/s/ {taker.name}",
                ),
            ]
        ),
    ]


def ws_ruiz(sv: StoryView) -> DocSpec:
    kl = [line.text for line in sv.plan_item("ws_ruiz").key_lines]
    market = sv.locations["loc_riverside_market"]
    ruiz = sv.person("p_witness_ruiz")
    blocks: list[Block] = [
        _statement_fields(
            sv,
            "ws_ruiz",
            f"{market.name}, front counter",
            "On file (Port Calder, fictional)",
            pretty_phone(sv.phone("ph_witness_ruiz").number),
        ),
        Heading("Statement"),
        Para(
            f"My name is {ruiz.name}. I work the 4 PM to midnight shift as the night clerk at "
            f"{market.name}. The front windows look right out at the Riverside Lot. Tonight I was "
            "at the register. There was a white truck parked in the lot since around 10, in the "
            "middle row where the light is out."
        ),
        Para(
            f"{kl[0]} I know it was 10:41 because I had just rung up a customer and I looked at "
            "the register clock. It was a gray Honda, a Civic, it has black rims. I see a lot of "
            "cars out that window."
        ),
        Para(
            f"{kl[1]} The driver walked over to the man at the back of the white truck. The tall "
            "one stayed by the back of the car. I couldn't hear them through the glass but it "
            "looked like they were arguing, the driver was moving his hands a lot."
        ),
        Para(
            f"{kl[2]} Right out of the lot onto Calder River Road. I didn't look at the clock when "
            "I heard the pop, it was a few minutes after they pulled in. I didn't see a plate."
        ),
        PageBreak(),
        Heading("Statement (continued)"),
        Para(
            f"{kl[3]} He was lying between the truck and the empty space. He wasn't talking. I "
            "told the operator a man was shot and a gray car, like a Civic, left. That was about "
            "10:48. I stayed with him until the officer came."
        ),
        Para(
            "I did not know the man. I have seen the white truck in the lot before. I did not see "
            "anybody else in the lot after the gray car left."
        ),
    ]
    blocks += _attestation(sv, "ws_ruiz", 2)
    facts = [
        ("ev_18", kl[0]),
        ("ev_19", "Two guys got out"),
        ("ev_20", "I heard one pop, like a firecracker"),
        ("ev_22", "the gray Civic went out fast with no lights and turned right"),
        ("ev_23", kl[3]),
        ("cf_04", "the gray Civic pulled in and parked right next to the white truck"),
        ("p_witness_ruiz", f"My name is {ruiz.name}"),
        ("v_gray_sedan", "It was a gray Honda, a Civic"),
        ("v_victim_pickup", "There was a white truck parked in the lot"),
        ("loc_riverside_market", f"night clerk at {market.name}"),
    ]
    return _spec(sv, "ws_ruiz", "VOLUNTARY WITNESS STATEMENT", blocks, facts=facts)


def ws_darnell(sv: StoryView) -> DocSpec:
    kl = [line.text for line in sv.plan_item("ws_darnell").key_lines]
    darnell = sv.person("p_red_herring")
    hale = sv.person("p_victim")
    camry = sv.vehicle("v_blue_camry")
    phone = sv.phone("ph_red_herring")
    blocks: list[Block] = [
        _statement_fields(
            sv,
            "ws_darnell",
            "Darnell residence, driveway (Port Calder, fictional)",
            "On file (Port Calder, fictional)",
            pretty_phone(phone.number),
        ),
        Heading("Statement"),
        Para(
            f"My name is {darnell.name}. I drive a forklift at the marina. {hale.name} is married "
            "to my sister Carla, so he's my brother-in-law. Carla is out of town this week."
        ),
        Para(
            f"{kl[0]} I called Marc around 7:40 that night and told him I'm done waiting, Marc. We "
            "went back and forth for a few minutes. I was mad, I admit it."
        ),
        Para(
            "Around 10 he called me back and said 'Come get your hitch, I'm at the river lot.' So "
            f"I drove my {camry.color} {camry.make} {camry.model} over there. I got to the lot "
            "about ten after ten and pulled in behind his truck."
        ),
        Para(
            "We shouted at each other by the back of his truck for a few minutes. I poked my "
            "finger at him, that's all, I never touched him. I lifted the hitch out of his truck "
            f"bed and put it in my trunk. {kl[1]} I left before 10:20. I probably spun the tires "
            "going out, I was still hot about it."
        ),
        Para(
            "I drove straight home, east on Route 9, past the Fuel & Go at Marsh Road. I was home "
            "before 10:40 and I stayed home the rest of the night. My wife can tell you that."
        ),
        Para(
            f"{kl[2]} I didn't know anything had happened until the detective came to my house. I "
            "gave the detective the hitch today. I don't own a gun. You can look at my phone."
        ),
    ]
    blocks += _attestation(sv, "ws_darnell", 1)
    facts = [
        ("ev_04", "I called Marc around 7:40 that night"),
        ("ev_09", "Come get your hitch, I'm at the river lot"),
        ("ev_10", "pulled in behind his truck"),
        ("ev_11", "We shouted at each other by the back of his truck"),
        ("ev_12", "I left before 10:20"),
        ("ev_15", "I drove straight home, east on Route 9"),
        ("ev_21", kl[2]),
        ("p_red_herring", f"My name is {darnell.name}"),
        ("p_victim", f"{hale.name} is married to my sister"),
        ("v_blue_camry", f"{camry.color} {camry.make} {camry.model}"),
        ("ph_red_herring", pretty_phone(phone.number)),
    ]
    return _spec(sv, "ws_darnell", "VOLUNTARY WITNESS STATEMENT", blocks, facts=facts)


def ws_whitfield(sv: StoryView) -> DocSpec:
    """The deliberately inconsistent statement (conflict cf_01): wrong time, wrong car color."""
    kl = [line.text for line in sv.plan_item("ws_whitfield").key_lines]
    whit = sv.person("p_witness_whitfield")
    claim = next(c for c in sv.story.conflicts if c.id == "cf_01").claim_a.text
    blocks: list[Block] = [
        _statement_fields(
            sv,
            "ws_whitfield",
            "PCPD headquarters, interview room 1",
            "On file (Port Calder, fictional)",
            "On file",
        ),
        Heading("Statement"),
        Para(
            f"My name is {whit.name}. I work in the laundry at the hospital. On Saturday night I "
            "was waiting for the Route 9 bus at the shelter on the side of the Riverside Lot. I "
            "got there a little after 10."
        ),
        Para(
            f"{kl[0]} {kl[1]} One of the men was big and bald with an orange vest, the kind the "
            "dock workers wear. I couldn't see the other one good, he was by the truck."
        ),
        Para(f"{claim} That's what I saw. It went right past the shelter, fast."),
        Para(
            f"{kl[2]} The next day my sister called me and said somebody got shot at the river "
            "lot, so I called the tip line. I want to help. I'm sure about the time because of my "
            "bus."
        ),
    ]
    blocks += _attestation(sv, "ws_whitfield", 1)
    facts = [
        ("ev_11", "Two men were shouting by a white truck"),
        ("ev_12", "a blue car took off out of the lot, tires squealing"),
        ("ev_13", "I got on my bus"),
        ("cf_01", claim),
        ("p_witness_whitfield", f"My name is {whit.name}"),
        ("v_blue_camry", "a blue car took off out of the lot"),
        ("loc_riverside_lot", "the shelter on the side of the Riverside Lot"),
    ]
    return _spec(sv, "ws_whitfield", "VOLUNTARY WITNESS STATEMENT", blocks, facts=facts)


def ws_pike(sv: StoryView) -> DocSpec:
    """Handwritten-style statement; rendered digitally, then scanned to an image-only PDF."""
    kl = [line.text for line in sv.plan_item("ws_pike").key_lines]
    pike = sv.person("p_witness_pike")
    blocks: list[Block] = [
        _statement_fields(
            sv,
            "ws_pike",
            "Pike residence, Alder Row (Port Calder, fictional)",
            "Alder Row, Port Calder (fictional)",
            "On file",
        ),
        Para(
            f"I am {pike.name}, I live on Alder Row right behind the Riverside Lot. I walk my "
            "beagle Biscuit along the lot fence every night around 10:30 before bed. Saturday "
            "night was the same.",
            style="hand",
        ),
        Para(
            "I was at the chain-link fence by the middle of the lot where the light is broken. "
            "There was a white pickup and a car next to it. Two men were by the back of the "
            "pickup "
            "and another fellow was standing back by the car. They were arguing loud.",
            style="hand",
        ),
        Para(kl[0], style="hand"),
        Para(f"{kl[1]} I got down low by the fence. I am not ashamed to say it.", style="hand"),
        PageBreak(),
        Para(f"{kl[2]} The car went right on Calder River Road toward town.", style="hand"),
        Para(
            f"{kl[3]} I went home. The detective came by this morning and I told him all of it.",
            style="hand",
        ),
        Para("Signed this morning. Gerald Pike.", style="hand"),
    ]
    blocks += _attestation(sv, "ws_pike", 2)
    facts = [
        ("ev_19", "You'll get it when I have it"),
        ("ev_20", "Then one loud bang"),
        ("ev_22", "went out with no headlights"),
        ("cf_04", "A gray or silver four-door, maybe a Toyota"),
        ("p_witness_pike", f"I am {pike.name}"),
        ("v_gray_sedan", "The plate started SYN 48"),
        ("loc_riverside_lot", "right behind the Riverside Lot"),
    ]
    return _spec(sv, "ws_pike", "VOLUNTARY WITNESS STATEMENT (HANDWRITTEN)", blocks, facts=facts)


BUILDERS: dict[str, Callable[[StoryView], DocSpec]] = {
    "rpt_incident": rpt_incident,
    "rpt_supp_01": rpt_supp_01,
    "rpt_supp_02": rpt_supp_02,
    "rpt_supp_03": rpt_supp_03,
    "ws_ruiz": ws_ruiz,
    "ws_darnell": ws_darnell,
    "ws_whitfield": ws_whitfield,
    "ws_pike": ws_pike,
}
