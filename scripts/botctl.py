"""Linux 服务器进程管理；只使用标准库，不导入 Bot 或读取密钥。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
MAIN = ROOT / "main.py"
PID_FILE = ROOT / ".bot.pid"
LOCK_FILE = ROOT / ".bot.lock"
LOG_FILE = ROOT / "bot.log"


@dataclass
class Process:
    pid: int
    start_ticks: str
    args: list[str]


def inspect_process(pid: int) -> Process | None:
    """同时核对工作目录、Python 解释器、入口和启动时间，拒绝复用的 PID。"""
    if pid <= 1:
        return None
    directory = Path("/proc") / str(pid)
    try:
        if (directory / "cwd").resolve(strict=True) != ROOT:
            return None
        if (directory / "exe").resolve(strict=True) != PYTHON.resolve(strict=True):
            return None
        argv = (directory / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        words = [os.fsdecode(arg) for arg in argv]
        index = 1
        if len(words) > index and words[index] == "-u":
            index += 1
        if len(words) <= index:
            return None
        entry = Path(words[index])
        if not entry.is_absolute():
            entry = ROOT / entry
        if entry.resolve() != MAIN:
            return None
        # comm 字段可能包含空格和括号，因此从最后一个右括号后解析。
        fields = (directory / "stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Process(pid, fields[19], words[index + 1:])
    except (OSError, ValueError, IndexError):
        return None


def read_pid() -> Process | None:
    try:
        value = json.loads(PID_FILE.read_text(encoding="utf-8"))
        pid = value["pid"]
        ticks = value["start_ticks"]
        if type(pid) is not int or not isinstance(ticks, str):
            return None
        current = inspect_process(pid)
        return current if current and current.start_ticks == ticks else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_pid(process: Process) -> None:
    fd, name = tempfile.mkstemp(prefix=".bot.pid.", suffix=".tmp", dir=ROOT)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(asdict(process), file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, PID_FILE)
    finally:
        temp.unlink(missing_ok=True)


def find_process() -> Process | None:
    # 兼容此前 nohup 手动启动、没有 PID 文件的 Bot，并检测外部重复启动。
    tracked = read_pid()
    matches = [tracked] if tracked else []
    for directory in Path("/proc").iterdir():
        if directory.name.isdigit():
            pid = int(directory.name)
            if tracked and pid == tracked.pid:
                continue
            process = inspect_process(pid)
            if process:
                matches.append(process)
    if len(matches) > 1:
        raise RuntimeError("发现多个本项目 Bot 进程，未自动操作：" + ", ".join(str(p.pid) for p in matches))
    process = matches[0] if matches else None
    if process:
        save_pid(process)
    else:
        PID_FILE.unlink(missing_ok=True)
    return process


@contextmanager
def management_lock():
    import fcntl

    with LOCK_FILE.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("另一个启动或重启命令正在执行，请稍后再试") from exc
        yield


def same_process(process: Process) -> bool:
    current = inspect_process(process.pid)
    return bool(current and current.start_ticks == process.start_ticks)


def wait_stopped(process: Process, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while same_process(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def stop(process: Process | None, *, timeout: float, force: bool) -> None:
    if process is None:
        print("Bot 已停止。")
        return
    fd = None
    try:
        # Linux 新内核使用 pidfd，使信号绑定到原进程，避免 PID 复用的竞争。
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                fd = os.pidfd_open(process.pid)
            except ProcessLookupError:
                return
            except OSError as exc:
                import errno
                if exc.errno not in (errno.ENOSYS, errno.EINVAL):
                    raise
        if not same_process(process):
            return

        def send(sig):
            if not same_process(process):
                return
            try:
                if fd is not None:
                    signal.pidfd_send_signal(fd, sig)
                else:
                    os.kill(process.pid, sig)
            except ProcessLookupError:
                pass

        print(f"正在停止 Bot（PID {process.pid}）…", flush=True)
        send(signal.SIGTERM)
        if not wait_stopped(process, timeout):
            if not force:
                raise RuntimeError("Bot 未在等待时间内退出；如需强制结束，请加 --force。未启动第二个进程。")
            send(signal.SIGKILL)
            if not wait_stopped(process, 5):
                raise RuntimeError("Bot 尚未退出，已保留 PID 文件，未启动第二个进程")
        PID_FILE.unlink(missing_ok=True)
        print("Bot 已停止。")
    finally:
        if fd is not None:
            os.close(fd)


def start(args: list[str]) -> None:
    if not PYTHON.is_file() or not os.access(PYTHON, os.X_OK):
        raise RuntimeError("找不到可执行的 venv/bin/python，请先创建 venv 并安装依赖")
    if not MAIN.is_file():
        raise RuntimeError("找不到 main.py")
    with LOG_FILE.open("ab", buffering=0) as log:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log.write(f"\n--- Bot 启动 {stamp} ---\n".encode())
        child = subprocess.Popen([str(PYTHON), "-u", str(MAIN), *args], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    try:
        process = inspect_process(child.pid)
        if process is None:
            raise RuntimeError("Bot 启动后立即退出，请查看 bot.log")
        save_pid(process)
    except BaseException:
        # 未能保存管理状态时，不遗留一个本命令创建的无人管理进程。
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    time.sleep(2)
    if child.poll() is not None:
        PID_FILE.unlink(missing_ok=True)
        raise RuntimeError(f"Bot 启动失败（退出码 {child.returncode}），请查看 bot.log")
    print(f"Bot 已在后台启动（PID {child.pid}），日志：{LOG_FILE}")
    print("进程存活检查已通过；QQ 连接状态请查看日志。")


def main() -> int:
    parser = argparse.ArgumentParser(description="AzureSnowBot Linux 启动、停止和重启")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "restart"):
        item = sub.add_parser(command)
        item.add_argument("provider", nargs="?", choices=("qwen",), help="可选：沿用 main.py 的 qwen 启动参数；默认读取现有配置")
        if command == "restart":
            item.add_argument("--force", action="store_true", help="正常退出超时后才强制结束")
            item.add_argument("--timeout", type=float, default=30)
    item = sub.add_parser("stop")
    item.add_argument("--force", action="store_true")
    item.add_argument("--timeout", type=float, default=30)
    sub.add_parser("status")
    options = parser.parse_args()
    if sys.platform != "linux":
        parser.error("此脚本用于 Linux 服务器")
    if getattr(options, "timeout", 30) <= 0:
        parser.error("timeout 必须大于 0")
    try:
        with management_lock():
            process = find_process()
            if options.command == "status":
                print(f"Bot 正在运行（PID {process.pid}）。" if process else "Bot 未运行。")
                return 0 if process else 3
            if options.command == "stop":
                stop(process, timeout=options.timeout, force=options.force)
            elif options.command == "start" and process:
                print(f"Bot 已在运行（PID {process.pid}），未重复启动。")
            else:
                args = [options.provider] if options.provider else (process.args if process else [])
                if options.command == "restart":
                    stop(process, timeout=options.timeout, force=options.force)
                start(args)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
