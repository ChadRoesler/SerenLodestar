"""
seren_lodestar.conversation - a voice conversation, kept once (PROTOTYPE, 13 Nov 2026).

Soma's voice server grew two jobs: the robot (its audio protocol, its
microphone, its speaker) and the conversation (queue what was heard for
whoever answers, say a reply a sentence at a time, back and forth, a spoken
stop, the timing of a turn). Symposium needs the second job again without a
robot, so it lives here, between the house's ears (/hear) and its voice
(/speak), and Soma and Symposium become ears and a mouth each.

    the ears  ── a finished clip ──▶  HEARD  ──▶  whoever answers (long-poll)
    the mouth ◀── a piece of sound ── SAID   ◀──  whoever answers (say)

WHAT IS HERE: hear, queue, speak. Not think: whoever answers is a session
somewhere else (a carabiner's, through the Workbench), and which model it
runs on is the carabiner's setting. Not endpointing either: deciding that
somebody has finished talking needs the raw sound, so it stays beside the
microphone, with who may open it. The ears send a clip that is already over.

A MOUTH is one place a conversation happens ("soma", "symposium"): a name,
what was heard there, the pieces waiting to be played there, and whether
back and forth is on. It is the first thing in Lodestar that lives between
requests. It is in memory only: a restart forgets every conversation, the
ears and the mouth find it off and say so.

This module is the state and the rules, with no HTTP and no cluster in it;
routes/talking.py is the door, and renders and transcribes through the
routes that already do that. Lifted from seren_soma.voice and
seren_soma.timing, with what that loop got wrong before it got it right:

- a reply is matched to what was waiting when it was ASKED for, not when it
  finished (VoiceClock.claim);
- a stop said aloud ends back and forth at once and is never handed on to
  whoever answers;
- what is still waiting to be played when a conversation stops is dropped,
  and the one saying it is told it was cut.
"""
from __future__ import annotations

import asyncio
import io
import re
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TALK_MINUTES = (1, 180)
KEEP_HEARD = 100
KEEP_PIECES = 40
MAX_WAIT_S = 30.0
PLAY_GRACE_S = 30.0        # past a piece's own length, how long a mouth has to say it played it
LIVE_S = 40.0              # a mouth or ears not seen for this long is not there

# ── the words ────────────────────────────────────────────────────────────

# Whisper writes sounds as tags: "(laughs)", "[music]", "*sigh*", "♪". A clip
# that is only those has no words in it.
SOUND_TAGS = re.compile(r"^(?:\s|[.,!?…♪-]|\([^)]*\)|\[[^\]]*\]|\*[^*]*\*)*$")

# A sentence: up to its closing punctuation (and any quote or bracket after
# it), when a space or the end follows. "3.5" and "e.g" stay whole.
SENTENCE = re.compile(r'''.+?(?:[.!?…]+["')\]]*(?=\s|$)|$)''', re.S)
SHORT_PIECE = 12
# A first sentence longer than this is cut at its first clause break, so
# there is something short to say at once: rendering runs at about the speed
# of speech, so the room waits as long as the first piece takes to say.
FIRST_PIECE_MAX = 45
CLAUSE_BREAK = re.compile(r"[,;:]\s|\s[—–-]\s")

# Said on its own during back and forth, one of these ends it. The whole
# utterance has to be the phrase (after an "alright" or "okay"): "oh, stop
# it" in the middle of banter is not an order. A stop is often said TO
# someone ("okay Juno, that's enough"), and what whoever answers is called
# is the person's to say, never written here: the ears send the names.
STOP_PHRASES = {"stop", "stop it", "stop listening", "that's enough", "thats enough", "shut it down", "mic off",
                "ears off", "go to sleep", "we're done", "were done", "end it"}
STOP_LEAD = {"alright", "all", "right", "okay", "ok", "hey", "now", "please", "well", "and", "so"}
STOP_TAIL = {"please", "now"}
MAX_NAMES = 12


