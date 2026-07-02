"""Tests for the cooperative Ctrl-C / SIGINT abort path.

The abort flow is:

1. :func:`epub_commentor.llm._abort.install_sigint_handler` installs a
   two-stage handler: the first SIGINT sets a module-level
   :class:`threading.Event` and prints a hint, the second SIGINT calls
   :func:`os._exit` so a stuck worker cannot block shutdown.
2. :class:`epub_commentor.llm.executor.LLMExecutor._invoke_model` polls
   the flag between streaming chunks and closes the underlying httpx
   stream when set, raising :class:`CommentAbortError`.
3. :class:`epub_commentor.llm.context.LLMContext.request` short-circuits
   at the top so a doomed network round-trip is never made.
4. :func:`epub_commentor.cli.main` catches :class:`CommentAbortError`
   and returns 130 with a clean "aborted by user." message.
"""

from __future__ import annotations

import io
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor.cli import main as cli_main
from epub_commentor.config import CommentConfig
from epub_commentor.errors import CommentAbortError
from epub_commentor.llm._abort import (
    install_sigint_handler,
    is_abort_requested,
    request_abort,
    reset_abort,
    restore_sigint_handler,
)
from epub_commentor.llm.executor import LLMExecutor
from epub_commentor.llm.statistics import Statistics
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.pipeline.process import process_chapters
from epub_commentor.xml.xml_like import XMLLikeNode


@pytest.fixture(autouse=True)
def _reset_abort_state() -> Iterator[None]:
    """Ensure each test starts with the abort flag cleared and the
    SIGINT handler back to the previous install state.

    The module-level flag is process-wide; without this fixture an
    earlier failure would leak into the next test.
    """
    saved_handler = signal.getsignal(signal.SIGINT)
    reset_abort()
    # Also force-restore to the default in case a previous test installed ours.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    yield
    reset_abort()
    # Best-effort restore; if a test installed our handler, this would
    # be safe because the test is responsible for restoring it.
    try:
        signal.signal(signal.SIGINT, saved_handler)
    except Exception:  # noqa: BLE001 - defensive
        pass


class TestAbortFlag:
    def test_default_unset(self) -> None:
        assert is_abort_requested() is False

    def test_request_abort_sets_flag(self) -> None:
        assert is_abort_requested() is False
        request_abort()
        assert is_abort_requested() is True

    def test_reset_abort_clears_flag(self) -> None:
        request_abort()
        assert is_abort_requested() is True
        reset_abort()
        assert is_abort_requested() is False


