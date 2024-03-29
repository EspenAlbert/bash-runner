import logging
import sys
import time
from json import loads
from os import getenv

import pytest
from bash_runner import (
    BashConfig,
    BashError,
    BashRun,
    kill,
    print_with_override,
    run,
    run_and_wait,
    stop_runs_and_pool,
    wait_on_ok_errors,
)

PYTHON_EXEC = sys.executable
running_in_pants = bool(getenv("RUNNING_IN_PANTS", ""))
pants_skip = pytest.mark.skipif(running_in_pants, reason="pants env incompatible")
logger = logging.getLogger(__name__)


@pytest.fixture(scope="session", autouse=True)
def stop_consumer():
    yield
    stop_runs_and_pool()


def test_normal_run():
    result = run_and_wait(BashConfig(script="echo hello"))
    assert result.stdout == "hello"
    assert result.stderr == ""


def test_async_run():
    result = run(BashConfig(script="sleep 1 && echo hello"))
    assert result.is_running
    result.wait_until_complete(timeout=2)
    assert result.stdout == "hello"
    assert result.stderr == ""


def test_error_run():
    with pytest.raises(BashError) as exc:
        run_and_wait(BashConfig(script="unknown hello"))
    assert exc.value.stderr == "bash: unknown: command not found"


def test_invalid_popen_args():
    with pytest.raises(TypeError) as exc:
        run_and_wait(BashConfig("echo ok", extra_popen_kwargs=dict(unknown=True)))
    assert "unexpected keyword" in str(exc.value)


def test_allow_non_zero_exit():
    with pytest.raises(BashError):
        run_and_wait("exit 1")
    result = run_and_wait(BashConfig("exit 1", allow_non_zero_exit=True))
    assert result.exit_code == 1


def test_custom_env():
    result = run_and_wait(BashConfig("echo $MY_VAR", env={"MY_VAR": "ok"}))
    assert "ok" in result.stdout


_attempt_script = """\
from pathlib import Path
attempt_path = Path(__file__).with_name("attempt")
if attempt_path.exists():
    current_attempt = int(attempt_path.read_text())
else:
    current_attempt = 1
attempt_path.write_text(str(current_attempt+1))
print(f"attempt in script: {current_attempt}/3")
if current_attempt < 3:
    raise Exception("sys_error")
"""


@pants_skip
def test_multiple_attempts(tmp_path):
    """error might look weird due to flushing"""
    script_path = tmp_path / "attempt.py"
    script_path.write_text(_attempt_script)
    result = run_and_wait(BashConfig(script=f"{PYTHON_EXEC} {script_path}", attempts=3))
    assert result.clean_complete


@pants_skip
def test_not_enough_attempts(tmp_path):
    script_path = tmp_path / "attempt.py"
    script_path.write_text(_attempt_script)
    with pytest.raises(BashError) as exc:
        run_and_wait(BashConfig(script=f"{PYTHON_EXEC} {script_path}", attempts=2))
    assert "attempt in script: 2/3" in exc.value.stdout
    assert exc.value.exit_code == 1


@pants_skip
def test_multi_attempts_retry_call_false(tmp_path):
    script_path = tmp_path / "attempt.py"
    script_path.write_text(_attempt_script)

    def never_retry(_):
        return False

    with pytest.raises(BashError) as exc:
        run_and_wait(
            BashConfig(
                script=f"{PYTHON_EXEC} {script_path}",
                attempts=4,
                should_retry=never_retry,
            )
        )
    assert "attempt in script: 1/3" in exc.value.stdout


@pants_skip
def test_multi_attempts_retry_call_true(tmp_path):
    script_path = tmp_path / "attempt.py"
    script_path.write_text(_attempt_script)

    def retry_if_attempt(run: BashRun):
        return "attempt in script" in run.stdout

    result = run_and_wait(
        BashConfig(
            script=f"{PYTHON_EXEC} {script_path}",
            attempts=4,
            should_retry=retry_if_attempt,
        )
    )
    assert "attempt in script: 3/3" in result.stdout


_parse_json_stdout = """\
{
  "check": true,
  "can_I_parse": [
    1,
    2,
    3
  ]
}"""


def test_parse_json(tmp_path):
    filename = "example.json"
    json_path = tmp_path / filename
    json_path.write_text(_parse_json_stdout)
    result = run_and_wait(BashConfig(f"cat {filename}", cwd=tmp_path))
    time.sleep(0.1)
    stdout = result.stdout
    logger.info(stdout)
    parsed = loads(stdout)
    assert parsed["check"]
    assert parsed["can_I_parse"] == [1, 2, 3]


_slow_script = """\
import time
print("start_sleep")
time.sleep(10)
print("sleep_done")

"""


@pants_skip
@pytest.mark.parametrize("immediate", [True, False])
def test_kill_process(tmp_path, immediate):
    filename = "sleeper.py"
    file_path = tmp_path / filename
    file_path.write_text(_slow_script)
    started = run(BashConfig(f"{PYTHON_EXEC} {filename}", cwd=tmp_path))
    time.sleep(0.5)
    start = time.monotonic()
    popen = started.p_open
    assert popen, "no process to kill"
    kill(
        popen,
        immediate=immediate,
        reason="test-kill",
        prefix=started.config.print_prefix,
    )
    duration = time.monotonic() - start
    assert duration < 4
    assert "start_sleep" in started.stdout
    assert "sleep_done" not in started.stdout
    if not immediate:
        assert "KeyboardInterrupt" in started.stderr


_continue_after_abort = """
import time
print("start_sleep")
try:
    time.sleep(10)
except KeyboardInterrupt:
    print("interrupted")
    time.sleep(1)
    print("completing OK")
"""


def test_adding_callback_to_bash_run():
    result = run("sleep 1")
    flag = False

    def called():
        nonlocal flag
        flag = True

    result.add_done_callback(called)
    result.wait_until_complete(timeout=2)
    assert flag


def test_adding_failing_callback_to_bash_run():
    result = run("sleep 1")
    flag = False

    def called():
        nonlocal flag
        flag = True
        raise Exception("what will happen?")

    result.add_done_callback(called)
    result.wait_until_complete(timeout=2)
    assert flag


@pants_skip
def test_allow_process_to_finish(tmp_path):
    filename = "continue_after_abort.py"
    file_path = tmp_path / filename
    file_path.write_text(_continue_after_abort)
    bash_run = run(BashConfig(f"{PYTHON_EXEC} {filename}", cwd=tmp_path))
    time.sleep(0.5)
    start = time.monotonic()
    popen = bash_run.p_open
    assert popen, "no process"
    kill(popen, immediate=False, abort_timeout=3)
    stdout = bash_run.stdout
    duration = time.monotonic() - start
    assert 1 < duration < 2
    assert "start_sleep" in stdout
    assert "completing OK" in stdout


def test_parallel_runs():
    results = [run(BashConfig(f"sleep {i} && echo {i}")) for i in range(4)]
    start = time.time()
    for result in results:
        result.wait_until_complete()
    assert time.time() - start < 4


@pytest.mark.parametrize("call_old", [False, True])
def test_print_with_override(call_old):
    called = False

    def overrider(*args, **kwargs):
        nonlocal called
        called = True

    with print_with_override(overrider, call_old=call_old):
        run_and_wait("echo ok")
    assert called


def test_wait_safely_on_ok_failures_all_ok():
    runs = [run(f"echo {i}") for i in range(10)]
    oks, errors = wait_on_ok_errors(*runs, timeout=1)
    assert not errors
    assert all(run.clean_complete for run in oks)
