"""Dialogue scripts for the SYNTHETIC audio evidence (911 call and suspect interview).

The lines are authored from the story's evidence plan (content notes, key lines, covered events).
Each turn lists the story fact ids (events, entities, conflicts) it states, so the generator can
emit exact audio locators for the eval answer key.
"""

from __future__ import annotations

from dataclasses import dataclass

from caselens_data_gen.story import Story

NOTICE_SPEAKER = "synthetic_notice"
NOTICE_LINE = "This is a synthetic recording for the CaseLens demo."


@dataclass(frozen=True)
class VoiceSpec:
    """A Piper voice plus a speaking-rate tweak (length_scale < 1 is faster)."""

    voice: str
    length_scale: float = 1.0
    gain: float = 0.6


@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str
    facts: tuple[str, ...] = ()
    pause_weight: float = 1.0
    """Relative length of the silence before this turn."""


@dataclass(frozen=True)
class RecordingScript:
    evidence_id: str
    title: str
    target_ms: int
    voices: dict[str, VoiceSpec]
    turns: list[Turn]
    background: str
    """'phone' (street noise, band-limited line) or 'room' (interview room tone)."""
    siren_before_turn: int | None = None
    """Index of the turn the siren becomes audible before (911 only)."""


NOTICE_VOICE = VoiceSpec("en_US-lessac-medium", 1.0, 0.5)


def _t(speaker: str, text: str, *facts: str, pause: float = 1.0) -> Turn:
    return Turn(speaker=speaker, text=text, facts=tuple(facts), pause_weight=pause)


def _call_911(story: Story) -> RecordingScript:
    plan = next(p for p in story.evidence_plan if p.id == "aud_911")
    d, c = "d_dispatch", "p_witness_ruiz"
    turns = [
        _t(NOTICE_SPEAKER, NOTICE_LINE),
        _t(d, "911, what is the address of your emergency?", "d_dispatch"),
        _t(
            c,
            "The Riverside Lot, by Riverside Market on Calder River Road, a man's been shot!",
            "ev_23",
            "loc_riverside_lot",
            "loc_riverside_market",
            pause=0.5,
        ),
        _t(
            d,
            "Okay ma'am, the Riverside Lot on Calder River Road, next to Riverside Market. "
            "Is that the twelve hundred block?",
        ),
        _t(c, "Yes, yes, twelve fourteen is the market. The lot is right next to it.", pause=0.5),
        _t(d, "Tell me exactly what happened."),
        _t(
            c,
            "I heard a pop, like a gunshot, a few minutes ago. I came out and he's on the ground "
            "by a white truck.",
            pause=0.5,
        ),
        _t(d, "Is he breathing?"),
        _t(c, "I don't think so, there's blood, he's not moving.", pause=0.6),
        _t(d, "Is the person who did this still there?"),
        _t(c, "No, no, they're gone. They left.", pause=0.5),
        _t(d, "Okay. Which way did they go?"),
        _t(
            c,
            "A gray car, like a Civic, it just left, it went right on Calder River.",
            "v_gray_sedan",
            pause=0.6,
        ),
        _t(
            d,
            "A gray car, possibly a Honda Civic, going right on Calder River Road. "
            "Did you get a license plate?",
        ),
        _t(c, "No, I couldn't see it. It didn't have its lights on.", pause=0.6),
        _t(d, "Did you see who was in the car?"),
        _t(c, "Two men, I think. One had a dark hoodie.", pause=0.8),
        _t(d, "Two men, one in a dark hoodie. Anything else about them?"),
        _t(c, "The other one was taller. I only saw them through the store window.", pause=0.6),
        _t(d, "Officers are on the way, stay on the line with me."),
        _t(c, "Okay. Okay.", pause=0.5),
        _t(d, "What is your name?"),
        _t(c, "Elena Ruiz. I work at Riverside Market.", "p_witness_ruiz", pause=0.5),
        _t(d, "Elena, is the number you're calling from a good number to reach you?"),
        _t(
            c,
            "Yes, it's my cell. Three six zero, five five five, zero one two nine.",
            "ph_witness_ruiz",
            pause=0.6,
        ),
        _t(
            d,
            "Thank you, Elena. Don't touch anything around him, and don't move him "
            "unless he's in danger.",
        ),
        _t(c, "He's not moving. I'm right next to him. Should I do something?", pause=0.6),
        _t(d, "Help is almost there. Watch for the officers and wave them in when they arrive."),
        _t(c, "I hear the sirens now.", pause=1.6),
        _t(d, "Good. Stay where they can see you, and keep your hands visible."),
        _t(c, "I can see the lights coming down Calder River Road.", pause=0.6),
        _t(d, "Okay, Elena. Wave them in. The officers will take it from here."),
    ]
    siren = next(i for i, t in enumerate(turns) if t.text == "I hear the sirens now.")
    return RecordingScript(
        evidence_id=plan.id,
        title=plan.title,
        target_ms=(plan.duration_s or 128) * 1000,
        voices={
            NOTICE_SPEAKER: NOTICE_VOICE,
            d: VoiceSpec("en_US-amy-medium", 1.0, 0.55),
            c: VoiceSpec("en_US-hfc_female-medium", 0.88, 0.7),
        },
        turns=turns,
        background="phone",
        siren_before_turn=siren,
    )


