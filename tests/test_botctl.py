"""在临时项目中管理真实 Linux 子进程，不加载 NoneBot 或连接 QQ。"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="服务器脚本使用 Linux /proc 和 flock")


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "project with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "venv/bin").mkdir(parents=True)
    (root / "venv/bin/python").symlink_to(sys.executable)
    shutil.copyfile(ROOT / "bot.sh", root / "bot.sh")
    shutil.copyfile(ROOT / "scripts/botctl.py", root / "scripts/botctl.py")
    (root / "main.py").write_text("import time\nprint('fixture started', flush=True)\nwhile True: time.sleep(.05)\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("botctl_fixture", root / "scripts/botctl.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    def run(*args):
        return subprocess.run(["bash", str(root / "bot.sh"), *args], cwd=tmp_path,
                              capture_output=True, text=True, encoding="utf-8", timeout=12)

    yield root, mod, run
    # 仅结束本测试临时目录内的 main.py，绝不根据未校验的 PID 文件杀进程。
    for directory in Path("/proc").iterdir():
        if directory.name.isdigit():
            process = mod.inspect_process(int(directory.name))
            if process:
                try:
                    os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    sys.modules.pop(spec.name, None)


def test_start_restart_stop_and_duplicate_prevention(service):
    root, mod, run = service
    assert run("status").returncode == 3
    started = run("start")
    assert started.returncode == 0, started.stderr
    first = mod.read_pid()
    assert first and mod.same_process(first)
    assert run("start").returncode == 0
    assert mod.read_pid().pid == first.pid
    assert run("status").returncode == 0
    restarted = run("restart")
    assert restarted.returncode == 0, restarted.stderr
    second = mod.read_pid()
    assert second and second.pid != first.pid
    assert not mod.same_process(first)
    assert run("stop").returncode == 0
    assert not mod.PID_FILE.exists()
    assert run("status").returncode == 3
    assert run("stop").returncode == 0
    assert (root / "bot.log").read_text(encoding="utf-8").count("fixture started") == 2


def test_adopts_legacy_nohup_process_and_preserves_arguments(service):
    root, mod, run = service
    old = subprocess.Popen([str(root / "venv/bin/python"), "main.py", "qwen"], cwd=root,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert run("start").returncode == 0
        assert mod.read_pid().pid == old.pid
        assert run("restart").returncode == 0
        assert mod.read_pid().args == ["qwen"]
        old.wait(timeout=3)
        assert run("stop").returncode == 0
    finally:
        if old.poll() is None:
            old.kill()
            old.wait()


def test_stale_pid_cannot_stop_unrelated_python(service):
    root, mod, run = service
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=root,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        mod.PID_FILE.write_text(json.dumps({"pid": other.pid, "start_ticks": "stale"}), encoding="utf-8")
        assert run("stop").returncode == 0
        assert other.poll() is None
        assert run("start").returncode == 0
        assert mod.read_pid().pid != other.pid
        assert run("stop").returncode == 0
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_immediate_startup_failure_removes_pid_but_keeps_log(service):
    root, mod, run = service
    (root / "main.py").write_text("print('startup failed fixture', flush=True)\nraise SystemExit(7)\n", encoding="utf-8")
    mod.LOG_FILE.write_text("previous log\n", encoding="utf-8")
    result = run("start")
    assert result.returncode == 1
    assert not mod.PID_FILE.exists()
    assert "previous log" in mod.LOG_FILE.read_text(encoding="utf-8")
    assert "startup failed fixture" in mod.LOG_FILE.read_text(encoding="utf-8")


def test_timeout_requires_explicit_force_and_never_starts_duplicate(service):
    root, mod, run = service
    (root / "main.py").write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(.05)\n", encoding="utf-8")
    assert run("start").returncode == 0
    original = mod.read_pid()
    result = run("restart", "--timeout", "0.2")
    assert result.returncode == 1 and "--force" in result.stderr
    assert mod.read_pid().pid == original.pid
    result = run("restart", "--timeout", "0.2", "--force")
    assert result.returncode == 0, result.stderr
    assert not mod.same_process(original)
    assert run("stop", "--timeout", "0.2", "--force").returncode == 0


def test_concurrent_starts_are_serialized(service):
    root, mod, run = service
    one = subprocess.Popen(["bash", str(root / "bot.sh"), "start"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 4
    while not mod.PID_FILE.exists() and one.poll() is None and time.monotonic() < deadline:
        time.sleep(.02)
    second = run("start")
    assert second.returncode == 1
    assert "正在执行" in second.stderr
    _, error = one.communicate(timeout=8)
    assert one.returncode == 0, error
    assert mod.find_process() is not None
    assert run("stop").returncode == 0
