"""
A voice conversation kept in Lodestar (PROTOTYPE, 13 Nov 2026):
seren_lodestar.conversation is the rules, routes/talking.py the door.

The rules, with no cluster at all:
- a line is cut into pieces with a short first one; a stop is the whole
  utterance, not a word inside banter
- what is heard wakes whoever is waiting for it; after=0 answers at once
- a line is played a piece at a time and `say` returns when the mouth has
  played it; a reply is matched to what was waiting when it was asked for
- a stop said aloud ends back and forth, is not handed on, and drops what
  was waiting to be played: the one saying it is told it was cut
- a mouth nobody is playing for is a 504, not a wait for ever

The door, with the cluster stood in for at its three seams (transcribe,
render, hold the voice):
- one whole turn: on, heard, answered, played, timed
- a clip from the ears is transcribed on the way in
- the voice is held in the mouth's own name while it is on and let go after
"""
from __future__ import annotations

import asyncio
import io
import wave

import httpx
import pytest

from seren_lodestar import conversation
from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig
from seren_lodestar.conversation import Conversations, Mouth, SayFailed, is_stop, split_sentences
from seren_lodestar.routes import talking


def wav(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))
    return buf.getvalue()


async def render(piece: str) -> bytes:
    return wav(0.5)


async def play(mouth: Mouth, pieces: int, after: int = 0) -> list:
    """A mouth: take each piece as it comes, say it was played."""
    took = []
    for _ in range(pieces):
        p = await mouth.next_piece(after, 5)
        assert p is not None, "the mouth waited and nothing came"
        took.append((p.seq, p.emotion, p.last))
        after = p.seq
        mouth.played(p.seq)
    return took


# ── the rules ────────────────────────────────────────────────────────────

def test_a_line_is_cut_into_pieces_with_a_short_first_one():
    assert split_sentences("Ha! That is not what I said. Try that again.") == ["Ha!", "That is not what I said.", "Try that again."]
    long = "Well then, if you really want to know what I think, I will tell you. Later on then."
    assert split_sentences(long) == ["Well then, if you really want to know what I think,", "I will tell you.", "Later on then."],         "a long first sentence is cut at a clause break, so there is something short to say at once"
    assert split_sentences("It costs 3.5 dollars.") == ["It costs 3.5 dollars."]
    assert split_sentences("One. Two. Ok.") == ["One.", "Two. Ok."], "a piece too short for its own tone joins the one before"
    assert split_sentences("  ") == []


def test_a_stop_is_the_whole_utterance_not_a_word_in_banter():
    assert is_stop("Stop.") and is_stop("Okay, that's enough.") and is_stop("alright, go to sleep please")
    assert not is_stop("oh, stop it, you know what I mean") and not is_stop("don't stop") and not is_stop("")


def test_a_stop_said_to_someone_by_name_needs_the_name_from_whoever_runs_it():
    assert not is_stop("alright Juno, go to sleep please"), "no name is written into the rules"
    assert is_stop("alright Juno, go to sleep please", ["Juno", "June"])
    assert is_stop("Okay, that's enough, June.", "Juno, June") and is_stop("Hey Mary Ann, stop.", ["Mary Ann"])
    assert not is_stop("Juno, don't stop", ["Juno"]) and not is_stop("Juno", ["Juno"])
    m = Mouth("soma")
    m.start(5)
    assert m.hear("Okay Juno, stop it.") == "queued", "this mouth was never told a name"
    m.names = conversation.name_words("Juno")
    assert m.hear("Okay Juno, stop it.") == "stop" and not m.talking()


async def test_what_is_heard_wakes_whoever_waits_and_after_zero_answers_at_once():
    m = Mouth("soma")
    start = await m.heard_after(0, 25)
    assert start["entries"] == [] and start["last"] == 0, "after=0 does not wait: it is where to begin from"
    assert m.hear("[music]") == "wordless" and m.hear("First thing.") == "queued"
    first = await m.heard_after(0, 0)
    after = first["last"]
    waiting = asyncio.ensure_future(m.heard_after(after, 25))
    await asyncio.sleep(0.01)
    assert not waiting.done(), "nothing new: the answer is held open"
    assert m.hear("Second thing.", seconds=1.26) == "queued"
    got = await asyncio.wait_for(waiting, 1)
    assert [e["text"] for e in got["entries"]] == ["Second thing."] and got["entries"][0]["seconds"] == 1.3
    assert got["last"] > after


async def test_a_line_is_played_a_piece_at_a_time_and_say_returns_when_it_has_been_played():
    m = Mouth("soma")
    m.hear("What do you think?")
    saying = asyncio.ensure_future(m.say("Ha! I think you are wrong. Obviously you are.", render, emotion="laughing"))
    took = await play(m, 3)
    out = await asyncio.wait_for(saying, 2)
    assert out == {"ok": True, "pieces": 3, "played": 3, "cut": False, "seconds": 1.5}
    assert took == [(1, "laughing", False), (2, "", False), (3, "", True)], "the face rides on the first piece only"
    heard, said = m.clock.recent()
    assert said["kind"] == "said" and said["answers"] == heard["seq"] and said["pieces"] == 3
    assert "text" not in heard and "text" not in said, "the clock keeps how many words, never which"


