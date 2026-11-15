"""
Talking - a voice conversation through the cluster (PROTOTYPE, 13 Nov 2026).

Hearing (`/hear`) is one clip to words and speaking (`/speak`) is words to
one clip; this is the back and forth between them, kept once for every
place it happens (seren_lodestar.conversation has the rules and the why).
A MOUTH is one such place: "soma" for the robot, "symposium" for a browser.
Three callers, three sets of routes on the same mouth:

  the ears (beside the microphone; they decide when somebody has finished):
    POST /api/v1/talk/{mouth}/heard     multipart: file (a finished WAV), language?, quiet_ms?, names?
                                        | form: text (ears that transcribe for themselves), seconds?
                                        names: what whoever answers is called, as said aloud ("Juno, June"),
                                        so a stop said to them by name is a stop; kept for this mouth
                                        -> {ok, text, heard: queued|stop|wordless, on}
                                        "stop": it was an order to end; close the microphone now

  whoever answers (a carabiner's talk session, through the Workbench):
    GET  /api/v1/talk/{mouth}/heard?after=<t>&wait=<s>   -> {entries: [{t, seconds, text}], last, now, on}
    POST /api/v1/talk/{mouth}/say       {text, emotion?, voice?, whole?}
                                        -> {ok, pieces, played, cut, seconds}, once it has been played

  the mouth (the thing with the loudspeaker):
    GET  /api/v1/talk/{mouth}/audio?after=<seq>&wait=<s>
                                        -> audio/wav, with X-Seren-Seq, X-Seren-Emotion, X-Seren-Last,
                                           X-Seren-Clip-Seconds | 204 (nothing yet); both with X-Seren-Talk,
                                           X-Seren-Seq-Max (the newest piece there is) and X-Seren-Since
                                           (when this mouth began here: it changes when Lodestar restarted)
    POST /api/v1/talk/{mouth}/played    {seq, started_ms?}  -> {ok, seq, on}: it finished playing that piece;
                                        started_ms is how long after taking it the sound began (a robot
                                        opens a channel first), wanted for a line's first piece
    A mouth keeps ONE /audio open all the time, while it is playing too: the
    next piece is then already there when this one ends, and it is the only
    way a mouth learns of a stop that came from somewhere else (a 204 with
    X-Seren-Talk: off: stop the sound, close the microphone).

  and anyone:
    GET  /api/v1/talk                   -> {ok, mouths: [state]}
    GET  /api/v1/talk/{mouth}           -> state: {mouth, on, until, voice, ears_live, mouth_live, ...}
    POST /api/v1/talk/{mouth}           {"minutes": 30, "names"?} on for that long (again, to extend) | {"on": false}
    GET  /api/v1/talk/{mouth}/timing    -> {turns}: how long each leg of the recent turns took; no words

WHILE BACK AND FORTH IS ON the speaker is held loaded in this mouth's own
name (`lodestar-talk-<mouth>`), on a lease that runs out by itself if
Lodestar dies. A name of its own because a line is rendered through /speak,
which lets its caller's lease go after every line.

THE CLOCK IS HERE AND ONLY HERE. The ears post the minutes when the person
starts it and keep no end time of their own; every answer to the ears and
the mouth carries whether it is on (`on`, or X-Seren-Talk), and "off" in any
of them means close the microphone. A Lodestar that restarted has forgotten
the conversation and answers off, so the microphone closes: the right way
for this to fail.

The microphone is never opened from here. Turning back and forth on says a
conversation may happen; who may open the microphone, the tap and the Stop
button stay with the ears.

/say works with back and forth off, on purpose: a line said out of nowhere
(a reminder, a greeting) is still a line.

NOBODY'S NAME IS IN HERE. What whoever answers is called comes from the
ears (`names`), and the voice a line is said in from whoever says it; with
neither, a stop is the bare phrase and the voice is the first one the
speaker lists.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from ..conversation import Conversations, Mouth, SayFailed, TALK_MINUTES, name_words, wav_seconds
from . import hearing, speaking
from .services import service_ensure, service_release

API_VERSION = "v1"
BASE = f"/api/{API_VERSION}/talk"
SAY_HOLDER = "lodestar-say"
TALK_LEASE_S = 150            # how long the speaker stays held if Lodestar dies mid-conversation
TALK_RENEW_S = 45
MAX_TEXT_CHARS = speaking.MAX_TEXT_CHARS

log = logging.getLogger("seren_lodestar.talking")
router = APIRouter(tags=["talking"])


def _conversations(request: Request) -> Conversations:
    c = getattr(request.app.state, "conversations", None)
    if c is None:
        c = request.app.state.conversations = Conversations()
    return c


def _mouth(request: Request, name: str) -> tuple[Optional[Mouth], Optional[JSONResponse]]:
    try:
        return _conversations(request).get(name), None
    except LookupError as e:
        return None, JSONResponse({"error": str(e)}, status_code=404)


# ── the cluster's side: one seam each, so a test can stand in for it ─────

async def _transcribe(request: Request, data: bytes, filename: str, language: str, holder: str) -> tuple[int, dict]:
    return await hearing.transcribe_clip(request, data, filename, language, holder)


async def _render(request: Request, text: str, voice: str) -> bytes:
    """One piece of text as a WAV, through /speak. Raises SayFailed."""
    # no voice asked for: /speak takes the first one the speaker lists
    r = await speaking.speak(request, speaking.SpeakBody(text=text, voice=voice, holder=SAY_HOLDER))
    if r.status_code != 200:
        try:
            why = json.loads(bytes(r.body)).get("error") or "the speaker could not say it"
        except ValueError:
            why = "the speaker could not say it"
        raise SayFailed(why, status=r.status_code)
    return bytes(r.body)


async def _hold_voice(request: Request, holder: str) -> str:
    """Keep the speaker loaded in `holder`'s name. "held", or why not."""
    up = await service_ensure(request, speaking.SERVICE, {"holder": holder, "reason": "back and forth",
                                                          "wait_seconds": speaking.WAKE_SECONDS,
                                                          "ttl_seconds": TALK_LEASE_S})
    return "held" if up.get("ok") and up.get("ready") else str(up.get("error") or "the speaker did not come up")