class TestInstallSigintHandler:
    def test_install_sets_flag_on_first_signal(self) -> None:
        install_sigint_handler()
        try:
            handler = signal.getsignal(signal.SIGINT)
            assert handler is not None
            # The handler should be a plain function (not SIG_DFL / SIG_IGN).
            assert callable(handler)
            # Invoke it directly: first call sets the flag + prints hint.
            handler(signal.SIGINT, None)  # type: ignore[arg-type]
            assert is_abort_requested() is True
        finally:
            restore_sigint_handler()

    def test_install_is_idempotent(self) -> None:
        install_sigint_handler()
        first = signal.getsignal(signal.SIGINT)
        install_sigint_handler()  # second call should be a no-op
        second = signal.getsignal(signal.SIGINT)
        try:
            assert first is second
        finally:
            restore_sigint_handler()

    def test_restore_returns_to_previous(self) -> None:
        # Replace with a sentinel first so we can detect restoration.
        def sentinel(signum: int, frame: object | None) -> None:
            return None

        signal.signal(signal.SIGINT, sentinel)
        install_sigint_handler()
        assert signal.getsignal(signal.SIGINT) is not sentinel
        restore_sigint_handler()
        # After restore, the previous handler (sentinel) should be back.
        assert signal.getsignal(signal.SIGINT) is sentinel
        # Cleanup
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    def test_second_sigint_force_exits(self) -> None:
        """Second Ctrl-C calls os._exit(130) — verify via a subprocess."""
        # We invoke a tiny script in a child Python: install handler,
        # trigger once (sets flag), trigger again (os._exit). The exit
        # code should be 130.
        project_root = str(Path(__file__).resolve().parent.parent).replace("\\", "\\\\")
        script = (
            "import sys, os, signal\n"
            f"sys.path.insert(0, r'{project_root}')\n"
            "from epub_commentor.llm._abort import install_sigint_handler, request_abort\n"
            "install_sigint_handler()\n"
            "request_abort()  # simulate first Ctrl-C side-effect\n"
            "handler = signal.getsignal(signal.SIGINT)\n"
            "handler(signal.SIGINT, None)  # would normally os._exit(130)\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 130, (
            f"expected exit 130, got {result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )


class TestExecutorAborts:
    def test_request_short_circuits_when_flag_set(self) -> None:
        """``LLMExecutor.request`` should bail before any network call."""
        executor = LLMExecutor(
            api_key="x",
            url="http://127.0.0.1:1/",
            model="m",
            timeout=0.1,
            retry_times=0,
            retry_interval_seconds=0.0,
            statistics=Statistics(),
        )
        request_abort()
        with pytest.raises(CommentAbortError):
            # url points at a closed port; if abort didn't short-circuit
            # this would raise a connection error instead.
            executor.request(messages=[], max_tokens=None, temperature=None, top_p=None, cache_key=None)

    def test_invoke_model_raises_on_abort_and_closes_stream(self) -> None:
        """``_invoke_model`` should raise ``CommentAbortError`` and close the stream."""
        executor = LLMExecutor(
            api_key="x",
            url="http://127.0.0.1:1/",
            model="m",
            timeout=0.1,
            retry_times=0,
            retry_interval_seconds=0.0,
            statistics=Statistics(),
        )
        closed = {"value": False}

        class _FakeStream:
            def __iter__(self) -> _FakeStream:
                return self

            def __next__(self) -> object:
                # Trigger abort the moment the consumer starts iterating.
                request_abort()
                # Yield something that looks like a chunk so the loop body runs.
                return _make_fake_chunk("hello")

            def close(self) -> None:
                closed["value"] = True

        # Patch the executor's client to return our fake stream.
        executor._client = _FakeClient(_FakeStream())  # type: ignore[assignment]

        with pytest.raises(CommentAbortError):
            executor._invoke_model(input_messages=[], top_p=None, temperature=None, max_tokens=None)
        assert closed["value"] is True, "stream.close() was not called on abort"


def _make_fake_chunk(content: str) -> object:
    """Build an object that mimics the shape consumed by ``_invoke_model``."""

    class _Delta:
        def __init__(self, c: str) -> None:
            self.content = c

    class _Choice:
        def __init__(self, c: str) -> None:
            self.delta = _Delta(c)

    class _Chunk:
        def __init__(self, c: str) -> None:
            self.choices = [_Choice(c)]
            self.usage = None

    return _Chunk(content)


class _FakeClient:
    """Minimal stand-in for ``openai.OpenAI`` exposing a streaming ``create``.

    The real SDK exposes ``client.chat.completions.create(...)`` where
    ``chat`` and ``completions`` are chainable sub-instances. We model
    that with properties that return ``self``, so any access chain lands
    back here for ``create``.
    """

    def __init__(self, stream: object) -> None:
        self._stream = stream

    @property
    def chat(self) -> _FakeClient:
        return self

    @property
    def completions(self) -> _FakeClient:
        return self

    def create(self, **kwargs: object) -> object:  # noqa: ARG002 - mirror SDK signature
        return self._stream


class TestContextShortCircuit:
    def test_context_request_raises_when_flag_set(self, tmp_path: Path) -> None:
        """``LLMContext.request`` should not touch the executor when aborted."""
        from epub_commentor.llm.context import LLMContext
        from epub_commentor.llm.increasable import Increasable
        from epub_commentor.llm.types import Message, MessageRole

        executor = LLMExecutor(
            api_key="x",
            url="http://127.0.0.1:1/",
            model="m",
            timeout=0.1,
            retry_times=0,
            retry_interval_seconds=0.0,
            statistics=Statistics(),
        )
        ctx = LLMContext(
            executor=executor,
            cache_path=tmp_path,
            cache_seed_content="seed",
            top_p=Increasable(None),
            temperature=Increasable(None),
        )
        request_abort()
        with pytest.raises(CommentAbortError):
            ctx.request([Message(MessageRole.USER, "hello")])


class TestProcessChaptersAborts:
    def test_abort_propagates_through_process_chapters(self, tmp_path: Path) -> None:
        """``process_chapters`` should re-raise ``CommentAbortError`` from a worker."""
        body_xml = "<html><body>" + "".join(f"<p>p{i}</p>" for i in range(8)) + "</body></html>"
        root = fromstring(body_xml)
        body = root.find("body")
        assert body is not None
        xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
        xml_node.element = root
        chapter = Chapter(path=tmp_path / "ch.xhtml", title="ch", body=body, xml_node=xml_node)

        # Stage 1 succeeds (any valid memo), Stage 2 raises abort on
        # the very first block — process_chapters should propagate it.
        memo_json = json_dumps(
            {
                "core_thesis": "x",
                "outline": ["a", "b", "c"],
                "tone": "t",
                "target_audience": "g",
            }
        )
        # Wrap the real annotate_block so it raises CommentAbortError
        # immediately — simulates an aborted worker.
        import epub_commentor.pipeline.process as process_mod

        original = process_mod.annotate_block

        def fake_abort(*args: object, **kwargs: object) -> object:
            request_abort()
            raise CommentAbortError("aborted by user")

        process_mod.annotate_block = fake_abort  # type: ignore[assignment]
        try:
            llm = MockLLM(
                responses_by_seed={
                    "scan__response": memo_json,
                    "annotate__response": json_dumps({"comments": []}),
                }
            )
            with pytest.raises(CommentAbortError):
                process_chapters(
                    chapters=[chapter],
                    book_metadata={},
                    llm=llm,
                    config=CommentConfig(concurrency=2, block_size=4),
                )
        finally:
            process_mod.annotate_block = original  # type: ignore[assignment]
            reset_abort()


class TestCliMainReturns130:
    def test_cli_main_returns_130_on_abort(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``cli.main`` should return 130 and print 'aborted by user.'."""
        import epub_commentor.cli as cli_mod

        # Stub out comment_epub to raise CommentAbortError instead of
        # running the full pipeline.
        def fake_comment_epub(*args: object, **kwargs: object) -> object:
            raise CommentAbortError("aborted by user")

        monkeypatch.setattr(cli_mod, "comment_epub", fake_comment_epub)

        # Build a minimal argv so argparse doesn't fail.
        monkeypatch.setattr(sys, "argv", ["epub-commentor", str(tmp_path / "in.epub")])

        # Create a dummy source EPUB (we won't actually read it because
        # comment_epub is stubbed). Use an empty file.
        src = tmp_path / "in.epub"
        src.write_bytes(b"")

        # Inject a fake API key via env so ``_construct_llm`` doesn't bail
        # out before ``comment_epub`` is invoked. Without this the test
        # leaks the developer's local ``format.json`` state into it.
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake-key-for-test")

        rc = cli_main()
        assert rc == 130
        captured = capsys.readouterr()
        assert "aborted by user." in captured.err