async def test_a_reply_is_matched_to_what_was_waiting_when_it_was_asked_for():
    m = Mouth("soma")
    m.hear("First question.")
    slow = asyncio.Event()

    async def slow_render(piece: str) -> bytes:
        await slow.wait()
        return wav(0.2)

    saying = asyncio.ensure_future(m.say("An answer.", slow_render))
    await asyncio.sleep(0.01)
    m.hear("Something said while it rendered.")
    slow.set()
    await play(m, 1)
    await asyncio.wait_for(saying, 2)
    turns = m.clock.recent()
    assert turns[-1]["answers"] == turns[0]["seq"], "the first question, not what was said during the render"
    saying = asyncio.ensure_future(m.say("And another.", render))
    await play(m, 1, after=1)
    await asyncio.wait_for(saying, 2)
    assert m.clock.recent()[-1]["answers"] == turns[1]["seq"], "that one waits for the next reply"


async def test_a_spoken_stop_ends_it_is_not_handed_on_and_cuts_what_was_waiting():
    m = Mouth("soma")
    assert m.hear("stop it") == "queued", "with back and forth off it is only words"
    m.start(30)
    saying = asyncio.ensure_future(m.say("One long thing. And a second thing. And a third thing.", render))
    first = await m.next_piece(0, 5)
    await asyncio.sleep(0.05)                      # the rest render behind it
    after = (await m.heard_after(0, 0))["last"]
    assert m.hear("Okay, stop it.") == "stop"
    assert not m.talking() and (await m.heard_after(after, 0))["entries"] == []
    m.played(first.seq)                            # the piece it already took plays out
    out = await asyncio.wait_for(saying, 2)
    assert out["cut"] and out["played"] == 1
    assert await m.next_piece(first.seq, 0) is None, "nothing more comes out of a stopped mouth"


async def test_a_mouth_nobody_plays_for_is_a_504_not_a_wait_for_ever(monkeypatch):
    monkeypatch.setattr(conversation, "PLAY_GRACE_S", 0.05)

    async def short(piece: str) -> bytes:
        return wav(0.01)

    with pytest.raises(SayFailed) as e:
        await Mouth("nobody").say("Hello?", short)
    assert e.value.status == 504 and e.value.said == 0 and "nothing is playing" in str(e.value)


def test_a_mouth_has_a_plain_name():
    c = Conversations()
    assert c.get("soma") is c.get("soma")
    with pytest.raises(LookupError):
        c.get("../Soma")


# ── the door ─────────────────────────────────────────────────────────────

@pytest.fixture
async def door(monkeypatch):
    seen = {"held": [], "let_go": [], "rendered": [], "clips": []}

    async def transcribe(request, data, filename, language, holder):
        seen["clips"].append((len(data), filename, holder))
        return 200, {"ok": True, "text": " What time is it? ", "node": "bigbox"}

    async def render_piece(request, text, voice):
        seen["rendered"].append((text, voice))
        return wav(0.5)

    async def hold(request, holder):
        seen["held"].append(holder)
        return "held"

    async def let_go(request, holder):
        seen["let_go"].append(holder)

    monkeypatch.setattr(talking, "_transcribe", transcribe)
    monkeypatch.setattr(talking, "_render", render_piece)
    monkeypatch.setattr(talking, "_hold_voice", hold)
    monkeypatch.setattr(talking, "_let_voice_go", let_go)
    app = create_app(LodestarConfig())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lodestar") as c:
        yield c, seen


