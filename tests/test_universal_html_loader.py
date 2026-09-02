from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
from collections.abc import Iterator
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

#: Stdout the doubled runner hands back. Held as bytes, and decoded at each use,
#: so it stays the same literal `tests/test_worker_runner.py` writes from a real
#: child — the two files assert the same payload at different strengths, and a
#: divergence between them should be a deliberate edit rather than a drift.
WORKER_STDOUT = b"<html><body><p>ok</p></body></html>"

#: Every variable `fetch_html_via_nodriver` reads that can change what these
#: tests observe. Cleared for each case, which then declares what it needs.
#:
#: **This is a superset, deliberately.** Some entries are read only on branches
#: these three never take; clearing one costs nothing and missing one costs a
#: test that passes while asserting nothing.
#:
#: Two are not hypothetical. Measured with the production behaviour fully
#: removed:
#:
#: * `PYTHONPATH` — the spawn case asserts the key is in the child environment,
#:   which is `dict(os.environ)`. Exported ambiently it satisfies that assertion
#:   with `_maybe_add_src_to_pythonpath` reduced to a no-op: **fails on a clean
#:   env, passes with `PYTHONPATH` set.**
#: * `NO_PROXY` / `no_proxy` — likewise for the loopback case with
#:   `_ensure_no_proxy_localhost_env` reduced to a no-op: **fails clean, passes
#:   with `NO_PROXY=localhost,127.0.0.1` set.**
#:
#: Both were found by review, not by the author's falsification pass, which ran
#: on a shell exporting neither. A control that only fires on one machine is no
#: control — the lesson `test_nodriver_worker_sandbox.py` records for `CHROME_BIN`.
READ_ENVIRONMENT_VARIABLES = (
    "KINDLY_NODRIVER_REUSE_BROWSER",
    "KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST",
    "KINDLY_HTML_TOTAL_TIMEOUT_SECONDS",
    "KINDLY_DIAGNOSTICS",
    "KINDLY_REQUEST_ID",
    "KINDLY_BROWSER_EXECUTABLE_PATH",
    "BROWSER_EXECUTABLE_PATH",
    "CHROME_BIN",
    "CHROME_PATH",
    "PYTHONPATH",
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


@contextlib.contextmanager
def pinned_environment(**overrides: str) -> Iterator[None]:
    """Run a case with every variable the loader reads cleared, then declared.

    `KINDLY_NODRIVER_REUSE_BROWSER` is forced off rather than merely cleared.
    `reuse_enabled()` returns True unless explicitly disabled, so without this
    pin `fetch_html_via_nodriver` enters the Chromium pool on every case.

    Measured in this worktree with the pin removed, by patching
    `nodriver_worker._launch_chromium` to record and raise: the launch **is**
    reached, with the real resolved browser and a real profile directory —

        real _launch_chromium reached: True
        ('/usr/bin/google-chrome-stable', [... --user-data-dir=/tmp/kindly-nodriver-pool-… ])
        module-global pool left behind: True
        atexit shutdown registered: True

    So an unpinned case launches Chromium on any machine that has one, and
    leaves a module-global pool and an `atexit` hook behind.

    **Two earlier wordings of this paragraph were wrong, in opposite
    directions.** The first claimed the tests left live browser trees behind; a
    process count then measured a delta of zero and the claim was retracted as
    false. The retraction was itself wrong: a count taken after the interpreter
    exits cannot distinguish "no browser launched" from "browser launched and
    reaped", and the second is what happens — `_register_shutdown` installs an
    `atexit` hook whose `shutdown_sync` calls `ChromiumSlot.terminate_sync`,
    killing the process and removing the profile directory. A hard-killed or
    `os._exit`ed interpreter never runs it, and then the tree does survive.

    Also corrected: `ChromiumPool.acquire` **returns `None`** on failure rather
    than raising, and on a machine with a working browser it does not fail at
    all — it returns a live slot.

    The pooled path with a *real* pool is a subsystem concern and is not this
    file's to cover. The rule for the cases that override this default: every one
    of them must also double `get_chromium_pool`, or it reaches a real browser.
    No count is given, deliberately. An earlier wording gave one, was wrong by a
    case on the day it was written, and was wrong inside the very sentence
    explaining that counts go stale.

    Args:
        **overrides: Variables this case needs set, applied after the clear.

    Yields:
        None, for the duration of the patched environment.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in READ_ENVIRONMENT_VARIABLES
    }
    environment["KINDLY_NODRIVER_REUSE_BROWSER"] = "0"
    environment.update(overrides)
    with patch.dict("os.environ", environment, clear=True):
        yield


def _run_call(mock_run: AsyncMock) -> tuple[list[str], dict[str, object]]:
    """Read the command and keyword arguments off the runner double's last call.

    The command is `_run_worker_command`'s first **positional** parameter, so a
    case that looked for it in `kwargs` would find nothing and assert nothing.
    One accessor, so that stays true in one place.

    Args:
        mock_run: The double standing in for `_run_worker_command`.

    Returns:
        The argv the runner was handed, and the keyword arguments it received.
    """
    call = mock_run.call_args
    assert call is not None, "the worker runner was never called"
    return call.args[0], call.kwargs


class TestUniversalHtmlLoader(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_url_returns_none(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        out = await load_url_as_markdown("https://example.com/file.pdf")
        self.assertIsNone(out)

    async def test_default_total_timeout_is_60(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
        )

        config = UniversalHtmlLoaderConfig()
        self.assertEqual(config.total_timeout_seconds, 60.0)

    async def test_converts_html_to_markdown(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        html = "<html><body><main><h1>Title</h1><p>Hello world</p></main></body></html>"

        with patch(
            "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = html
            out = await load_url_as_markdown("https://example.com")

        self.assertIsInstance(out, str)
        self.assertIn("Title", out)
        self.assertIn("Hello world", out)

    async def test_fetch_html_spawns_worker_subprocess(self) -> None:
        """Spawn the worker module, and return what the runner produced

        **The seam is `_run_worker_command`, not the standard library.** Until
        the runner was extracted these cases patched
        `universal_html.asyncio.create_subprocess_exec`, which resolves through
        the shared `asyncio` module object and so replaced the spawn primitive
        for the whole process. The suite design rules that out as opaque
        coupling and made removing it this extraction's job; the module no
        longer imports `asyncio` at all, so the old target now raises
        `AttributeError` rather than quietly working.

        **What that costs, stated rather than hidden.** These cases used to
        drive a `FakeWorkerProcess` through production's stream readers, so the
        returned markup proved the parent still read the child's stdout — the
        exact drift that left this file red. With the runner doubled, the
        equality below proves only that the runner's return value reaches the
        caller unaltered. The streaming claim moved to
        `tests/test_worker_runner.py`, which asserts it against a real child
        process, where it can no longer be satisfied by a fake.
        """
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        with (
            pinned_environment(),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                return_value=WORKER_STDOUT.decode(),
            ) as mock_run,
        ):
            html = await fetch_html_via_nodriver("https://example.com")

        self.assertEqual(html, WORKER_STDOUT.decode())
        self.assertTrue(mock_run.called)
        command, kwargs = _run_call(mock_run)
        # Membership, not adjacency: `tests/test_worker_command_builder.py` owns
        # the builder's exact shape by whole-list equality. Pinning order here as
        # well would put one claim at two layers.
        self.assertIn("-m", command)
        self.assertIn("kindly_web_search_mcp_server.scrape.nodriver_worker", command)
        self.assertIn("PYTHONPATH", kwargs["env"])

    async def test_fetch_html_passes_browser_executable_path_when_set(self) -> None:
        """Forward the configured browser path to the worker command line"""
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        with (
            pinned_environment(KINDLY_BROWSER_EXECUTABLE_PATH="/usr/bin/chromium"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                return_value=WORKER_STDOUT.decode(),
            ) as mock_run,
        ):
            await fetch_html_via_nodriver("https://example.com")

        command, _kwargs = _run_call(mock_run)
        self.assertIn("--browser-executable-path", command)
        self.assertIn("/usr/bin/chromium", command)

    async def test_fetch_html_sets_no_proxy_for_loopback(self) -> None:
        """Exempt loopback from the proxy, so the child can reach its own DevTools"""
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        with (
            pinned_environment(
                HTTP_PROXY="http://proxy.invalid:8080",
                # Declared on: this case's whole subject is the behaviour the
                # variable gates, so an ambient "0" would leave it passing for
                # having asserted nothing.
                KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST="1",
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                return_value=WORKER_STDOUT.decode(),
            ) as mock_run,
        ):
            await fetch_html_via_nodriver("https://example.com")

        _command, kwargs = _run_call(mock_run)
        env = kwargs.get("env") or {}
        no_proxy = (env.get("NO_PROXY") or env.get("no_proxy") or "").lower()
        self.assertIn("localhost", no_proxy)
        self.assertIn("127.0.0.1", no_proxy)

    async def test_pooled_fetch_spawns_exactly_what_the_builder_returned(self) -> None:
        """Run the builder's command verbatim, and build it for the pooled slot

        Wiring only, no shape: `tests/test_worker_command_builder.py` owns what
        the command line looks like, and the three cases above own it at
        membership strength for the unpooled path. Neither can see the pooled
        call site, because all three force the pool off — so a rewire that
        passed `slot=None` here would kill pooled browser reuse in production
        with the whole suite still green and the node-id set unchanged. This is
        the case that goes red on it.

        Three preconditions the case cannot do without, each of which would
        otherwise leave it passing while asserting nothing, or reaching a real
        browser:

        * **Reuse is switched back on.** `pinned_environment` forces
          `KINDLY_NODRIVER_REUSE_BROWSER=0` for the other cases, and without the
          override the whole acquisition block is skipped and the builder is
          called with `slot=None` — which is exactly the state the mutation this
          case exists to kill injects.
        * **The patch target is `universal_html.get_chromium_pool`, not
          `chromium_pool.get_chromium_pool`.** The loader from-imports the name,
          so patching it at its source leaves the bound name alone, the real
          pool runs, and `acquire` reaches `ChromiumSlot.ensure_started` — a
          real Chromium, a cached module-global pool and an `atexit` hook that
          outlives the test.
        * **`pool.acquire` is asserted to have been awaited.** The acquisition
          block swallows every exception, so a misbuilt double fails this case
          with `slot=None` — indistinguishable from the mutation. The extra
          assertion makes a broken double name itself.

        The builder is asserted on its *first* call: a slot being present makes
        the pool-restart retry path live for the first time in the unit lane,
        and a second builder call from there would otherwise overwrite
        `call_args` and hide the mutation.
        """
        from kindly_web_search_mcp_server.scrape.chromium_pool import ChromiumSlot
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        slot = ChromiumSlot(slot_id=3, host="127.0.0.4", port=9444)
        pool = AsyncMock()
        pool.acquire.return_value = slot
        sentinel_command = ["/sentinel/python", "-m", "sentinel.worker"]

        with (
            pinned_environment(KINDLY_NODRIVER_REUSE_BROWSER="1"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.get_chromium_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._build_worker_command",
                return_value=sentinel_command,
            ) as mock_builder,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                return_value=WORKER_STDOUT.decode(),
            ) as mock_run,
        ):
            await fetch_html_via_nodriver("https://example.com")

        self.assertEqual(
            pool.acquire.await_count, 1, "the pooled slot was never acquired"
        )
        command, _kwargs = _run_call(mock_run)
        self.assertEqual(command, sentinel_command)
        self.assertEqual(mock_builder.call_count, 1)
        self.assertIs(mock_builder.call_args_list[0].kwargs["slot"], slot)
        # The ordinary success path's release count. Every other release
        # assertion in this file is on a failure path, and "released exactly
        # once on every exit path" is not shown by failure paths alone.
        self.assertEqual(pool.release.await_count, 1)

    async def test_pool_slot_is_released_when_the_run_never_starts(self) -> None:
        """Return the pooled slot even when the failure precedes the worker run

        The defect this pins, measured before the fix with the acquisition
        forced to succeed and a `RuntimeError` injected in the window::

            queue before: 1 | slots: 1
            raised: RuntimeError
            queue after : 0 | slots: 1
            SLOT STRANDED: True

        The slot was acquired at the top of `fetch_html_via_nodriver` while the
        `finally` that returns it hung off a `try` opened much further down, so
        anything raising in between stranded it. Bounded rather than fatal —
        once every slot is stranded, `acquire` times out, returns `None`, and
        callers fall back to a cold browser — which is why it was carried as a
        medium-severity defect into the step that restructures this function
        rather than filed as its own.

        The failure is injected at the builder because that is the first thing
        the window does after acquiring, and because it is a call the case can
        reach without doubling anything the acquisition itself depends on. The
        pool is a full double: this case is about the caller's release
        contract, not about the pool's own bookkeeping.
        """
        from kindly_web_search_mcp_server.scrape.chromium_pool import ChromiumSlot
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        slot = ChromiumSlot(slot_id=5, host="127.0.0.5", port=9445)
        pool = AsyncMock()
        pool.acquire.return_value = slot

        with (
            pinned_environment(KINDLY_NODRIVER_REUSE_BROWSER="1"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.get_chromium_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._build_worker_command",
                side_effect=RuntimeError("injected after acquisition"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                await fetch_html_via_nodriver("https://example.com")

        # The identity of the propagated error, not only its type. Widening the
        # *retry* try to cover the acquisition would also release the slot and
        # satisfy every other assertion here, while sending this failure into
        # the pool-restart handler, which reads names the raise skipped past —
        # so the caller would receive an UnboundLocalError instead of its own
        # error, with nothing to say why.
        self.assertEqual(str(caught.exception), "injected after acquisition")
        self.assertEqual(
            pool.acquire.await_count, 1, "the pooled slot was never acquired"
        )
        self.assertEqual(
            pool.release.await_count, 1, "the acquired slot was never released"
        )
        self.assertIs(pool.release.await_args.args[0], slot)

    async def test_pool_restart_retry_builds_a_second_command_for_the_second_slot(
        self,
    ) -> None:
        """Rebuild the command for the replacement slot after a pool restart

        The retry call site had been verified exactly once, by a throwaway
        differential run against the pre-extraction implementation, and by no
        committed test: a mutation there — reusing the stale slot, or passing
        `slot=None` — survived the whole fault-injection battery, which reaches
        the *first* builder call only. Restructuring this function inherits that
        gap, so it is closed here.

        The first run fails with a message the restart classifier matches. Its
        wording is load-bearing: `_pool_error_requires_restart` scans the
        exception chain for a fixed set of patterns, and an unmatched message is
        re-raised without any retry at all, which would leave this case passing
        for the wrong reason. `assertEqual` on the call count is what catches
        that — a re-raise gives one builder call, not two.
        """
        from kindly_web_search_mcp_server.scrape.chromium_pool import ChromiumSlot
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        first = ChromiumSlot(slot_id=1, host="127.0.0.1", port=9441)
        second = ChromiumSlot(slot_id=2, host="127.0.0.2", port=9442)
        pool = AsyncMock()
        pool.acquire.side_effect = [first, second]

        with (
            pinned_environment(KINDLY_NODRIVER_REUSE_BROWSER="1"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.get_chromium_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._build_worker_command",
                side_effect=[["first-command"], ["second-command"]],
            ) as mock_builder,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                side_effect=[
                    RuntimeError("nodriver worker failed (exit=1): boom"),
                    WORKER_STDOUT.decode(),
                ],
            ) as mock_run,
        ):
            html = await fetch_html_via_nodriver("https://example.com")

        self.assertEqual(html, WORKER_STDOUT.decode())
        self.assertEqual(mock_builder.call_count, 2)
        self.assertIs(mock_builder.call_args_list[0].kwargs["slot"], first)
        self.assertIs(mock_builder.call_args_list[1].kwargs["slot"], second)
        self.assertEqual(
            [call.args[0] for call in mock_run.call_args_list],
            [["first-command"], ["second-command"]],
        )
        # Both slots go back, each once, in the order they were held. A retry
        # that released the stale slot and then let the `finally` release it
        # again would show `[first, first]` -- the shape that hands one browser
        # to two callers.
        self.assertEqual(
            [call.args[0] for call in pool.release.await_args_list], [first, second]
        )

    async def test_pool_slot_is_released_once_when_the_replacement_is_unreachable(
        self,
    ) -> None:
        """Never queue the same slot twice, however the replacement acquire ends

        The restart path releases the failed slot and immediately acquires a
        replacement. If that acquire raises — it is outside the block that
        swallows acquisition errors, and `asyncio.Queue.get` under a `wait_for`
        can be cancelled — the local name still referred to the slot just
        released, and the `finally` released it a second time.

        That is worse than the leak above rather than merely different.
        `ChromiumPool.release` is an unconditional `queue.put` with no
        membership check, so the same slot sits in the queue twice and two
        concurrent callers are handed one browser and one profile directory.
        """
        from kindly_web_search_mcp_server.scrape.chromium_pool import ChromiumSlot
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        slot = ChromiumSlot(slot_id=7, host="127.0.0.7", port=9447)
        pool = AsyncMock()
        pool.acquire.side_effect = [slot, asyncio.CancelledError()]

        with (
            pinned_environment(KINDLY_NODRIVER_REUSE_BROWSER="1"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.get_chromium_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                side_effect=RuntimeError("nodriver worker failed (exit=1): boom"),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await fetch_html_via_nodriver("https://example.com")

        self.assertEqual(
            pool.release.await_count,
            1,
            "the stale slot was released more than once",
        )

    async def test_a_failed_release_leaves_the_stale_slot_recoverable(self) -> None:
        """Keep the slot reachable when handing it back is what fails

        The restart path's four statements are ordered so each failure mode
        leaves the slot in exactly one place, and this is the case for the
        middle one. The local name is cleared **after** the release, not before,
        so a release that raises leaves the slot still bound and the outer
        `finally` returns it. Cleared before, the slot would be stranded — the
        very defect the surrounding fix exists to remove, reintroduced one
        statement further along.

        Near-unreachable in production today: `ChromiumPool.release` is a
        `Diagnostics.emit` — which swallows everything — followed by an
        unbounded `queue.put`. Pinned anyway, because the ordering reads like an
        accident without it, and the cheapest tidy-up available to the next
        author is to move that line back.
        """
        from kindly_web_search_mcp_server.scrape.chromium_pool import ChromiumSlot
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        slot = ChromiumSlot(slot_id=9, host="127.0.0.9", port=9449)
        pool = AsyncMock()
        pool.acquire.return_value = slot
        pool.release.side_effect = [RuntimeError("queue is unwell"), None]

        with (
            pinned_environment(KINDLY_NODRIVER_REUSE_BROWSER="1"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.get_chromium_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                side_effect=RuntimeError("nodriver worker failed (exit=1): boom"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await fetch_html_via_nodriver("https://example.com")

        # Twice: the restart path's attempt, which raised, and the `finally`'s,
        # which is the recovery. Both name the same slot.
        self.assertEqual(pool.release.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in pool.release.await_args_list], [slot, slot]
        )

    async def test_non_retryable_worker_failure_is_not_retried(self) -> None:
        """Retry only the failures the restart classifier actually recognises

        The polarity of `_pool_error_requires_restart`, which the retry case
        above cannot see: a mutation making that predicate always true leaves
        every case that feeds it a matching message passing. This one feeds it a
        message matching none of its patterns and requires exactly one run and
        one build — and the original error, not a second failure's.
        """
        from kindly_web_search_mcp_server.scrape.chromium_pool import ChromiumSlot
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        slot = ChromiumSlot(slot_id=8, host="127.0.0.8", port=9448)
        pool = AsyncMock()
        pool.acquire.return_value = slot

        with (
            pinned_environment(KINDLY_NODRIVER_REUSE_BROWSER="1"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.get_chromium_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._build_worker_command",
                return_value=["only-command"],
            ) as mock_builder,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                side_effect=ValueError("nothing the classifier knows about"),
            ) as mock_run,
        ):
            with self.assertRaises(ValueError) as caught:
                await fetch_html_via_nodriver("https://example.com")

        self.assertEqual(str(caught.exception), "nothing the classifier knows about")
        self.assertEqual(mock_builder.call_count, 1)
        self.assertEqual(mock_run.await_count, 1)
        self.assertEqual(pool.release.await_count, 1)

    async def test_pool_acquisition_failure_falls_back_to_an_unpooled_run(self) -> None:
        """Degrade to a cold browser rather than failing when the pool is unusable

        The acquisition block swallows every exception on purpose: a pool that
        cannot be reached is a performance problem, not a fetch failure. Three
        things follow from that and are asserted together, because the swallow
        makes each of them invisible on its own — the fetch still succeeds, the
        command is built for no slot, and the reason is recorded rather than
        lost.

        Nothing is released: no slot was ever acquired, and a `finally` that
        released on a `None` slot would fail against a real pool.
        """
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        diagnostics = Diagnostics(
            request_id="pool-fallback", enabled=True, stream=io.StringIO()
        )

        with (
            pinned_environment(KINDLY_NODRIVER_REUSE_BROWSER="1"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.get_chromium_pool",
                new_callable=AsyncMock,
                side_effect=RuntimeError("no pool today"),
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_pipe_probe",
                new_callable=AsyncMock,
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._build_worker_command",
                return_value=["unpooled-command"],
            ) as mock_builder,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                return_value=WORKER_STDOUT.decode(),
            ),
        ):
            html = await fetch_html_via_nodriver(
                "https://example.com", diagnostics=diagnostics
            )

        self.assertEqual(html, WORKER_STDOUT.decode())
        self.assertIsNone(mock_builder.call_args.kwargs["slot"])
        self.assertIn("pool.error", [entry["stage"] for entry in diagnostics.entries])

    async def test_caller_side_diagnostics_keep_their_order(self) -> None:
        """Pin the order of the records the loader emits around the worker run

        The extraction moved the timeout parse and its
        `worker.timeout_budget_parent` record into the runner rather than
        resolving the budget in the caller, and the reason given was that the
        record would otherwise change position in the stream. A reason nothing
        checks is a reason that stops being true, so the caller's own sequence
        is pinned here and the runner's in `tests/test_worker_runner.py`.

        Order, not membership: every stage below is emitted on any successful
        diagnostic run, so a set comparison would pass with them shuffled.

        The pipe probe is doubled because it spawns a real interpreter, which is
        not this lane's business — its *position* is the claim, and that
        survives the double.
        """
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        diagnostics = Diagnostics(
            request_id="emit-order", enabled=True, stream=io.StringIO()
        )

        with (
            pinned_environment(),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_pipe_probe",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._run_worker_command",
                new_callable=AsyncMock,
                return_value=WORKER_STDOUT.decode(),
            ),
        ):
            await fetch_html_via_nodriver(
                "https://example.com", diagnostics=diagnostics
            )

        stages = [entry["stage"] for entry in diagnostics.entries]
        self.assertEqual(stages, ["worker.diagnostics_state", "worker.spawn"])
        self.assertTrue(mock_probe.await_count == 1, "the pipe probe did not run")


class TestMarkdownSuffixProbe(unittest.IsolatedAsyncioTestCase):
    """markdown-suffix fast path: wiring, rewrite, allowlist gate, cap, errors."""

    async def test_md_suffix_hit_returns_markdown_without_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        md = "# Title\n\nRendered body from the .md endpoint.\n"
        with (
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_suffix",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_probe.return_value = md
            out = await load_url_as_markdown(
                "https://help.aliyun.com/zh/oss/user-guide/policy"
            )

        self.assertEqual(out, md)
        mock_nodriver.assert_not_called()

    async def test_md_suffix_miss_falls_through_to_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        html = "<html><body><main><h1>Real</h1><p>content</p></main></body></html>"
        with (
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_suffix",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_probe.return_value = None
            mock_nodriver.return_value = html
            out = await load_url_as_markdown(
                "https://help.aliyun.com/zh/oss/user-guide/policy"
            )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Real", out)
        mock_nodriver.assert_called_once()

    def test_build_md_suffix_url_rewrite_cases(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            _build_md_suffix_url,
        )

        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p"),
            "https://help.aliyun.com/zh/oss/p.md",
        )
        # query is preserved and .md lands before it
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p?spm=1"),
            "https://help.aliyun.com/zh/oss/p.md?spm=1",
        )
        # fragment is preserved
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p#sec"),
            "https://help.aliyun.com/zh/oss/p.md#sec",
        )
        # already .md is idempotent
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p.md"),
            "https://help.aliyun.com/zh/oss/p.md",
        )
        # .html -> .md (path segment preserved, not stripped)
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/document_detail/123.html"),
            "https://help.aliyun.com/document_detail/123.md",
        )
        # trailing slash is not a doc leaf -> None
        self.assertIsNone(_build_md_suffix_url("https://help.aliyun.com/zh/oss/"))

    async def test_non_allowlisted_host_skips_probe_no_diagnostic(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )
        from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

        diag = Diagnostics(request_id="t", enabled=True)
        # example.com is not in the allowlist -> probe skips silently (no httpx, no emit)
        with patch.dict(
            "os.environ", {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"}
        ):
            result = await _probe_markdown_suffix(
                "https://example.com/page",
                config=UniversalHtmlLoaderConfig(),
                diagnostics=diag,
            )

        self.assertIsNone(result)
        self.assertFalse(
            any(e["stage"] == "content.md_suffix_probe" for e in diag.entries)
        )

    async def test_md_suffix_probe_caps_overlong_markdown(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )

        big_body = ("x" * 60_000).encode("utf-8")

        class _FakeResp:
            status_code = 200
            headers = {"content-type": "text/markdown; charset=utf-8"}
            content = big_body

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                return _FakeResp()

        with (
            patch.dict(
                "os.environ", {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"}
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            out = await _probe_markdown_suffix(
                "https://help.aliyun.com/zh/oss/p",
                config=UniversalHtmlLoaderConfig(),
            )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("…(truncated)", out)
        self.assertLessEqual(
            len(out), UniversalHtmlLoaderConfig().max_markdown_chars + 64
        )

    async def test_md_suffix_probe_swallows_httpx_errors(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                raise RuntimeError("network down")

        with (
            patch.dict(
                "os.environ", {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"}
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            out = await _probe_markdown_suffix(
                "https://help.aliyun.com/zh/oss/p",
                config=UniversalHtmlLoaderConfig(),
            )

        # never raises into the caller; None -> caller falls back to the browser
        self.assertIsNone(out)

    async def test_md_suffix_probe_rejects_invalid_responses(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )
        from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

        class _FakeResp:
            def __init__(self, status_code, content_type, body):
                self.status_code = status_code
                self.headers = {"content-type": content_type}
                self.content = body

        # each case must fail the four-way gate and return None with a
        # validation_failed miss diagnostic (the self-verifying degradation)
        cases = [
            ("non-200", 404, "text/markdown", b"x" * 2048),
            ("wrong content-type", 200, "text/html", b"x" * 2048),
            ("body under floor", 200, "text/markdown", b"x" * 100),
        ]
        for name, status, ctype, body in cases:
            with self.subTest(name):

                class _FakeClient:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_exc):
                        return False

                    async def get(self, *_args, **_kwargs):
                        return _FakeResp(status, ctype, body)

                diag = Diagnostics(request_id="t", enabled=True)
                with (
                    patch.dict(
                        "os.environ",
                        {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"},
                    ),
                    patch(
                        "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                        return_value=_FakeClient(),
                    ),
                ):
                    out = await _probe_markdown_suffix(
                        "https://help.aliyun.com/zh/oss/p",
                        config=UniversalHtmlLoaderConfig(),
                        diagnostics=diag,
                    )

            self.assertIsNone(out, f"{name}: expected a miss")
            self.assertTrue(
                any(
                    e["stage"] == "content.md_suffix_probe"
                    and e["data"].get("result") == "miss"
                    and e["data"].get("reason") == "validation_failed"
                    for e in diag.entries
                ),
                f"{name}: expected a validation_failed miss diagnostic",
            )


class TestMarkdownAcceptBlanketProbe(unittest.IsolatedAsyncioTestCase):
    """Blanket Accept: text/markdown probe (opt-in, double-fetch on text/html)."""

    async def test_switch_off_does_not_call_blanket_probe(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        html = "<html><body><main><p>browser content</p></main></body></html>"
        with (
            patch.dict("os.environ", {"KINDLY_MARKDOWN_ACCEPT_PROBE": "0"}),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_accept_blanket",
                new_callable=AsyncMock,
            ) as mock_blanket,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_nodriver.return_value = html
            out = await load_url_as_markdown("https://example.com/page")

        mock_blanket.assert_not_called()
        mock_nodriver.assert_called_once()
        self.assertIsNotNone(out)

    async def test_switch_on_hit_skips_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        md = "# Negotiated\n\nBody from the .md-by-Accept endpoint.\n"
        with (
            patch.dict("os.environ", {"KINDLY_MARKDOWN_ACCEPT_PROBE": "1"}),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_accept_blanket",
                new_callable=AsyncMock,
            ) as mock_blanket,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_blanket.return_value = md
            out = await load_url_as_markdown("https://example.com/page")

        self.assertEqual(out, md)
        mock_nodriver.assert_not_called()

    async def test_text_html_falls_through_to_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        # the blanket probe (real) GETs and gets text/html -> miss; browser re-fetches
        class _FakeResp:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"x" * 4096

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, *_args, **_kwargs):
                return _FakeResp()

        html = (
            "<html><body><main><h1>Rendered</h1><p>via browser</p></main></body></html>"
        )
        with (
            patch.dict("os.environ", {"KINDLY_MARKDOWN_ACCEPT_PROBE": "1"}),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_nodriver.return_value = html
            out = await load_url_as_markdown("https://example.com/page")

        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Rendered", out)
        mock_nodriver.assert_called_once()

    async def test_validation_failures_return_none(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_accept_blanket,
        )
        from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

        class _FakeResp:
            def __init__(self, status_code, content_type, body):
                self.status_code = status_code
                self.headers = {"content-type": content_type}
                self.content = body

        cases = [
            ("server returns html", 200, "text/html", b"x" * 2048),
            ("non-200", 404, "text/markdown", b"x" * 2048),
            ("body under floor", 200, "text/markdown", b"x" * 100),
        ]
        for name, status, ctype, body in cases:
            with self.subTest(name):

                class _FakeClient:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_exc):
                        return False

                    async def get(self, *_args, **_kwargs):
                        return _FakeResp(status, ctype, body)

                diag = Diagnostics(request_id="t", enabled=True)
                with patch(
                    "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                    return_value=_FakeClient(),
                ):
                    out = await _probe_markdown_accept_blanket(
                        "https://example.com/page",
                        config=UniversalHtmlLoaderConfig(),
                        diagnostics=diag,
                    )

            self.assertIsNone(out, f"{name}: expected a miss")
            self.assertTrue(
                any(
                    e["stage"] == "content.md_accept_probe"
                    and e["data"].get("result") == "miss"
                    and e["data"].get("reason") == "validation_failed"
                    for e in diag.entries
                ),
                f"{name}: expected a validation_failed miss diagnostic",
            )

    async def test_httpx_error_returns_none(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_accept_blanket,
        )

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, *_args, **_kwargs):
                raise RuntimeError("network down")

        with patch(
            "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
            return_value=_FakeClient(),
        ):
            out = await _probe_markdown_accept_blanket(
                "https://example.com/page",
                config=UniversalHtmlLoaderConfig(),
            )

        # never raises into the caller; None -> caller falls back to the browser
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
