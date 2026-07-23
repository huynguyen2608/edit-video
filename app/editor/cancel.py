"""Hủy an toàn các tiến trình ngoài trong pipeline biên tập."""
from __future__ import annotations

import subprocess
import tempfile
import time
from typing import Callable, Optional

CancelCb = Optional[Callable[[], bool]]


class EditCancelled(Exception):
    """Người dùng chủ động dừng video hiện tại."""


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def run_cancellable(cmd, *, cancel_cb: CancelCb = None, text: bool = False,
                    capture_output: bool = True, check: bool = False):
    """Tương tự subprocess.run nhưng có thể dừng process đang chạy."""
    if cancel_cb and cancel_cb():
        raise EditCancelled("Đã dừng video hiện tại")
    mode = "w+" if text else "w+b"
    stdout_file = tempfile.TemporaryFile(mode=mode)
    stderr_file = tempfile.TemporaryFile(mode=mode)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_file if capture_output else None,
            stderr=stderr_file if capture_output else None,
            text=text,
        )
        while proc.poll() is None:
            if cancel_cb and cancel_cb():
                stop_process(proc)
                raise EditCancelled("Đã dừng video hiện tại")
            time.sleep(0.1)
        proc.wait()
        stdout = stderr = None
        if capture_output:
            stdout_file.seek(0); stderr_file.seek(0)
            stdout, stderr = stdout_file.read(), stderr_file.read()
    finally:
        stdout_file.close(); stderr_file.close()
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result