async def _let_voice_go(request: Request, holder: str) -> None:
    await service_release(request, speaking.SERVICE, {"holder": holder})


async def _keep(request: Request, mouth: Mouth) -> None:
    """While back and forth is on for this mouth: hold the voice. When it
    ends, by the clock or by being stopped, let it go."""
    holder = f"lodestar-talk-{mouth.name}"
    renew_at = 0.0
    try:
        while mouth.talking():
            if time.monotonic() >= renew_at:
                renew_at = time.monotonic() + TALK_RENEW_S
                try:
                    mouth.voice = await _hold_voice(request, holder)
                except Exception as e:  # noqa: BLE001 - talk goes on with a cold voice rather than not at all
                    mouth.voice = f"could not hold the speaker: {e or type(e).__name__}"
                if mouth.voice != "held":
                    log.info("talk %s: the voice is not held: %s", mouth.name, mouth.voice)
            await asyncio.sleep(1)
    finally:
        # Stopped and started again at once: a newer keeper holds the voice
        # now, and this one letting go would drop it from under that one.
        if mouth.keeper is None or mouth.keeper is asyncio.current_task():
            mouth.voice = ""
            try:
                await asyncio.shield(_let_voice_go(request, holder))
            except BaseException:  # noqa: BLE001 - the lease runs out by itself in TALK_LEASE_S
                pass
            log.info("talk %s: off", mouth.name)


def _stop(mouth: Mouth) -> None:
    mouth.stop()
    if mouth.keeper is not None and not mouth.keeper.done():
        mouth.keeper.cancel()
    mouth.keeper = None


# ── anyone ───────────────────────────────────────────────────────────────

@router.get(BASE)
async def talk_list(request: Request):
    return {"ok": True, "mouths": _conversations(request).states()}


@router.get(BASE + "/{mouth}")
async def talk_state(request: Request, mouth: str):
    m, err = _mouth(request, mouth)
    return err or m.state()


@router.post(BASE + "/{mouth}")
async def talk_set(request: Request, mouth: str, body: dict = Body(default={})):
    m, err = _mouth(request, mouth)
    if err:
        return err
    if body.get("on") is False:
        _stop(m)
        return m.state()
    minutes = body.get("minutes")
    lo, hi = TALK_MINUTES
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not lo <= minutes <= hi:
        return JSONResponse({"error": f'send {{"minutes": {lo} to {hi}}}, or {{"on": false}}'}, status_code=400)
    if body.get("names"):
        m.names = name_words(body.get("names"))
    m.start(minutes)
    if m.keeper is None or m.keeper.done():
        m.keeper = asyncio.ensure_future(_keep(request, m))
    log.info("talk %s: on for %s minutes", m.name, minutes)
    return m.state()


@router.get(BASE + "/{mouth}/timing")
async def talk_timing(request: Request, mouth: str):
    m, err = _mouth(request, mouth)
    return err or {"ok": True, "mouth": m.name, "turns": m.clock.recent()}


# ── the ears ─────────────────────────────────────────────────────────────

