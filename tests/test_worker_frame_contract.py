"""Contract tests for the parent ⇄ worker ``KINDLY_DIAG`` frame format.

Section 4.3 of ``.system_design/TEST_SUITE.md`` describes one line format shared
by two sides that are edited independently:

.. code-block:: text

    KINDLY_DIAG {"request_id": "...", "stage": "...", "msg": "...", ...}

Both sides ship from the same wheel, so this is an **internal** protocol rather
than an agreement between independently versioned parties. It earns a contract
test anyway, because the format is the only thing holding the two sides together
and nothing else compares them.

**Everything here is hermetic, and that is a deliberate instrument choice.**
Four of §4.3's seven stream claims are about *where a chunk boundary falls* — a
frame split mid-frame, several frames arriving together, a multi-byte character
torn in half, a stream ending without a terminator. Where a boundary falls is a
property of pipe timing, not of the child, so a real process cannot be asked to
put one in a chosen place. ``tests/child_processes/worker_child.py`` is the right
instrument for lifecycle claims and the wrong one for these; §5.2a records the
reasoning, and ``.github/review/rules/scrape-browser.md`` was narrowed alongside
this module because it previously read every hermetic runner case as a finding.

**Two instruments, and the smaller one is calibrated against the larger.**
:class:`_ChunkedStream` hands
:func:`~kindly_web_search_mcp_server.scrape.worker_runner._read_stderr_stream`
exactly the chunks a case names, with no scheduling and no waiting, so a boundary
case asserts on a boundary rather than on the event loop's mood. Because that is
a stand-in, :func:`test_the_chunked_stream_stand_in_matches_a_real_stream_reader`
drives the same bytes through a genuine :class:`asyncio.StreamReader` and
requires the same result — without it, every case below would rest on an
uncalibrated double.

No case in this module sleeps for a duration, so none can flake under load.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.scrape import nodriver_worker, worker_runner
from kindly_web_search_mcp_server.utils.diagnostics import (
    FRAME_PREFIX,
    MAX_LINE_CHARS,
    MAX_STDERR_CHARS,
    MAX_STDERR_LINE_CHARS,
    apply_line_limit,
    decode_frame_payload,
    emit_diagnostic,
    encode_frame,
    frame_payload,
)

#: The wire marker, written out as a literal exactly once in this module.
#:
#: Importing :data:`FRAME_PREFIX` everywhere would let the constant be changed to
#: any value at all with every case still green, because both sides of every
#: comparison would move together. That is the specific cost of centralising a
#: format: it stops being observable from inside the code that owns it. This
#: literal is the independent anchor.
WIRE_MARKER = "KINDLY_DIAG "

#: Where the marker may legally appear in code, and how many times, with the
#: reason each site is exempt. An exact count rather than a floor, so deleting a
#: site is a failure too.
#:
#: Docstrings and comments are not counted — the sweep reads syntax trees and
#: skips docstring nodes, because §4.3 is discussed in prose in several modules
#: and prose is not a second implementation.
MARKER_ALLOWANCE = {
    # The one definition. Everything else that needs the marker imports it.
    "src/kindly_web_search_mcp_server/utils/diagnostics.py": 1,
    # `_split_worker_diagnostics`, which has no callers anywhere and is tracked
    # for removal in TEST_SUITE.md §14. Deliberately left alone by E6-2 rather
    # than repointed at the codec: it is dead, and a step whose ticket says "do
    # not test it" should not edit it either. Its two sites go when it does.
    "src/kindly_web_search_mcp_server/scrape/universal_html.py": 2,
    # The fixture child is a test *instrument*. It must not import the package it
    # calibrates -- a guard case in `test_worker_child_fixture.py` holds exactly
    # that -- so it carries its own copy on purpose.
    "tests/child_processes/worker_child.py": 1,
    # That instrument's calibration, which reads the frames the script writes as
    # raw bytes before decoding, and so must name the marker without importing
    # production either.
    "tests/test_worker_child_fixture.py": 1,
    # This module's literal anchor, above.
    "tests/test_worker_frame_contract.py": 1,
}

REPO_ROOT = Path(__file__).resolve().parents[1]


class _ChunkedStream:
    """A stand-in stream that yields exactly the chunks a case names.

    :func:`~kindly_web_search_mcp_server.scrape.worker_runner._read_stderr_stream`
    reads its input with a single ``await stream.read(limit)``, so a stand-in
    needs that one method and nothing else. Feeding a real
    :class:`asyncio.StreamReader` would work too, but only by yielding to the
    event loop between chunks and trusting that one pass is enough for the reader
    to consume each — a timing assumption in a test whose whole subject is where
    a boundary falls.

    It also records the accumulator's buffer length at the top of every read,
    which is the moment *after* the previous chunk was fully drained. That is the
    only place the "the buffer stays bounded" claim can be observed: at the end
    of a run the buffer is empty whether or not a cap exists.

    Attributes:
        buffer_sizes: ``len(state.buffer)`` sampled before each chunk is handed
            over, plus once more before the terminating empty read.
    """

    def __init__(
        self, chunks: list[bytes], state: worker_runner._StderrAccumulator
    ) -> None:
        """Store the chunks to yield and the accumulator to observe.

        Args:
            chunks: The exact byte chunks to return, in order. The stream ends
                after the last one.
            state: The accumulator the reader is draining into, sampled for
                :attr:`buffer_sizes`.
        """
        self._chunks = list(chunks)
        self._state = state
        self.buffer_sizes: list[int] = []

    async def read(self, _limit: int) -> bytes:
        """Return the next chunk, or empty bytes once they are exhausted.

        Args:
            _limit: The reader's requested size, ignored — a case's chunking is
                the point of this class, so the caller does not get to re-chunk.

        Returns:
            The next chunk, or ``b""`` to signal end of stream.
        """
        # Sampled here rather than after the read, because this is the instant at
        # which the previous chunk has been completely processed.
        self.buffer_sizes.append(len(self._state.buffer))
        return self._chunks.pop(0) if self._chunks else b""


async def _drive(
    chunks: list[bytes],
) -> tuple[worker_runner._StderrAccumulator, list[int]]:
    """Run the real stderr reader over an exact chunk sequence.

    Drives the production reader and then the production finaliser, in the same
    order and with the same tail limit ``_run_worker_command`` uses, so a case
    asserts on what a real run would have produced.

    Args:
        chunks: The byte chunks to deliver, in order.

    Returns:
        The accumulator after the stream closed and was finalised, and the buffer
        length sampled after each chunk was processed.
    """
    state = worker_runner._StderrAccumulator()
    stream = _ChunkedStream(chunks, state)
    await worker_runner._read_stderr_stream(
        stream,  # type: ignore[arg-type]
        state,
        diagnostics=None,
        started=0.0,
        tail_limit=MAX_STDERR_CHARS,
    )
    worker_runner._finalize_stderr_state(state, tail_limit=MAX_STDERR_CHARS)
    return state, stream.buffer_sizes


def _run(chunks: list[bytes]) -> tuple[worker_runner._StderrAccumulator, list[int]]:
    """Drive :func:`_drive` on its own event loop.

    Args:
        chunks: The byte chunks to deliver, in order.

    Returns:
        Whatever :func:`_drive` returned.
    """
    return asyncio.run(_drive(chunks))


def _frame(**fields: Any) -> bytes:
    """Build one encoded frame, without its terminator, as bytes.

    Args:
        **fields: The record's fields.

    Returns:
        The encoded line, UTF-8 encoded.
    """
    return encode_frame(dict(fields)).encode("utf-8")


def _worker_diagnostics(
    monkeypatch: pytest.MonkeyPatch, stream: io.StringIO
) -> None:
    """Arm the worker's diagnostics globals against a captured stream.

    ``_emit_diag`` reads module globals rather than the environment — only the
    worker's own entry point assigns them — so a case drives it by setting them,
    which is the pattern ``test_nodriver_worker_sandbox.py`` already uses.

    Args:
        monkeypatch: pytest's patcher, so the globals are restored afterwards.
        stream: Where the worker should write its frames.
    """
    monkeypatch.setattr(nodriver_worker, "_DIAG_ENABLED", True)
    monkeypatch.setattr(nodriver_worker, "_DIAG_STREAM", stream)
    monkeypatch.setattr(nodriver_worker, "_DIAG_REQUEST_ID", "req-42")
    monkeypatch.setattr(nodriver_worker, "_DIAG_STARTED", 0.0)


def _sole_record(written: str) -> dict[str, Any]:
    """Decode the single frame a captured worker stream contains.

    Args:
        written: Everything the worker wrote.

    Returns:
        The decoded record.
    """
    payload = frame_payload(written.rstrip("\n"))
    assert payload is not None, f"not a frame: {written!r}"
    record = decode_frame_payload(payload)
    assert record is not None, f"frame did not decode: {payload!r}"
    return record


def _marker_sites(path: Path) -> list[int]:
    """Find the lines where a file's *code* composes the frame marker.

    Reads the syntax tree rather than the text, and skips docstring nodes, so a
    module that merely discusses the format in prose is not mistaken for a second
    implementation of it. Byte literals count: the fixture calibration holds its
    copy as ``bytes``.

    Args:
        path: The source file to scan.

    Returns:
        The line number of every string or bytes constant containing the marker.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Docstrings are the first statement of a module, class or function, and are
    # the one place the marker may appear as prose without being a copy of it.
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))

    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings:
            continue
        value = node.value
        if isinstance(value, bytes):
            text: str | None = value.decode("utf-8", errors="replace")
        elif isinstance(value, str):
            text = value
        else:
            text = None
        if text is not None and WIRE_MARKER in text:
            sites.append(node.lineno)
    return sites


