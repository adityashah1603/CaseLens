"""Reusable report sections built straight from story data (persons, vehicles, property)."""

from __future__ import annotations

from caselens_data_gen.documents.facts import StoryView, officer_label, pretty_phone, stamp
from caselens_data_gen.documents.layout import Block, Fields, Heading, Signature, Table


def persons_section(sv: StoryView, rows: list[tuple[str, str, str]]) -> list[Block]:
    """rows: (code, person id, involvement)."""
    phones = {ph.owner_id: ph.number for ph in sv.story.phones if ph.owner_id}
    table_rows = []
    for code, pid, involvement in rows:
        p = sv.person(pid)
        contact = pretty_phone(phones[pid]) if pid in phones else "-"
        table_rows.append(
            [
                code,
                p.name,
                str(p.age) if p.age is not None else "-",
                involvement,
                p.occupation or "-",
                contact,
            ]
        )
    return [
        Heading("Persons"),
        Table(
            ["Code", "Name", "Age", "Involvement", "Occupation", "Phone"],
            table_rows,
            [7, 17, 6, 16, 32, 15],
        ),
    ]


def vehicles_section(sv: StoryView, rows: list[tuple[str, str, str]]) -> list[Block]:
    """rows: (code, vehicle id, status)."""
    table_rows = []
    for code, vid, status in rows:
        v = sv.vehicle(vid)
        owner = sv.person(v.owner_id).name
        table_rows.append(
            [
                code,
                f"{v.year} {v.make} {v.model}",
                v.color,
                f"{v.plate} ({v.plate_state})",
                owner,
                status,
            ]
        )
    return [
        Heading("Vehicles"),
        Table(
            ["Code", "Year / make / model", "Color", "Plate", "Registered owner", "Status"],
            table_rows,
            [7, 20, 8, 14, 16, 35],
        ),
    ]


def property_section(
    sv: StoryView, item_ids: list[str], title: str = "Property / evidence"
) -> list[Block]:
    table_rows = []
    for item_id in item_ids:
        item = sv.item(item_id)
        table_rows.append(
            [
                item.item_id,
                item.description,
                item.location_detail,
                sv.holders[item.collected_by],
                stamp(item.collected_at),
            ]
        )
    return [
        Heading(title),
        Table(
            ["Item", "Description", "Where collected", "Collected by", "Date / time"],
            table_rows,
            [9, 30, 27, 20, 14],
        ),
    ]


def report_fields(pairs: list[tuple[str, str]]) -> Fields:
    return Fields(pairs)


def officer_signoff(sv: StoryView, author_id: str, when: str) -> Signature:
    author = sv.person(author_id)
    return Signature(
        [
            (
                f"Reporting officer: {officer_label(author)} - submitted {when}",
                f"/s/ {author.name}",
            ),
        ]
    )