def has_words(text: str) -> bool:
    return not SOUND_TAGS.match(text or "")


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z' ]", " ", (text or "").lower().replace("’", "'")).split()


def name_words(names: Any) -> frozenset:
    """What whoever answers is called, as said aloud: a list, or one comma
    line ("Juno, June"). Every word of every name, lowercase."""
    items = names if isinstance(names, (list, tuple, set, frozenset)) else str(names or "").split(",")
    return frozenset(w for item in list(items)[:MAX_NAMES] for w in _words(str(item))[:4])


def is_stop(text: str, names: Any = ()) -> bool:
    """names: what whoever answers is called here, so "okay Juno, stop" is a
    stop where the one answering is Juno and banter anywhere else."""
    called = name_words(names)
    words = _words(text)
    while words and (words[0] in STOP_LEAD or words[0] in called):
        words.pop(0)
    while words and (words[-1] in STOP_TAIL or words[-1] in called):
        words.pop()
    return " ".join(words) in STOP_PHRASES


def split_sentences(text: str) -> list[str]:
    """The pieces a line is rendered and spoken in. The first is what the
    room waits for, so it is kept as short as it comes, a lone "Ha!"
    included. After it, a piece too short to carry its own tone joins the
    one before."""
    text = (text or "").strip()
    parts = [m.group(0).strip() for m in SENTENCE.finditer(text)]
    parts = [x for x in parts if x]
    if parts and len(parts[0]) > FIRST_PIECE_MAX:
        cuts = [m.end() for m in CLAUSE_BREAK.finditer(parts[0]) if SHORT_PIECE <= m.end() <= FIRST_PIECE_MAX + 15]
        if cuts:
            parts[0:1] = [parts[0][:cuts[0]].strip(), parts[0][cuts[0]:].strip()]
    out: list[str] = []
    for part in parts:
        if len(out) > 1 and len(part) < SHORT_PIECE:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out or ([text] if text else [])


