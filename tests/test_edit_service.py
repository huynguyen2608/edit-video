"""Test hàng đợi edit 1 luồng: FIFO, dedup khi đang xử lý, nối đuôi khi bận, lỗi
không chặn video sau."""
import threading

from app.editor.edit_service import EditQueueService


def test_processes_backlog_in_fifo_order():
    order = []
    svc = EditQueueService(lambda vid: order.append(vid))
    svc.start(initial_ids=["a", "b", "c"])
    assert svc.wait_idle(timeout=5)
    svc.stop()
    assert order == ["a", "b", "c"]


def test_dedup_while_inflight():
    started, release, order = threading.Event(), threading.Event(), []

    def proc(vid):
        order.append(vid)
        started.set()
        release.wait(timeout=5)

    svc = EditQueueService(proc)
    assert svc.enqueue("x") is True
    assert started.wait(timeout=5)         # 'x' đang xử lý
    assert svc.enqueue("x") is False       # dedup: vẫn đang inflight
    release.set()
    assert svc.wait_idle(timeout=5)
    svc.stop()
    assert order == ["x"]


def test_enqueue_while_busy_is_sequential():
    started, release, order = threading.Event(), threading.Event(), []

    def proc(vid):
        order.append(vid)
        if vid == "first":
            started.set()
            release.wait(timeout=5)        # giữ worker bận

    svc = EditQueueService(proc)
    svc.enqueue("first")
    assert started.wait(timeout=5)         # đang xử lý 'first'
    svc.enqueue("second")                  # đang bận -> chỉ thêm vào hàng đợi
    assert svc.is_busy()
    assert svc.pending_count() == 1        # 'second' đang chờ, chưa chạy
    release.set()
    assert svc.wait_idle(timeout=5)
    svc.stop()
    assert order == ["first", "second"]    # xử lý lần lượt, đúng thứ tự


def test_error_does_not_stop_queue():
    order = []

    def proc(vid):
        order.append(vid)
        if vid == "bad":
            raise RuntimeError("boom")

    svc = EditQueueService(proc)
    svc.start(initial_ids=["bad", "good"])
    assert svc.wait_idle(timeout=5)
    svc.stop()
    assert order == ["bad", "good"]        # lỗi 1 video không chặn video kế tiếp