def _interview(story: Story) -> RecordingScript:
    plan = next(p for p in story.evidence_plan if p.id == "aud_interview")
    b, k = "o_det_lead", "p_suspect"
    turns = [
        _t(NOTICE_SPEAKER, NOTICE_LINE),
        _t(
            b,
            "This is Detective Samuel Brandt, March 16th, 2:10 PM, interview with Travis Kemper.",
            "ev_30",
            "p_suspect",
        ),
        _t(
            b,
            "We're in interview room two at Port Calder police headquarters, and this "
            "conversation is being recorded. Mr. Kemper, you have the right to remain silent. "
            "Anything you say can be used against you in a court of law. You have the right to "
            "an attorney, and if you can't afford one, one will be appointed for you. "
            "Do you understand these rights?",
            pause=0.6,
        ),
        _t(k, "Yeah. I understand."),
        _t(b, "With those rights in mind, are you willing to talk to me today?"),
        _t(k, "Sure. I don't have anything to hide."),
        _t(b, "Tell me how you know Marcus Hale."),
        _t(
            k,
            "Marc? We had a detailing business together. Cars, boats, that kind of thing. "
            "It went under last year.",
            "p_victim",
        ),
        _t(b, "And he lent you money when it went under."),
        _t(k, "Eighteen thousand dollars. I signed a note for it. I was going to pay him back."),
        _t(
            b,
            "Phone records show a fourteen minute call between your phone and his on March 9th, "
            "just after seven in the evening. What was that about?",
        ),
        _t(
            k,
            "Marc and me argued about the money, sure. Everybody knew he was on me about it.",
            "ev_02",
            pause=1.3,
        ),
        _t(
            b,
            "He threatened to take you to small claims court. He said he'd tell your new "
            "employer about the debt.",
        ),
        _t(k, "He said a lot of things when he was mad. I didn't take it serious."),
        _t(b, "Is three six zero, five five five, zero one one seven your cell phone?"),
        _t(k, "Yeah, that's my number.", "ph_suspect"),
        _t(
            b,
            "Then on Friday night, the 13th, around ten o'clock, you called him again. "
            "Seven minutes.",
        ),
        _t(
            k,
            "I told him I could bring him part of it. We said Saturday night at the river lot, "
            "around ten thirty.",
            "ev_03",
        ),
        _t(b, "The Riverside Lot, on Calder River Road.", "loc_riverside_lot"),
        _t(k, "Yeah. That lot."),
        _t(b, "So Saturday, March 14th. Walk me through your night."),
        _t(
            k,
            "I drove over there to give him some cash, he wasn't there, I left. Before 10:40.",
            "cf_02",
            "ev_19",
            pause=1.5,
        ),
        _t(b, "What were you driving?"),
        _t(k, "My car. The gray Civic.", "v_gray_sedan"),
        _t(b, "Plate S Y N, four eight two one."),
        _t(k, "If you say so. It's my car."),
        _t(b, "Was anybody with you that night?"),
        _t(
            k,
            "I was by myself. I never even got out of the car.",
            "cf_02",
            "cf_03",
            "ev_19",
            pause=1.6,
        ),
        _t(b, "You never got out. Not once."),
        _t(k, "No. I pulled in, I looked around, I didn't see Marc, and I went home."),
        _t(b, "His white pickup was parked in that lot from just after ten. Hard to miss."),
        _t(k, "It was dark. I wasn't looking that hard.", pause=1.5),
        _t(b, "Did you stop anywhere before the lot?"),
        _t(k, "I don't remember. Maybe I got gas. I get gas all the time."),
        _t(b, "Here's my problem, Travis.", pause=1.5),
        _t(
            b,
            "Your car was read leaving that lot at 10:47 PM. And who bought the energy drinks "
            "at Fuel and Go?",
            pause=0.4,
        ),
        _t(k, "I don't know what you're talking about.", pause=1.5),
        _t(
            b,
            "The camera at Fuel and Go shows a tall man with red hair and a gray cap getting "
            "out of your passenger seat. He pays cash for two energy drinks and a pack of "
            "cigarettes. The receipt was in your car when we searched it this morning.",
        ),
        _t(k, "That could be anybody.", pause=2.0),
        _t(b, "It's your cousin, isn't it? Rory Vance.", "p_associate"),
        _t(k, "I'm not saying anything about Rory.", pause=2.0),
        _t(b, "You told me you were by yourself."),
        _t(k, "I was by myself at the lot."),
        _t(
            b,
            "The video from the market shows two people getting out of your car at 10:42. "
            "One of them walks over to Marcus at the back of his truck.",
        ),
        _t(k, "Video can be wrong. It was dark.", pause=1.5),
        _t(
            b,
            "Marcus Hale was shot once, at close range, at 10:44. Three minutes later your "
            "Civic leaves that lot with its headlights off.",
        ),
        _t(k, "I didn't shoot anybody.", pause=3.0),
        _t(
            b,
            "Then help me understand what happened in that lot, Travis. "
            "This is your chance to tell it your way.",
        ),
        _t(k, "I think I want a lawyer.", pause=4.0),
        _t(
            b,
            "Okay. You've asked for an attorney, so we're going to stop here. "
            "The time is 2:15 PM. This interview is concluded.",
        ),
    ]
    return RecordingScript(
        evidence_id=plan.id,
        title=plan.title,
        target_ms=(plan.duration_s or 300) * 1000,
        voices={
            NOTICE_SPEAKER: NOTICE_VOICE,
            b: VoiceSpec("en_US-ryan-medium", 1.0, 0.6),
            k: VoiceSpec("en_US-joe-medium", 1.08, 0.6),
        },
        turns=turns,
        background="room",
    )


_AUTHORED = {
    ("riverside_lot", "aud_911"): _call_911,
    ("riverside_lot", "aud_interview"): _interview,
}


def build_scripts(story: Story) -> list[RecordingScript]:
    """Return the authored script for every audio item in the story's evidence plan."""
    scripts: list[RecordingScript] = []
    for item in story.evidence_plan:
        if item.type != "audio":
            continue
        author = _AUTHORED.get((story.case_slug, item.id))
        if author is None:
            raise ValueError(f"no audio script authored for {story.case_slug}/{item.id}")
        scripts.append(author(story))
    return scripts