# ---------------------------------------------------------------------------
# One definition of the format
# ---------------------------------------------------------------------------


def test_the_marker_has_one_definition_and_a_named_exception_list() -> None:
    """Keep the frame marker from acquiring a sixth copy

    §4.3 asks for the encoder and decoder to live "in one place". That is a claim
    about the whole repository, not about one module, so it is checked by
    sweeping every source file rather than by asserting something local.

    The allowance is an **exact count per file**, not a floor. A floor would let
    a site be deleted silently — and two of the four exempt sites exist only
    until the code they belong to is removed, so noticing their departure is the
    point.

    The vocabulary is the marker **including its trailing space**, matched
    against string and bytes *literals* in the syntax tree. Both halves of that
    are load-bearing. Without the space, `KINDLY_DIAGNOSTICS` — the environment
    variable that turns diagnostics on, a different identifier in the same
    vocabulary — is swept in too, from files this allow-list has no reason to
    name. How many is not stated anywhere: it moves whenever something new reads
    that variable, and the property is held by an assertion rather than by a
    figure. Without the literals-only rule,
    the several modules that discuss this format in prose match too.
    """
    found = {}
    for path in sorted(
        [*REPO_ROOT.glob("src/**/*.py"), *REPO_ROOT.glob("tests/**/*.py")]
    ):
        sites = _marker_sites(path)
        if sites:
            # `as_posix`, not `str`. `str(Path)` renders the platform separator,
            # so on Windows every key came back backslash-spelled and matched
            # nothing in the allow-list -- green on Linux, red on Windows, with
            # a diagnostic that read as "the marker is nowhere" rather than "the
            # two spellings disagree". Measured in CI, not reasoned about.
            found[path.relative_to(REPO_ROOT).as_posix()] = len(sites)

    # Keys are compared as text, so their spelling is part of the instrument.
    # This fires only on Windows, where `str(Path)` yields backslashes; it is
    # stated rather than left to the comparison below because that failure
    # presents as "every file is unexpected", which reads as a tree problem.
    assert not [key for key in found if "\\" in key], (
        "the sweep produced platform-spelled keys; MARKER_ALLOWANCE is written "
        f"with forward slashes, so nothing can match.\n  found: {sorted(found)}"
    )
    # Non-vacuity, asserted before the comparison: a sweep whose vocabulary
    # matched nothing at all would otherwise report an empty dict and read as a
    # clean tree rather than as a broken instrument. It names what it *did* find,
    # because the first version said "the marker is nowhere" on a Windows run
    # that had in fact found every site under a different spelling.
    assert "src/kindly_web_search_mcp_server/utils/diagnostics.py" in found, (
        "the sweep did not find the marker in the module that defines it, so "
        "the sweep is broken rather than the tree.\n"
        f"  what it did find: {sorted(found)}"
    )
    # The message deliberately does not spell the marker out: this sweep counts
    # literals, and its own diagnostic would be one of them. Prose that needs to
    # refer to the marker says "the frame marker".
    assert found == MARKER_ALLOWANCE, (
        "the frame marker appears in code somewhere it is not allowed, or has "
        "vanished from somewhere it was expected.\n"
        f"  found:    {found}\n"
        f"  expected: {MARKER_ALLOWANCE}\n"
        "Import FRAME_PREFIX from utils.diagnostics rather than restating it."
    )


