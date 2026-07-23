"""Hàng đợi edit chạy MỘT luồng liên tục, xuyên suốt danh sách video cần sửa.

- Một worker thread DUY NHẤT rút từng video khỏi hàng đợi và edit lần lượt (không
  bao giờ chạy hai edit song song).
- Khi một video mới tải xong -> enqueue(video_id): nếu worker đang bận thì chỉ THÊM
  vào hàng đợi rồi xử lý khi tới lượt; nếu đang rảnh thì bắt đầu ngay.
- start(initial_ids): nạp sẵn backlog (video 'downloaded' + chưa edit) rồi chạy tiếp.
- dedup: một video_id chỉ nằm trong hàng đợi / đang xử lý đúng một lần.

Đây là phần "1 luồng auto xuyên suốt list" + "tải xong báo sang edit, đang bận thì
nối đuôi list".
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Iterable, Optional

from ..logging_setup import get_logger

log = get_logger("edit_queue")
LogCb = Callable[[str], None]


class EditQueueService:
    def __init__(self, process_fn: Callable[[str], object], on_log: Optional[LogCb] = None,
                 on_item_done: Optional[Callable[[str], None]] = None):
        self._process = process_fn                 # callable(video_id) -> edit 1 video
        self.on_log = on_log or (lambda m: None)
        self._on_item_done = on_item_done or (lambda vid: None)  # gọi sau mỗi video xong
        self._cv = threading.Condition()
        self._queue: deque[str] = deque()
        self._inflight: set[str] = set()           # đang trong hàng đợi hoặc đang xử lý
        self._processing: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = False

    def start(self, initial_ids: Iterable[str] = ()) -> None:
        for vid in initial_ids:
            self.enqueue(vid)
        self._ensure_worker()

    def enqueue(self, video_id: str) -> bool:
        """Thêm video vào hàng đợi edit. Trả False nếu đã có trong đợi/đang xử lý."""
        with self._cv:
            if self._stop or video_id in self._inflight:
                return False
            self._inflight.add(video_id)
            self._queue.append(video_id)
            self._cv.notify_all()
        self._log(f"+ vào hàng đợi edit: {video_id} (đang chờ: {self.pending_count()})")
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        with self._cv:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="edit-queue", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait()
                if self._stop and not self._queue:
                    self._processing = None
                    return
                vid = self._queue.popleft()
                self._processing = vid
            try:
                self._process(vid)              # pipeline tự cập nhật trạng thái + log
            except Exception as e:              # phòng hờ; pipeline đã tự set 'failed'
                log.exception("edit queue lỗi %s", vid)
                self._log(f"! lỗi edit {vid}: {e}")
            finally:
                with self._cv:
                    self._inflight.discard(vid)
                    self._processing = None
                    self._cv.notify_all()
            try:
                self._on_item_done(vid)      # ngoài lock: báo UI refresh, v.v.
            except Exception:
                log.exception("on_item_done lỗi %s", vid)

    # ---------------- trạng thái ----------------
    def pending_count(self) -> int:
        with self._cv:
            return len(self._queue)

    def is_busy(self) -> bool:
        with self._cv:
            return self._processing is not None or bool(self._queue)

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Chờ tới khi hàng đợi rỗng và không còn video đang xử lý (cho test/CLI)."""
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._queue and self._processing is None, timeout=timeout)

    def stop(self, wait: bool = True) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        t = self._thread
        if wait and t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10)

    def _log(self, msg: str) -> None:
        log.info(msg)
        self.on_log(msg)