@router.post(BASE + "/{mouth}/heard")
async def talk_heard_in(request: Request, mouth: str, file: Optional[UploadFile] = File(None), text: str = Form(""),
                        language: str = Form(""), seconds: float = Form(0.0), quiet_ms: float = Form(0.0),
                        names: str = Form("")):
    """One finished utterance from this mouth's ears: a clip to transcribe,
    or the words when the ears transcribed it themselves. quiet_ms is how
    long ago the talking stopped, as the ears know it (the quiet they waited
    to call it finished), for the timing."""
    m, err = _mouth(request, mouth)
    if err:
        return err
    arrived = time.monotonic()
    if names.strip():
        m.names = name_words(names)
    if file is not None:
        data = await file.read()
        status, out = await _transcribe(request, data, file.filename or "clip.wav", language, f"lodestar-ears-{m.name}")
        if status != 200:
            m.clock.heard(speech_end=arrived - quiet_ms / 1000, arrived=arrived, done=time.monotonic(),
                          audio_s=wav_seconds(data), words=0, transcribed=False)
            return JSONResponse(out, status_code=status)
        text = str(out.get("text") or "")
        seconds = seconds or wav_seconds(data)
    elif not text.strip():
        return JSONResponse({"error": "send the clip as multipart `file`, or the words as `text`"}, status_code=400)
    outcome = m.hear(text, seconds, arrived=arrived, quiet_ms=quiet_ms)
    if outcome == "stop":
        _stop(m)
        log.info("talk %s: told to stop out loud", m.name)
    return {"ok": True, "text": text.strip(), "heard": outcome, "on": m.talking()}


# ── whoever answers ──────────────────────────────────────────────────────

@router.get(BASE + "/{mouth}/heard")
async def talk_heard(request: Request, mouth: str, after: float = 0.0, wait: float = 0.0):
    m, err = _mouth(request, mouth)
    if err:
        return err
    return await m.heard_after(after, max(0.0, wait))


@router.post(BASE + "/{mouth}/say")
async def talk_say(request: Request, mouth: str, body: dict = Body(default={})):
    """Say this through that mouth. Answers when the mouth has played it."""
    m, err = _mouth(request, mouth)
    if err:
        return err
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "send `text` to say"}, status_code=400)
    if len(text) > MAX_TEXT_CHARS:
        return JSONResponse({"error": f"that is {len(text)} characters; at most {MAX_TEXT_CHARS} in one go"},
                            status_code=413)
    voice = str(body.get("voice") or "")[:64]
    speaker = _conversations(request).speaker

    async def render(piece: str) -> bytes:
        async with speaker:                      # one speaker, every mouth: they take turns here
            return await _render(request, piece, voice)

    try:
        return await m.say(text, render, emotion=re.sub(r"[^a-z_-]", "", str(body.get("emotion") or "").lower())[:32], whole=bool(body.get("whole")))
    except SayFailed as e:
        return JSONResponse({"error": str(e), "played": e.said, "mouth": m.name}, status_code=e.status)


# ── the mouth ────────────────────────────────────────────────────────────

@router.get(BASE + "/{mouth}/audio")
async def talk_audio(request: Request, mouth: str, after: int = 0, wait: float = 0.0):
    """The next piece to play, after the one it last took. Held open until
    there is one or the wait runs out (204)."""
    m, err = _mouth(request, mouth)
    if err:
        return err
    piece = await m.next_piece(after, max(0.0, wait))
    # Seq-Max and Since on every answer: a Lodestar that restarted counts its
    # pieces from 1 again, and a mouth still asking after=37 would never be
    # given them. A Seq-Max below its `after`, or a Since that changed, says so.
    always = {"X-Seren-Talk": "on" if m.talking() else "off", "X-Seren-Seq-Max": str(m.seq),
              "X-Seren-Since": f"{m.since:.3f}"}
    if piece is None:
        return Response(status_code=204, headers=always)
    headers = {"X-Seren-Seq": str(piece.seq), "X-Seren-Last": "1" if piece.last else "0",
               "X-Seren-Clip-Seconds": f"{piece.seconds:.2f}", **always}
    if piece.emotion:
        headers["X-Seren-Emotion"] = piece.emotion
    return Response(content=piece.wav, media_type="audio/wav", headers=headers)


@router.post(BASE + "/{mouth}/played")
async def talk_played(request: Request, mouth: str, body: dict = Body(default={})):
    m, err = _mouth(request, mouth)
    if err:
        return err
    seq = body.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return JSONResponse({"error": 'send {"seq": the piece that finished playing}'}, status_code=400)
    started = body.get("started_ms")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        started = None
    return {"ok": m.played(seq, started), "seq": seq, "on": m.talking()}