def test_the_marker_constant_holds_its_wire_value() -> None:
    """Pin the marker's value, not merely its name

    The sweep above compares a constant against a table of *locations*. Both
    would still agree if ``FRAME_PREFIX`` were changed to ``"DIAG "``, because
    every other site imports it. This is the literal that makes such a change
    fail, and it is why ``WIRE_MARKER`` exists as a separate spelling.
    """
    assert FRAME_PREFIX == WIRE_MARKER
    # The trailing space is not cosmetic; see the sweep's docstring.
    assert FRAME_PREFIX.endswith(" ")
    assert not "KINDLY_DIAGNOSTICS".startswith(FRAME_PREFIX)


def test_the_encoder_writes_the_exact_bytes_the_wire_format_names() -> None:
    """Anchor the encoded line against a literal, not against the encoder

    Every other assertion in this module compares one side of the codec with the
    other, and is therefore blind to a change that moves both — a different
    separator, or a renamed prefix. This is the one case that names the wire
    form outright, so those changes have somewhere to fail.

    The compact separators matter: they are what keeps a frame inside the line
    ceiling, and ``json.dumps``' default ``", "`` would silently widen every
    frame.
    """
    assert (
        encode_frame({"stage": "s", "msg": "m", "n": 1})
        == WIRE_MARKER + '{"stage":"s","msg":"m","n":1}'
    )


# ---------------------------------------------------------------------------
# The codec itself
# ---------------------------------------------------------------------------


def test_frame_payload_separates_a_frame_from_ordinary_output() -> None:
    """Return a payload for a frame line and nothing for any other line

    ``None`` rather than ``""`` for a non-frame line: a line that is only the
    marker is a frame whose payload is empty, which is malformed and must be
    *sampled*, while ``None`` means the line is not a frame at all and is
    ordinary output. Both are falsy, so one falsy return value cannot carry both
    meanings — which is the distinction, and the only place it can be made.

    A blank line reaches neither channel: the router discards it before asking
    this function anything, under both the real code and the mutation below.
    Measured, routing each shape through ``_consume_stderr_line``:

    ==================  ====================  ==========================
    line                real code             ``frame_payload`` → ``None``
    ==================  ====================  ==========================
    ``""``              discarded             discarded
    ``"KINDLY_DIAG "``  sampled               **reaches the tail**
    ==================  ====================  ==========================

    So the mutation misroutes the *bare-marker* line — a frame whose payload is
    empty — and never a blank one. **Writing "blank line" for "line with a blank
    payload" is the elision this split exists to keep out of the code**, and
    successive drafts of this very paragraph made it: first claiming a blank line
    reaches the tail, then claiming a blank line is what the mutation produces.
    Both were replaced by the table above, which cannot drift because it records
    an observation rather than a recollection.
    """
    assert frame_payload(FRAME_PREFIX + '{"stage":"a"}') == '{"stage":"a"}'
    assert frame_payload("chrome: ordinary noise on stderr") is None
    assert frame_payload("") is None
    # A near miss: the marker without its trailing space is not the marker.
    assert frame_payload("KINDLY_DIAGNOSTICS=1") is None


def test_frame_payload_strips_the_whitespace_a_carriage_return_leaves() -> None:
    """Keep the frame path tolerant of a terminator the reader did not remove

    This ``strip`` is load-bearing and is easy to mistake for tidiness: it is
    what makes a ``\\r\\n``-terminated *frame* decode even if the reader's own
    carriage-return handling is removed. The non-frame path has no such second
    line of defence, which is why the CRLF case below asserts on the tail rather
    than on a frame.
    """
    assert frame_payload(FRAME_PREFIX + '  {"stage":"a"}  ') == '{"stage":"a"}'
    assert frame_payload(FRAME_PREFIX + '{"stage":"a"}\r') == '{"stage":"a"}'


def test_decode_frame_payload_rejects_what_is_not_a_record() -> None:
    """Accept an object, reject everything else, and never raise

    A malformed frame must not cost the caller the run, so both failure shapes
    return ``None`` rather than raising: a payload that is not JSON at all, and
    one that is valid JSON but not an object. The second is the one a reader
    forgets — ``json.loads`` succeeds on ``"a string"``, ``7`` and ``[1, 2]``.
    """
    assert decode_frame_payload('{"stage":"a","msg":"b"}') == {"stage": "a", "msg": "b"}
    assert decode_frame_payload('{"stage": "truncated"') is None
    assert decode_frame_payload('"a string, not an object"') is None
    assert decode_frame_payload("[1, 2]") is None
    assert decode_frame_payload("7") is None
    assert decode_frame_payload("") is None


