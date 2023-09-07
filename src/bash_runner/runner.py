from __future__ import annotations
import atexit
import logging
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, Future, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from os import getenv, setsid
from typing import Callable

from bash_runner.colors import ContentType
from bash_runner.models import BashConfig, BashRun, StartResult, BashError
from bash_runner.printer import print_with, log_exception

logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=int(getenv("BASH_THREAD_COUNT", "50")))
_STDOUT = ContentType.STDOUT
_STDERR = ContentType.STDERR
_ERROR = ContentType.ERROR
_WARNING = ContentType.WARNING


def _read_until_complete(
    proc: subprocess.Popen,
    is_stdout: bool,
    prefix: str,
    result: list[str],
    ansi_content: bool,
):
    stream = proc.stdout if is_stdout else proc.stderr
    content_type = _STDOUT if is_stdout else _STDERR
    try:
        for line in iter(stream.readline, ""):
            result.append(line)
            print_with(
                line.strip("\n"),
                prefix=prefix,
                content_type=content_type,
                ansi_content=ansi_content,
            )
    except ValueError as e:
        if "I/O operation on closed file" in str(e):
            return
        print_with(repr(e), prefix=prefix, content_type=_ERROR)
        log_exception(e)


def run(config: BashConfig | str) -> BashRun:
    if isinstance(config, str):
        config = BashConfig(script=config)
    on_started = Future()
    _pool.submit(_execute_run, config, on_started)
    return on_started.result()


def run_and_wait(config: BashConfig | str) -> BashRun:
    if isinstance(config, str):
        config = BashConfig(script=config)
    run = _execute_run(config)
    run.wait_until_complete()
    return run


_runs: dict[int, BashRun] = {}


@contextmanager
def _track_run(bash_run: BashRun):
    key = id(bash_run)
    _runs[key] = bash_run
    try:
        yield
    except (KeyboardInterrupt, InterruptedError) as e:
        logger.info(f"interrupt: {e!r}")
        stop_runs_and_pool()
        bash_run._complete()
    finally:
        _runs.pop(key, None)


def kill_all_runs(
    immediate: bool = False, reason: str = "", abort_timeout: float = 3.0
):
    for run in _runs.values():
        kill(
            run.p_open,
            immediate=immediate,
            reason=reason,
            abort_timeout=abort_timeout,
            prefix=run.config.print_prefix,
        )


def stop_runs_and_pool():
    print_with(
        "STOPPING stop_runs_and_pool", prefix="_script_runner", content_type=_WARNING
    )
    kill_all_runs(reason="atexit")
    _pool.shutdown(wait=True)


atexit.register(stop_runs_and_pool)


def _execute_run(config: BashConfig, on_started: Future | None = None) -> BashRun:
    bash_run = BashRun(config)
    for attempt in range(1, config.attempts + 1):
        prefix = config.print_prefix
        if attempt > 1:
            prefix += f"-{attempt}"
            print_with(f"attempt: {attempt}", prefix=prefix, content_type=_WARNING)
        is_last_attempt = attempt == config.attempts
        if result := _attempt_run(bash_run, prefix, on_started, is_last_attempt):
            return result
        if retry_call := config.should_retry:
            if not retry_call(bash_run):
                bash_run._complete()
                break
    return bash_run


@dataclass
class _FutureContext:
    run: BashRun
    start_future: Future = field(default_factory=Future, init=False)

    def result(self) -> None:
        self.start_future.result()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            self.start_future.set_exception(exc_val)
            self.run._complete(exc_val)

    def on_started(self, start: StartResult):
        self.run._set_start_result(start)
        if not self.start_future.done():
            self.start_future.set_result(self.run)


def _attempt_run(
    bash_run: BashRun, prefix: str, on_started: Future | None, is_last_attempt: bool
) -> BashRun:
    config = bash_run.config
    start_future = _FutureContext(bash_run)

    def _start_in_thread():
        with start_future:
            _run(
                prefix,
                config.script,
                start_future.on_started,
                config.popen_kwargs,
                config.ansi_content,
            )

    run_future = _pool.submit(_start_in_thread)
    start_future.result()
    with _track_run(bash_run):
        try:
            if on_started and not on_started.done():
                on_started.set_result(bash_run)
            run_future.result()
            if bash_run.clean_complete:
                bash_run._complete()
                return bash_run
            if is_last_attempt or config.allow_non_zero_exit:
                bash_run._complete()
        except Exception as e:
            print_with(repr(e), prefix=prefix, content_type=_ERROR)
            log_exception(e)
            if is_last_attempt:
                base_error = run_future.exception()
                error = BashError(bash_run, base_error=base_error)
                bash_run._complete(error)
                raise error from e


def _run(
    prefix: str,
    script: str,
    process_started: Callable[[StartResult], None],
    kwargs: dict,
    ansi_content: bool,
) -> None:
    kwargs = (
        dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=setsid,
            universal_newlines=True,
            shell=False,
        )
        | kwargs
    )
    stdout_result: list[str] = []
    stderr_result: list[str] = []
    with subprocess.Popen(["bash", "-c", script], **kwargs) as proc:
        process_started(StartResult(proc, stdout_result, stderr_result))

        def read_stdout():
            _read_until_complete(
                proc,
                is_stdout=True,
                prefix=prefix,
                result=stdout_result,
                ansi_content=ansi_content,
            )

        def read_stderr():
            _read_until_complete(
                proc,
                is_stdout=False,
                prefix=prefix,
                result=stderr_result,
                ansi_content=ansi_content,
            )

        fut_stdout = _pool.submit(read_stdout)
        fut_stderr = _pool.submit(read_stderr)
        wait([fut_stdout, fut_stderr])


def kill(
    proc: subprocess.Popen,
    immediate: bool = False,
    reason: str = "",
    prefix: str = "",
    abort_timeout: float = 3.0,
):
    """https://stackoverflow.com/questions/4789837/how-to-terminate-a-python-subprocess-launched-with-shell-true"""
    if proc.returncode is not None:
        # already finished
        return

    def warn(message: str):
        print_with(message, prefix=prefix, content_type=_WARNING)

    warn(f"killing: {reason}")
    try:
        if immediate:
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=abort_timeout)
        warn("killing complete")
    except subprocess.TimeoutExpired:
        warn(f"timeout after {abort_timeout}s! forcing a kill")
        proc.terminate()
    except (OSError, ValueError) as e:
        warn(f"unable to get output when shutting down {e!r}")


if __name__ == "__main__":
    _, *script_args = sys.argv
    run_and_wait(BashConfig(" ".join(script_args)))