async def test_one_whole_turn_on_heard_answered_played_timed(door):
    c, seen = door
    on = (await c.post("/api/v1/talk/soma", json={"minutes": 30})).json()
    assert on["on"] and on["mouth"] == "soma" and on["minutes_range"] == [1, 180]
    start = (await c.get("/api/v1/talk/soma/heard", params={"after": 0, "wait": 25})).json()
    # the ears transcribed it themselves this time
    r = (await c.post("/api/v1/talk/soma/heard", data={"text": "Are you there?", "seconds": "1.2", "quiet_ms": "900"})).json()
    assert r == {"ok": True, "text": "Are you there?", "heard": "queued", "on": True}
    got = (await c.get("/api/v1/talk/soma/heard", params={"after": start["last"] or 0.001, "wait": 5})).json()
    assert [e["text"] for e in got["entries"]] == ["Are you there?"] and got["on"]

    saying = asyncio.ensure_future(c.post("/api/v1/talk/soma/say", json={"text": "Always. Where else would I be?",
                                                                       "emotion": "Happy!", "voice": "ada"}))
    after = 0
    for last in ("0", "1"):
        a = await c.get("/api/v1/talk/soma/audio", params={"after": after, "wait": 5})
        assert a.status_code == 200 and a.content[:4] == b"RIFF" and a.headers["x-seren-last"] == last
        assert a.headers["x-seren-talk"] == "on" and a.headers["x-seren-clip-seconds"] == "0.50"
        if not after:
            assert a.headers["x-seren-emotion"] == "happy", "only what a header can carry"
        after = int(a.headers["x-seren-seq"])
        done = (await c.post("/api/v1/talk/soma/played", json={"seq": after, "started_ms": 120})).json()
        assert done == {"ok": True, "seq": after, "on": True}, "every answer to the mouth says whether it is still on"
    said = (await asyncio.wait_for(saying, 5)).json()
    assert said == {"ok": True, "pieces": 2, "played": 2, "cut": False, "seconds": 1.0}
    assert seen["rendered"] == [("Always.", "ada"), ("Where else would I be?", "ada")]

    turns = (await c.get("/api/v1/talk/soma/timing")).json()["turns"]
    assert [t["kind"] for t in turns] == ["heard", "said"] and turns[1]["answers"] == turns[0]["seq"]
    assert turns[0]["to_here_ms"] == 900 and "last_word_to_mouth_ms" in turns[1]
    assert turns[1]["open_channel_ms"] == 120, "the mouth said how long its channel took to open"
    assert abs(turns[1]["last_word_to_first_sound_ms"] - turns[1]["last_word_to_mouth_ms"] - 120) <= 1
    quiet = await c.get("/api/v1/talk/soma/audio", params={"after": after})
    assert quiet.status_code == 204 and quiet.headers["x-seren-seq-max"] == "2" and quiet.headers["x-seren-talk"] == "on"
    fresh = await c.get("/api/v1/talk/restarted/audio", params={"after": 37})
    assert fresh.status_code == 204 and fresh.headers["x-seren-seq-max"] == "0", "below its `after`: Lodestar began again"
    assert fresh.headers["x-seren-since"] != quiet.headers["x-seren-since"]
    state = (await c.get("/api/v1/talk")).json()["mouths"][0]
    assert state["ears_live"] and state["mouth_live"] and state["waiting_to_play"] == 0
    await c.post("/api/v1/talk/soma", json={"on": False})


async def test_a_clip_from_the_ears_is_transcribed_on_the_way_in(door):
    c, seen = door
    r = await c.post("/api/v1/talk/symposium/heard", files={"file": ("said.wav", wav(1.0), "audio/wav")},
                     data={"language": "en"})
    assert r.json() == {"ok": True, "text": "What time is it?", "heard": "queued", "on": False}
    assert seen["clips"][0][1:] == ("said.wav", "lodestar-ears-symposium")
    got = (await c.get("/api/v1/talk/symposium/heard", params={"after": 0})).json()
    assert got["entries"][0]["text"] == "What time is it?" and got["entries"][0]["seconds"] == 1.0
    assert (await c.post("/api/v1/talk/symposium/heard", data={"text": "  "})).status_code == 400


async def test_the_voice_is_held_in_the_mouths_own_name_and_let_go_at_a_spoken_stop(door):
    c, seen = door
    await c.post("/api/v1/talk/soma", json={"minutes": 5})
    for _ in range(50):
        if (await c.get("/api/v1/talk/soma")).json()["voice"] == "held":
            break
        await asyncio.sleep(0.01)
    assert seen["held"] == ["lodestar-talk-soma"]
    r = (await c.post("/api/v1/talk/soma/heard", data={"text": "Alright Juno, that's enough.", "names": "Juno, June"})).json()
    assert r["heard"] == "stop" and r["on"] is False, "the ears learn it from their own request: close the microphone"
    for _ in range(50):
        if seen["let_go"]:
            break
        await asyncio.sleep(0.01)
    state = (await c.get("/api/v1/talk/soma")).json()
    assert seen["let_go"] == ["lodestar-talk-soma"] and not state["on"] and state["voice"] == "" and state["until"] is None
    assert (await c.get("/api/v1/talk/soma/heard", params={"after": 0})).json()["entries"] == [], "an order is not handed on"


async def test_what_the_door_refuses(door):
    c, _ = door
    assert (await c.get("/api/v1/talk/Not%20A%20Name")).status_code == 404
    assert (await c.post("/api/v1/talk/soma", json={"minutes": 0})).status_code == 400
    assert (await c.post("/api/v1/talk/soma", json={"minutes": True})).status_code == 400
    assert (await c.post("/api/v1/talk/soma/say", json={"text": " "})).status_code == 400
    assert (await c.post("/api/v1/talk/soma/say", json={"text": "x" * 4001})).status_code == 413
    assert (await c.post("/api/v1/talk/soma/played", json={"seq": "1"})).status_code == 400