def test_the_encoder_produces_a_line_the_decoder_accepts() -> None:
    """Close the round trip through the codec's own two halves

    Asserted before either side's callers are involved, so a failure here points
    at the codec rather than at a consumer of it.
    """
    record = {"request_id": "r-1", "stage": "worker.spawn", "msg": "x", "data": {"n": 1}}

    line = encode_frame(record)

    assert line.startswith(FRAME_PREFIX)
    # No terminator: both callers supply their own, and an encoder that added one
    # would give `_safe_write_text` a blank line to strip.
    assert not line.endswith("\n")
    payload = frame_payload(line)
    assert payload is not None
    assert decode_frame_payload(payload) == record


def test_the_encoder_escapes_non_ascii_rather_than_emitting_it() -> None:
    """Keep frames pure ASCII on the wire

    ``ensure_ascii=True`` is what makes a *frame* immune to the chunk-boundary
    hazard the reader was fixed for: a frame containing no multi-byte sequence
    cannot have one torn in half. The tail is not so lucky, which is why the
    reader had to be fixed as well rather than instead.
    """
    line = encode_frame({"msg": "naïve — ünïcode ✓"})

    assert line.isascii()
    payload = frame_payload(line)
    assert payload is not None
    assert decode_frame_payload(payload) == {"msg": "naïve — ünïcode ✓"}


# ---------------------------------------------------------------------------
# One ceiling
# ---------------------------------------------------------------------------


def test_apply_line_limit_leaves_an_ordinary_record_untouched() -> None:
    """Pass a record that fits through unchanged

    Without this, an ``apply_line_limit`` that truncated *everything* would
    satisfy every other ceiling case in this module.
    """
    record = {"request_id": "r", "stage": "s", "msg": "m", "data": {"a": 1}}

    assert apply_line_limit(record) == record


def test_the_ceiling_bounds_the_frame_it_produces_not_only_the_one_it_rejects() -> None:
    """Cap the truncated record too, so the ceiling is real

    The fallback copies ``stage`` and ``msg`` from the record it is shortening.
    Measured against the shipped code, a record with a 50 000-character ``msg``
    produced a **50 156**-character wire line while reporting
    ``line_truncated: True`` — so the ceiling bounded what *triggered* the
    fallback, not what the fallback emitted.

    That made the reader's cap unsound rather than merely generous: a frame the
    emitter considered legal would be cut up by the parent and filed as a parse
    error, losing it. Unreachable today only because every ``_emit_diag`` call
    site passes a string literal for ``msg`` — which nothing enforces, and which
    one ``_emit_diag("worker.error", str(exc))`` undoes.
    """
    capped = apply_line_limit(
        {"request_id": "r", "stage": "s", "msg": "M" * 50_000, "elapsed_ms": 1, "data": {}}
    )

    assert capped["line_truncated"] is True
    assert len(encode_frame(capped)) <= len(FRAME_PREFIX) + MAX_LINE_CHARS
    # Shortened, not dropped. The fallback degrades to copying *nothing* if what
    # it built still will not serialize, and a length-only assertion is satisfied
    # by that path too -- so it would survive the very mutation it exists to
    # catch. What truncating buys over dropping is that the record still names
    # itself, and that is what is asserted.
    assert capped["stage"] == "s"
    assert capped["msg"].startswith("M")
    assert 0 < len(capped["msg"]) < 500


def test_the_ceiling_bounds_a_record_whose_stage_is_oversized() -> None:
    """Hold the same claim for the other field the fallback copies

    ``stage`` and ``msg`` are copied by the same two lines. Driving only one of
    them leaves a mutation that shortens one and not the other alive.
    """
    capped = apply_line_limit(
        {"request_id": "r", "stage": "S" * 50_000, "msg": "m", "elapsed_ms": 1, "data": {}}
    )

    assert len(encode_frame(capped)) <= len(FRAME_PREFIX) + MAX_LINE_CHARS
    assert capped["msg"] == "m"
    assert capped["stage"].startswith("S")
    assert 0 < len(capped["stage"]) < 500


def test_the_ceiling_returns_something_encodable_for_every_record() -> None:
    """Make the ceiling's promise true for a field it copies without converting

    The three text fields go through `truncate_text`, which coerces with `str`.
    `elapsed_ms` is copied as it stands, and a large enough integer raises inside
    `json.dumps` — on the *fallback*, after the original was rejected for that
    same reason. Both emitters derive it from a monotonic clock so nothing
    reaches this today, but `apply_line_limit` is public and E5-7 is scheduled to
    drive it with generated values.

    The claim under test is the function's postcondition: whatever it returns,
    `encode_frame` can write and the result fits.
    """
    hostile = [
        {"request_id": "r", "stage": "s", "msg": "m", "elapsed_ms": 10**5000, "data": {}},
        {"request_id": "r", "stage": "s", "msg": "m", "elapsed_ms": object(), "data": {}},
        {"stage": {"not": "a string"}, "msg": ["nor", "this"], "data": {"o": object()}},
        {},
    ]

    for entry in hostile:
        capped = apply_line_limit(entry)
        line = encode_frame(capped)
        assert len(line) <= len(FRAME_PREFIX) + MAX_LINE_CHARS


def test_the_line_cap_cannot_truncate_a_frame_the_worker_considers_legal() -> None:
    """Keep the reader's ceiling above the writer's

    Asserted as the **relation** between the two constants, not as two literals.
    A cap at or below the emitter's ceiling would make the two sides disagree by
    construction: the worker would emit a frame it considers legal and the parent
    would cut it up.
    """
    assert MAX_STDERR_LINE_CHARS > len(FRAME_PREFIX) + MAX_LINE_CHARS


def test_a_frame_at_the_emitters_ceiling_survives_the_reader_intact() -> None:
    """Drive the relation above end to end rather than trusting the arithmetic

    The constants agreeing proves nothing if the cap is applied to a different
    quantity than the one they describe — the prefix forgotten, say, or the limit
    compared against bytes while the buffer holds characters. This builds a frame
    at the ceiling with the real encoder and requires the real reader to return
    it whole.
    """
    record = apply_line_limit(
        {
            "request_id": "r",
            "stage": "s",
            "msg": "m",
            "data": {"blob": "x" * MAX_LINE_CHARS},
        }
    )
    line = encode_frame(record)
    assert len(line) <= len(FRAME_PREFIX) + MAX_LINE_CHARS

    state, _ = _run([line.encode() + b"\n"])

    assert state.worker_entries == [record]