def wav_seconds(data: bytes) -> float:
    """How long a WAV plays, or 0 for one that cannot be read."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, EOFError, ValueError):
        return 0.0


# ── the clock ────────────────────────────────────────────────────────────

ANSWER_WITHIN_S = 120      # a reply later than this after the words is not an answer to them


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


class VoiceClock:
    """How long a turn takes and where the time goes, each leg on its own
    because each is somebody else's to make faster. Nothing that was said is
    kept: a turn holds how many words, never which."""

    def __init__(self, keep: int = 60):
        self.turns: deque = deque(maxlen=keep)
        self.seq = 0
        self._open = None      # the last thing heard that nothing has answered: (seq, words ready, talking stopped)

    def heard(self, *, speech_end: float, arrived: float, done: float, audio_s: float, words: int,
              transcribed: bool) -> dict:
        """speech_end: when the talking stopped, as the ears say. arrived:
        when the clip got here. done: when the words came back."""
        self.seq += 1
        turn = {"seq": self.seq, "kind": "heard", "t": time.time(), "audio_s": round(audio_s, 1), "words": words,
                "transcribed": transcribed, "to_here_ms": _ms(arrived - speech_end), "transcribe_ms": _ms(done - arrived)}
        self.turns.append(turn)
        if words:
            self._open = (self.seq, done, speech_end)
        return turn

    def claim(self):
        """Take the words nothing has answered yet, at the moment a reply is
        asked for. What is heard after that, while the reply is still being
        rendered, waits for the next reply."""
        answering, self._open = self._open, None
        return answering

    def said(self, *, asked: float, rendered_first: float, first_taken: float, done: float, pieces: int,
             chars: int, audio_s: float, answering=None, first_sound: Optional[float] = None) -> dict:
        """asked: when the words arrived to be said. rendered_first: when the
        first piece was ready. first_taken: when the mouth took it.
        first_sound: when the sound started, if the mouth said (a robot has
        a channel to open between taking a piece and making a noise)."""
        self.seq += 1
        turn = {"seq": self.seq, "kind": "said", "t": time.time(), "pieces": pieces, "chars": chars,
                "audio_s": round(audio_s, 1), "render_first_ms": _ms(rendered_first - asked),
                "to_mouth_ms": _ms(first_taken - asked), "speaking_ms": _ms(done - first_taken)}
        if first_sound is not None:
            turn["open_channel_ms"] = _ms(first_sound - first_taken)
            turn["to_first_sound_ms"] = _ms(first_sound - asked)
        if answering is not None and asked - answering[1] <= ANSWER_WITHIN_S:
            seq, words_ready, speech_end = answering
            turn["answers"] = seq
            turn["mind_ms"] = _ms(asked - words_ready)
            turn["last_word_to_mouth_ms"] = _ms(first_taken - speech_end)
            if first_sound is not None:      # the number a conversation actually feels
                turn["last_word_to_first_sound_ms"] = _ms(first_sound - speech_end)
        self.turns.append(turn)
        return turn

    def recent(self) -> list:
        return list(self.turns)


# ── a mouth ──────────────────────────────────────────────────────────────

class SayFailed(Exception):
    """A line that could not be said, or not all of it. `status` is the HTTP
    status the door answers with; `said` is how many pieces were played."""

    def __init__(self, message: str, status: int = 502, said: int = 0):
        super().__init__(message)
        self.status, self.said = status, said


@dataclass
class Piece:
    seq: int
    wav: bytes
    seconds: float
    emotion: str = ""
    last: bool = False             # the last piece of its line
    taken: Optional[float] = None  # when the mouth took it (time.monotonic())
    sound: Optional[float] = None  # when its sound started, if the mouth said
    played: bool = False
    dropped: bool = False          # the conversation stopped before it was played
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Mouth:
    def __init__(self, name: str):
        self.name = name
        self.since = time.time()           # when this mouth began here: a restart makes a new one, counting from 1
        self.names: frozenset = frozenset()  # what whoever answers is called here, as the ears last said
        self.heard: list[dict] = []
        self._heard = asyncio.Event()
        self.pieces: list[Piece] = []
        self._audio = asyncio.Event()
        self.seq = 0
        self.talk_until = 0.0
        self.voice = ""                    # "held" while the speaker is kept loaded for this mouth, else why not
        self.clock = VoiceClock()
        self.ears_seen = 0.0
        self.mouth_seen = 0.0
        self.keeper: Optional[asyncio.Task] = None   # the task that holds the speaker while this is on
        self.stops = 0                     # how many times this was stopped: a line begun before a stop is cut by it
        self._saying = asyncio.Lock()      # one line at a time through one mouth

    # -- back and forth --

    def talking(self) -> bool:
        return time.time() < self.talk_until

    def start(self, minutes: float) -> None:
        self.talk_until = time.time() + minutes * 60

    def stop(self) -> int:
        """End back and forth now. What was waiting to be played is dropped
        and whoever was saying it is told; a piece the mouth already took
        plays out, and cutting that sound is the mouth's. Returns how many
        pieces were dropped."""
        self.talk_until = 0.0
        self.stops += 1
        dropped = 0
        for p in self.pieces:
            if not p.played and not p.dropped and p.taken is None:
                p.dropped, p.wav = True, b""
                p.done.set()
                dropped += 1
        self._wake_audio()
        self._wake_heard()        # whoever answers is waiting on what is heard: let them see it is over
        return dropped

    def state(self) -> dict[str, Any]:
        now = time.monotonic()
        return {"mouth": self.name, "on": self.talking(), "until": self.talk_until if self.talking() else None,
                "voice": self.voice, "ears_live": bool(self.ears_seen and now - self.ears_seen < LIVE_S),
                "mouth_live": bool(self.mouth_seen and now - self.mouth_seen < LIVE_S),
                "waiting_to_play": sum(1 for p in self.pieces if not p.played and not p.dropped),
                "minutes_range": list(TALK_MINUTES)}

    # -- what was heard --

    def _wake_heard(self) -> None:
        waiting, self._heard = self._heard, asyncio.Event()
        waiting.set()

    def hear(self, text: str, seconds: float = 0.0, *, arrived: Optional[float] = None, quiet_ms: float = 0.0) -> str:
        """One finished utterance, transcribed. "wordless": a noise, dropped.
        "stop": an order to end back and forth, obeyed and not handed on.
        "queued": waiting for whoever answers. arrived: when the clip got
        here (time.monotonic(); now, when the ears sent words). quiet_ms: how
        long before that the talking stopped, as the ears know it."""
        now = time.monotonic()
        self.ears_seen = now
        arrived = now if arrived is None else arrived
        text = (text or "").strip()
        stop = self.talking() and is_stop(text, self.names)
        outcome = "wordless" if not has_words(text) else ("stop" if stop else "queued")
        self.clock.heard(speech_end=arrived - quiet_ms / 1000, arrived=arrived, done=now, audio_s=seconds,
                         words=len(text.split()) if outcome == "queued" else 0, transcribed=True)
        if outcome == "wordless":
            return outcome
        if outcome == "stop":
            self.stop()
            return outcome
        t = time.time()
        if self.heard and t <= self.heard[-1]["t"]:
            t = self.heard[-1]["t"] + 0.000001        # `after` has to tell two lines apart
        self.heard = (self.heard + [{"t": t, "seconds": round(seconds, 1), "text": text}])[-KEEP_HEARD:]
        self._wake_heard()
        return "queued"

    async def heard_after(self, after: float, wait: float) -> dict[str, Any]:
        """What was heard after time `after` (an entry's own t). With wait,
        an empty answer is held until something is heard, back and forth
        ends, or the wait runs out. after=0 answers at once, to begin from."""
        def new():
            return [e for e in self.heard if e["t"] > after]

        entries = new()
        if not entries and wait > 0 and after > 0:
            try:
                await asyncio.wait_for(self._heard.wait(), min(wait, MAX_WAIT_S))
            except asyncio.TimeoutError:
                pass
            entries = new()
        return {"entries": entries, "now": time.time(), "last": self.heard[-1]["t"] if self.heard else after,
                "on": self.talking()}

    # -- what is said --

    def _wake_audio(self) -> None:
        waiting, self._audio = self._audio, asyncio.Event()
        waiting.set()

    def _add(self, wav: bytes, emotion: str, last: bool) -> Piece:
        self.seq += 1
        piece = Piece(seq=self.seq, wav=wav, seconds=wav_seconds(wav), emotion=emotion, last=last)
        self.pieces = (self.pieces + [piece])[-KEEP_PIECES:]
        self._wake_audio()
        return piece

    async def next_piece(self, after: int, wait: float) -> Optional[Piece]:
        """The next piece for the mouth to play, after the one it last took.
        Held open until there is one or the wait runs out; None then."""
        self.mouth_seen = time.monotonic()

        def nxt():
            return next((p for p in self.pieces if p.seq > after and not p.dropped and not p.played), None)

        piece = nxt()
        if piece is None and wait > 0:
            try:
                await asyncio.wait_for(self._audio.wait(), min(wait, MAX_WAIT_S))
            except asyncio.TimeoutError:
                pass
            piece = nxt()
        self.mouth_seen = time.monotonic()
        if piece is not None and piece.taken is None:
            piece.taken = time.monotonic()
        return piece

    def played(self, seq: int, started_ms: Optional[float] = None) -> bool:
        """The mouth says it has finished playing this piece. started_ms:
        how long after taking it the sound started, when the mouth knows."""
        self.mouth_seen = time.monotonic()
        for p in self.pieces:
            if p.seq == seq and not p.dropped:
                if started_ms is not None and p.taken is not None:
                    p.sound = p.taken + max(0.0, started_ms) / 1000
                p.played, p.wav = True, b""
                p.done.set()
                return True
        return False

    async def say(self, text: str, render: Callable[[str], Awaitable[bytes]], emotion: str = "",
                  whole: bool = False) -> dict[str, Any]:
        """Say a line through this mouth: a sentence at a time, the first
        ready to play while the rest are still being rendered. Returns when
        the mouth has played all of it, or it was cut by a stop. `render`
        turns one piece of text into a WAV, or raises SayFailed."""
        asked = time.monotonic()
        answering = self.clock.claim()        # what this is a reply to: decided now, not when it is done
        parts = [text.strip()] if whole else split_sentences(text)
        if not parts or not parts[0]:
            raise SayFailed("no text", status=400)
        async with self._saying:
            return await self._say(text, parts, render, emotion, asked, answering)

    async def _say(self, text: str, parts: list[str], render, emotion: str, asked: float, answering) -> dict[str, Any]:
        ready: asyncio.Queue = asyncio.Queue()
        stops = self.stops
        CUT = object()

        async def rendering():
            try:
                for i, part in enumerate(parts):
                    wav = await render(part)
                    if self.stops != stops:           # stopped while this piece was being rendered: it never plays
                        await ready.put(CUT)
                        return
                    await ready.put(self._add(wav, emotion if i == 0 else "", last=i == len(parts) - 1))
                await ready.put(None)
            except Exception as e:  # noqa: BLE001 - handed to the one waiting, who knows what was said so far
                await ready.put(e)

        renderer = asyncio.create_task(rendering())
        pieces: list[Piece] = []
        rendered_first = None
        failed: Optional[Exception] = None
        cut = False
        try:
            while True:
                piece = await ready.get()
                if piece is None:
                    break
                if piece is CUT:
                    cut = True
                    break
                if isinstance(piece, Exception):
                    failed = piece
                    break
                if rendered_first is None:
                    rendered_first = time.monotonic()
                pieces.append(piece)
                try:
                    await asyncio.wait_for(piece.done.wait(), piece.seconds + PLAY_GRACE_S)
                except asyncio.TimeoutError:
                    piece.dropped, piece.wav = True, b""
                    raise SayFailed(f"nothing is playing for '{self.name}': the piece waited "
                                    f"{piece.seconds + PLAY_GRACE_S:.0f}s and was not played", status=504,
                                    said=sum(1 for p in pieces if p.played)) from None
                if piece.dropped:
                    cut = True
                    break
        finally:
            renderer.cancel()
            for p in self.pieces:                      # anything rendered behind a cut or a failure never plays
                if p.seq > (pieces[-1].seq if pieces else self.seq) and not p.played and p.taken is None:
                    p.dropped, p.wav = True, b""
                    p.done.set()
        said = [p for p in pieces if p.played]
        if said and said[0].taken is not None and rendered_first is not None:
            self.clock.said(asked=asked, rendered_first=rendered_first, first_taken=said[0].taken,
                            done=time.monotonic(), pieces=len(said), chars=len(text),
                            audio_s=sum(p.seconds for p in said), answering=answering, first_sound=said[0].sound)
        if failed is not None:
            if isinstance(failed, SayFailed):
                failed.said = len(said)
                raise failed
            raise SayFailed(f"{type(failed).__name__}: {failed}", said=len(said)) from failed
        return {"ok": True, "pieces": len(parts), "played": len(said), "cut": cut,
                "seconds": round(sum(p.seconds for p in said), 1)}


class Conversations:
    """Every mouth this Lodestar has heard of since it started, and the one
    speaker they share: it renders one request at a time, and only here are
    two mouths seen at once, so here is where they take turns."""

    def __init__(self) -> None:
        self.mouths: dict[str, Mouth] = {}
        self.speaker = asyncio.Lock()

    def get(self, name: str) -> Mouth:
        if not NAME.fullmatch(name or ""):
            raise LookupError(f"not a mouth's name: {name!r} (lowercase letters, digits, - and _, 40 at most)")
        if name not in self.mouths:
            self.mouths[name] = Mouth(name)
        return self.mouths[name]

    def states(self) -> list[dict[str, Any]]:
        return [m.state() for m in self.mouths.values()]