# ---------------------------------------------------------------------------
# One encoder — both writers route through it
# ---------------------------------------------------------------------------


def test_the_parent_side_emitter_writes_the_encoders_line() -> None:
    """Route `emit_diagnostic` through the codec

    Byte-for-byte equality against the encoder's output, plus the terminator this
    caller is responsible for.
    """
    record = {"request_id": "r-1", "stage": "s", "msg": "m", "data": {"a": 1}}
    stream = io.StringIO()

    emit_diagnostic(record, stream=stream)

    assert stream.getvalue() == encode_frame(record) + "\n"


def test_the_worker_side_emitter_writes_the_encoders_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route the worker's `_emit_diag` through the same codec

    `_safe_write_text` supplies the terminator, so the encoder's line is what
    must appear before it.
    """
    stream = io.StringIO()
    _worker_diagnostics(monkeypatch, stream)

    nodriver_worker._emit_diag("worker.start", "starting", {"a": 1})

    written = stream.getvalue()
    assert written.endswith("\n")
    record = _sole_record(written)
    assert written == encode_frame(record) + "\n"
    assert record["stage"] == "worker.start"
    assert record["msg"] == "starting"
    assert record["data"] == {"a": 1}
    assert record["request_id"] == "req-42"


def test_the_worker_keeps_no_private_copy_of_the_frame_ceiling() -> None:
    """Leave one ceiling, not two agreeing by comment

    `_DIAG_LINE_LIMIT` carried the comment "Keep in sync with
    utils.diagnostics.MAX_LINE_CHARS", which is a hope rather than a control —
    and the two had already diverged in behaviour, not merely in risk: only one
    of them handled a payload that would not serialise at all.
    """
    assert not hasattr(nodriver_worker, "_DIAG_LINE_LIMIT"), (
        "the worker has a private frame ceiling again; use "
        "utils.diagnostics.apply_line_limit so there is one"
    )


def test_the_worker_truncates_an_oversized_payload_through_the_shared_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap a huge worker frame, and say that it was capped

    The truncated form is not a shorter version of the record — it is a different
    record that names the original's size, so a reader can tell "this is all
    there was" from "this is what fitted".
    """
    stream = io.StringIO()
    _worker_diagnostics(monkeypatch, stream)

    nodriver_worker._emit_diag("s", "m", {"blob": "x" * (MAX_LINE_CHARS * 2)})

    record = _sole_record(stream.getvalue())
    assert record["line_truncated"] is True
    assert record["data"]["original_len"] > MAX_LINE_CHARS
    assert len(encode_frame(record)) <= len(FRAME_PREFIX) + MAX_LINE_CHARS


def test_the_worker_reports_a_payload_it_cannot_serialise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emit the fallback frame rather than nothing at all

    This is behaviour the worker **gained** by adopting the shared ceiling. Its
    own truncation had a blanket `except: return` around the whole emit, so a
    record carrying a non-serialisable value produced silence — the one outcome a
    diagnostics channel must not produce, because it is indistinguishable from
    the stage never having been reached.
    """
    stream = io.StringIO()
    _worker_diagnostics(monkeypatch, stream)

    nodriver_worker._emit_diag("s", "m", {"unserialisable": object()})

    written = stream.getvalue()
    assert written, "a non-serialisable payload produced no frame at all"
    record = _sole_record(written)
    assert record["line_truncated"] is True
    assert "non-serializable" in record["data"]["note"]


# ---------------------------------------------------------------------------
# One decoder — the live routing on the parent side
# ---------------------------------------------------------------------------


def test_the_line_router_sorts_the_four_shapes_it_receives() -> None:
    """Route a frame, a malformed frame, a non-object frame and plain output

    The four shapes `worker_child.py`'s garbage mode writes, plus the good one,
    asserted through the production router rather than through the codec — the
    codec says what a line *is*, and this says where each kind goes.
    """
    state = worker_runner._StderrAccumulator()

    for line in (
        "",
        "chrome: ordinary noise on stderr",
        encode_frame({"stage": "good"}),
        FRAME_PREFIX + '{"stage": "truncated"',
        FRAME_PREFIX + '"a string, not an object"',
    ):
        worker_runner._consume_stderr_line(state, line, tail_limit=MAX_STDERR_CHARS)

    assert state.worker_entries == [{"stage": "good"}]
    assert state.parse_errors == ['{"stage": "truncated"', '"a string, not an object"']
    # The empty line contributed nothing; the ordinary one is the whole tail.
    assert state.tail == "chrome: ordinary noise on stderr\n"


def test_the_line_router_caps_its_malformed_frame_samples_at_three() -> None:
    """Keep a chatty broken child from flooding memory

    Three is the cap, and the fourth malformed frame must change nothing. A case
    feeding only two — which is all `--stderr-garbage` emits — cannot see the cap
    at all, which is why §5.2a records that the claim had no driver.
    """
    state = worker_runner._StderrAccumulator()

    for index in range(9):
        worker_runner._consume_stderr_line(
            state, FRAME_PREFIX + f'{{"n": {index}', tail_limit=MAX_STDERR_CHARS
        )

    assert len(state.parse_errors) == 3
    # The first three, not the last three: a sample is a sample of what arrived
    # first, and reversing that would lose the frames nearest the cause.
    assert state.parse_errors == ['{"n": 0', '{"n": 1', '{"n": 2']


def test_a_frame_whose_payload_is_empty_is_sampled_rather_than_sent_to_the_tail() -> None:
    """Hold the outcome the ``None``-versus-``""`` split exists to produce

    `frame_payload`'s docstring makes that split load-bearing, and until this
    case nothing drove it through the **router**: `decode_frame_payload("") is
    None` was pinned, but a mutation returning `None` for an empty payload —
    sending a bare marker line to the tail as though it were ordinary browser
    output — left every other case green.

    The three lines below are the whole distinction. A line that is only the
    marker is a frame with nothing in it, and malformed. A line carrying the
    marker *without* its trailing space is not the marker at all. A blank line is
    discarded before the codec is consulted, so it reaches neither channel — the
    docstring claimed it reached the tail, which was wrong in the direction that
    matters, because "it goes to the tail" is exactly the mutation's behaviour.
    """
    state = worker_runner._StderrAccumulator()

    for line in (FRAME_PREFIX, "KINDLY_DIAGNOSTICS=1", ""):
        worker_runner._consume_stderr_line(state, line, tail_limit=MAX_STDERR_CHARS)

    # The bare marker: sampled, and specifically *not* in the tail.
    assert state.parse_errors == [""]
    # The near-miss line is ordinary output; the blank line contributed nothing.
    assert state.tail == "KINDLY_DIAGNOSTICS=1\n"
    assert state.worker_entries == []


def test_a_bare_marker_line_is_sampled_when_it_arrives_through_the_stream() -> None:
    """Drive the same split through the real reader rather than the router alone

    The router case above feeds a line directly. This one proves the same
    outcome survives the path a child actually takes, terminator included.
    """
    state, _ = _run([FRAME_PREFIX.encode() + b"\nchrome: noise\n"])

    assert state.parse_errors == [""]
    assert state.tail == "chrome: noise\n"
    assert state.worker_entries == []


def test_a_malformed_sample_is_truncated_rather_than_kept_whole() -> None:
    """Bound one sample as well as the number of them

    Three samples of a megabyte each is still unbounded logging. The 200-character
    limit is the second half of the same claim.

    The retained *payload* is 200 characters; `truncate_text` then appends its
    own `...(truncated)` marker, so the stored string is longer than the limit by
    exactly that suffix. Asserting `<= 200` on the whole string would be wrong
    about the function rather than about the cap.
    """
    state = worker_runner._StderrAccumulator()

    worker_runner._consume_stderr_line(
        state, FRAME_PREFIX + "{" + "x" * 5000, tail_limit=MAX_STDERR_CHARS
    )

    assert len(state.parse_errors) == 1
    sample = state.parse_errors[0]
    assert sample.endswith("...(truncated)")
    assert len(sample.removesuffix("...(truncated)")) == 200


# ---------------------------------------------------------------------------
# Both sides, end to end
# ---------------------------------------------------------------------------


def test_a_record_emitted_by_the_worker_arrives_at_the_parent_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close the round trip through both sides' real functions

    Not through the codec twice: the worker's real `_emit_diag` writes, and the
    parent's real `_read_stderr_stream` reads. That is the claim §4.3 makes —
    "emission through the real encoder produces a line the real decoder accepts"
    — and it is the one a shared-codec change could break without any
    single-sided case noticing.

    The compared fields are `stage`, `msg`, `data` and `request_id`. Not the
    whole record: `_emit_diag` synthesises `elapsed_ms` from a monotonic clock,
    so there is no "record sent" to compare against.
    """
    stream = io.StringIO()
    _worker_diagnostics(monkeypatch, stream)

    nodriver_worker._emit_diag("worker.spawn", "launching", {"pid": 4242})
    nodriver_worker._emit_diag("worker.stdout", "done", {"bytes": 17})

    state, _ = _run([stream.getvalue().encode("utf-8")])

    assert [entry["stage"] for entry in state.worker_entries] == [
        "worker.spawn",
        "worker.stdout",
    ]
    assert state.worker_entries[0]["request_id"] == "req-42"
    assert state.worker_entries[0]["msg"] == "launching"
    assert state.worker_entries[0]["data"] == {"pid": 4242}
    assert state.parse_errors == []
    assert state.tail == ""


def test_the_chunked_stream_stand_in_matches_a_real_stream_reader() -> None:
    """Calibrate the stand-in against the stream it replaces

    Every boundary case in this module drives `_ChunkedStream`. That is only
    sound while the stand-in and a real :class:`asyncio.StreamReader` produce the
    same result from the same bytes, so this drives both and compares them.
    Without it the module would rest on an uncalibrated double — the failure mode
    a fixture is supposed to prevent, not introduce.

    **The payload deliberately exceeds `STREAM_READ_CHUNK`.** That is where the
    two instruments actually differ: `_ChunkedStream.read` ignores the requested
    limit and hands back whatever a case gave it, while a real reader returns at
    most `STREAM_READ_CHUNK` bytes and is called again. A calibration built from
    a payload that fits in one read compares two single-read behaviours and could
    not observe the divergence it exists to rule out — which is what the first
    version of this case did.
    """
    bulk = b"\n".join(
        _frame(stage="bulk", n=index) for index in range(worker_runner.STREAM_READ_CHUNK // 30)
    )
    payload = (
        _frame(stage="a")
        + b"\nchrome: noise\n"
        + bulk
        + b"\n"
        + _frame(stage="b")
        + b"\n\xff\x80 undecodable\n"
    )
    assert len(payload) > worker_runner.STREAM_READ_CHUNK, (
        "the calibration must span more than one real read or it compares "
        "two single-read behaviours"
    )

    async def through_a_real_reader() -> worker_runner._StderrAccumulator:
        """Drive the production reader over a genuine StreamReader."""
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        state = worker_runner._StderrAccumulator()
        await worker_runner._read_stderr_stream(
            reader, state, diagnostics=None, started=0.0, tail_limit=MAX_STDERR_CHARS
        )
        worker_runner._finalize_stderr_state(state, tail_limit=MAX_STDERR_CHARS)
        return state

    real = asyncio.run(through_a_real_reader())
    stub, _ = _run([payload])

    assert stub.worker_entries == real.worker_entries
    assert stub.tail == real.tail
    assert stub.parse_errors == real.parse_errors
    # Non-vacuity: the two agreeing on *nothing* would satisfy the three
    # comparisons above.
    assert real.worker_entries[0] == {"stage": "a"}
    assert real.worker_entries[-1] == {"stage": "b"}
    assert real.tail.endswith("�� undecodable\n")


# ---------------------------------------------------------------------------
# §4.3's stream cases
# ---------------------------------------------------------------------------


def test_a_frame_split_across_a_chunk_boundary_is_reassembled() -> None:
    """Reassemble one frame delivered in two reads

    The first of §4.3's fragmentation claims and the cheapest: the reader must
    hold a partial line rather than route it.
    """
    line = _frame(stage="split", msg="hello") + b"\n"

    state, _ = _run([line[:20], line[20:]])

    assert state.worker_entries == [{"stage": "split", "msg": "hello"}]
    assert state.tail == ""


def test_several_frames_in_one_chunk_all_arrive_in_order() -> None:
    """Drain every complete line in a chunk, not merely the first

    A reader written with a single `find("\\n")` per chunk passes the case above
    and fails this one, which is why both exist.
    """
    chunk = b"\n".join([_frame(stage=name) for name in ("a", "b", "c")]) + b"\n"

    state, _ = _run([chunk])

    assert [entry["stage"] for entry in state.worker_entries] == ["a", "b", "c"]


def test_a_frame_terminated_with_crlf_decodes_without_its_carriage_return() -> None:
    """Strip the carriage return from a Windows-terminated line

    The assertion that matters is on the **tail**, not on the frame. The frame
    path has two defences — the reader's `rstrip("\\r")` and the decoder's
    `strip()` — so a frame decodes even with the first removed. Ordinary output
    has only the first, and a stray `\\r` there corrupts an operator's
    transcript.
    """
    state, _ = _run([_frame(stage="crlf") + b"\r\nchrome: noise\r\n"])

    assert state.worker_entries == [{"stage": "crlf"}]
    assert state.tail == "chrome: noise\n"
    assert "\r" not in state.tail


def test_a_stream_that_ends_without_a_newline_still_yields_its_last_line() -> None:
    """Recover the final line a child left unterminated

    Often the most interesting line in the stream: a child that dies mid-write
    leaves its last word in the buffer, where the reader's newline loop never
    sees it.
    """
    state, _ = _run([_frame(stage="last", msg="no newline")])

    assert state.worker_entries == [{"stage": "last", "msg": "no newline"}]


def test_a_trailing_plain_line_without_a_newline_reaches_the_tail() -> None:
    """Apply the same recovery to ordinary output

    The finaliser routes through the same function, so both shapes must survive
    it; a fix that special-cased frames would pass the case above alone.
    """
    state, _ = _run([b"chrome: died mid-sentence"])

    assert state.tail == "chrome: died mid-sentence\n"
    assert state.worker_entries == []


def test_the_finaliser_strips_a_carriage_return_from_its_last_line() -> None:
    """Pin the finaliser's own carriage-return handling

    There are **two** `rstrip("\\r")` sites — one in the reader's newline loop
    and one in the finaliser — and the CRLF case above reaches only the first.
    A child killed after a `\\r` but before its `\\n` exercises this one, and
    nothing else did.
    """
    state, _ = _run([b"chrome: cut off mid-terminator\r"])

    assert state.tail == "chrome: cut off mid-terminator\n"


@pytest.mark.parametrize("split_at", [1, 2])
def test_a_multi_byte_character_split_across_chunks_survives(split_at: int) -> None:
    """Carry a partial UTF-8 sequence from one chunk into the next

    Decoding each chunk on its own turns a torn character into two invalid
    fragments, and `errors="replace"` renders each byte as U+FFFD — so the frame
    still parses and is silently wrong, which is worse than failing.

    `_read_stdout_stream`, just above, already stated the rule — "a
    multi-byte character split across two reads would otherwise be corrupted at
    the seam" — and accumulates undecoded bytes for exactly this reason. Stderr
    cannot copy that approach, because it must yield lines before the child
    exits, so it carries an incremental decoder instead.

    Driven at **every** internal split point of the character rather than one,
    because a decoder that carried only the last byte would pass a single-point
    case. The subject is a 3-byte character, so there are two.
    """
    text = "naïve — ünïcode ✓"
    line = json.dumps({"stage": "utf8", "msg": text}, ensure_ascii=False)
    raw = (FRAME_PREFIX + line + "\n").encode("utf-8")
    tick = raw.index("✓".encode("utf-8"))

    state, _ = _run([raw[: tick + split_at], raw[tick + split_at :]])

    assert state.worker_entries == [{"stage": "utf8", "msg": text}]


def test_a_stream_ending_mid_character_still_flushes_a_replacement() -> None:
    """Flush the incremental decoder when the stream closes

    An incremental decoder holds a partial sequence until the next call. If the
    stream ends there and nothing flushes it, those bytes are dropped in silence.
    Measured: without the final flush the truncated tail simply does not appear,
    which is invisible in every other case.
    """
    state, _ = _run([b"chrome: truncated \xe2\x9c"])

    assert state.tail == "chrome: truncated �\n"


def test_the_finaliser_flushes_the_decoder_on_a_run_that_never_saw_eof() -> None:
    """Flush on the cancellation path too, not only at end of stream

    On a timeout `_read_stderr_stream` is **cancelled** rather than reaching end
    of stream, and `_finalize_stderr_state` runs afterwards over whatever state
    survived. A decoder local to the reader would be discarded with its pending
    bytes, so the flush lives on the accumulator and is performed by the
    finaliser — which is the one function both paths reach.

    Driven by decoding a partial sequence into the state and finalising it
    without ever ending the stream, which is that path's exact shape.
    """
    state = worker_runner._StderrAccumulator()

    state.buffer += state.decoder.decode(b"chrome: truncated \xe2\x9c")
    assert state.buffer == "chrome: truncated ", "the partial character was not held"

    worker_runner._finalize_stderr_state(state, tail_limit=MAX_STDERR_CHARS)

    assert state.tail == "chrome: truncated �\n"


def test_undecodable_bytes_are_replaced_rather_than_raised() -> None:
    """Survive bytes that are not UTF-8 at all

    The shape `worker_child.py`'s garbage mode writes. Losing a run to a stray
    byte on a *diagnostic* channel is the wrong trade, and it is the one shape
    that would turn a diagnostic into a crash.

    A non-regression assertion with no injection of its own: an incremental
    decoder and a per-chunk one produce identical output here, measured. It is
    kept because it is the shape the fixture actually writes, so a future
    decoder change has somewhere to fail.
    """
    state, _ = _run([b"\xff\x80 undecodable browser noise\n"])

    assert state.tail == "�� undecodable browser noise\n"
    assert state.worker_entries == []


def test_malformed_frames_are_capped_while_plain_output_still_reaches_the_tail() -> None:
    """Hold both halves of §4.3's malformed-payload claim through the stream

    The router's cap is asserted directly above; this drives it through the real
    reader together with an ordinary line, because "sampled and capped without
    raising" and "non-frame lines survive as human-readable stderr" are one
    sentence in the design and a reader that dropped the second would still pass
    the first.
    """
    chunk = (
        b"\n".join([FRAME_PREFIX.encode() + b'{"n": %d' % n for n in range(5)])
        + b"\nchrome: ordinary noise on stderr\n"
    )

    state, _ = _run([chunk])

    assert len(state.parse_errors) == 3
    assert state.tail == "chrome: ordinary noise on stderr\n"
    assert state.worker_entries == []


# ---------------------------------------------------------------------------
# The oversized-line cap
# ---------------------------------------------------------------------------


def test_an_unterminated_oversized_line_is_bounded_rather_than_buffered() -> None:
    """Bound the buffer a child can grow without ever ending its line

    Measured against the shipped code before the fix: 655 360 bytes fed in,
    655 360 characters retained, no cap. `_append_tail_text` bounds the *tail*
    and the sample list bounds *parse errors*; neither bounds this.
    """
    state, sizes = _run([b"A" * 65536] * 10)

    assert max(sizes) <= MAX_STDERR_LINE_CHARS, (
        f"the stderr buffer grew to {max(sizes)} characters; the cap is "
        f"{MAX_STDERR_LINE_CHARS}"
    )
    assert set(state.tail) <= {"A", "\n"}


def test_the_end_of_an_overlong_line_is_what_survives_it() -> None:
    """Keep the most recent characters, matching `_append_tail_text`'s rule

    `_append_tail_text` keeps the end of the tail "because the end of a failing
    child's stderr is what names the failure". A cap that kept the *first*
    characters of an overlong line would invert that rule for exactly the case it
    was written for — a child that spews and then dies mid-line hands the caller
    the beginning of the spew and drops the message naming the crash.

    So the bound is a sliding window, not a head truncation, and this is the case
    that tells the two apart.
    """
    state, _ = _run([(("Y" * 100_000) + "chrome: segfault at the very end").encode()])

    assert state.tail.endswith("chrome: segfault at the very end\n"), (
        "the end of an overlong line was discarded; the tail keeps the most "
        "recent text, and this cap must agree with it"
    )


def test_the_end_survives_across_chunks_as_well() -> None:
    """Hold the same rule when the line spans many reads

    The single-chunk case above is satisfiable by a cap applied once. This one
    needs the window to slide on every chunk.
    """
    state, sizes = _run(
        [b"Z" * 60_000, b"Z" * 60_000, b"Z" * 60_000, b"chrome: the last word\n"]
    )

    assert max(sizes) <= MAX_STDERR_LINE_CHARS
    assert state.tail.endswith("chrome: the last word\n")


def test_a_chunk_of_many_complete_lines_is_not_truncated_as_one() -> None:
    """Drain complete lines before considering the bound

    `STREAM_READ_CHUNK` is 16 384 and the cap is 16 000, so **every** full read
    can exceed the cap. A bound applied before the newline loop would shred a
    chunk of many short, complete lines — and the fragmentation, several-per-chunk
    and no-newline cases all still pass under that implementation, which is why
    this case exists separately.

    **The observable is frames, not tail text, and that is the whole design of
    this case.** An earlier version asserted on `state.tail` and the mutation
    survived: the tail carries its own 4000-character cap, so the damage — done
    to the *front* of a 20 000-character chunk — had already been discarded by
    the time the assertion looked. `worker_entries` has no cap, so a lost or
    mangled line is visible there and nowhere else.
    """
    frames = [_frame(stage="bulk", n=index) for index in range(500)]
    chunk = b"\n".join(frames) + b"\n"
    assert len(chunk) > MAX_STDERR_LINE_CHARS, (
        "the chunk must exceed the cap or this case proves nothing"
    )

    state, _ = _run([chunk])

    assert len(state.worker_entries) == 500
    assert [entry["n"] for entry in state.worker_entries] == list(range(500))
    assert state.parse_errors == []
    assert state.tail == ""


@pytest.mark.parametrize("length", [MAX_STDERR_LINE_CHARS, MAX_STDERR_LINE_CHARS + 1])
def test_a_terminated_line_at_the_bound_is_routed_whole(length: int) -> None:
    """Document the bound's edge, which no mutation can reach

    **This case pins nothing about the comparison, and saying so is the point.**
    Under a sliding window `buffer[-N:]` is the identity when the buffer is
    exactly `N`, so flipping `>` to `>=` cannot change execution — it is an
    equivalent mutant, proven rather than assumed, and no test can kill it. It
    was killable under the head-truncate-and-discard design this step rejected,
    which is why a boundary case was asked for.

    What it does hold is the surrounding behaviour at the edge: both lengths are
    newline-terminated, so both are routed whole regardless of the bound, which
    is what stops a future author "fixing" the comparison and cutting a
    terminated line.
    """
    line = "G" * length

    state, _ = _run([line.encode() + b"\n"])

    # The tail's own cap keeps the most recent characters, so the observable is
    # that the line's end arrived, not its whole length.
    assert state.tail.endswith("G\n")
    assert len(state.tail) <= MAX_STDERR_CHARS
    assert state.worker_entries == []
