"""Test QueueManager: jobs view, dashboard, import dedup, runnable, resume, persistence."""
from app.config import AppConfig
from app.store import ExcelStore
from app.queue_manager import QueueManager, display_status
from app.editor.stages import Status
from app.downloader.downloader import DownloadResult
from app.downloader.scheduler import ScanService


def _mk(p):
    p.write_bytes(b"\x00\x00")


def _qm(tmp_path):
    cfg = AppConfig()
    cfg.editor.output_dir = str(tmp_path / "out")
    db = ExcelStore(tmp_path / "data.xlsx")
    return QueueManager(cfg, db), db


def test_import_and_jobs(tmp_path):
    inp = tmp_path / "in"; inp.mkdir()
    _mk(inp / "a.mp4"); _mk(inp / "b.mp4")
    qm, db = _qm(tmp_path)
    res = qm.import_paths([str(inp)])
    assert res["added"] == 2 and len(res["eligible"]) == 2
    jobs = qm.jobs()
    assert len(jobs) == 2
    j = jobs[0]
    assert j.status == Status.WAITING and j.source_path.endswith(".mp4")
    assert j.output_dir  # có đường dẫn output


def test_dashboard_counts(tmp_path):
    inp = tmp_path / "in"; inp.mkdir()
    for n in ("a.mp4", "b.mp4", "c.mp4"):
        _mk(inp / n)
    qm, db = _qm(tmp_path)
    ids = qm.import_paths([str(inp)])["eligible"]
    db.set_edit_status(ids[0], "done")                      # 1 completed
    db.update_job(ids[1], job_status=Status.PROCESSING, progress=40)  # 1 processing
    d = qm.dashboard()
    assert d["total"] == 3 and d["completed"] == 1 and d["processing"] == 1 and d["waiting"] == 1
    assert 0 <= d["overall"] <= 100


def test_runnable_excludes_completed(tmp_path):
    inp = tmp_path / "in"; inp.mkdir()
    _mk(inp / "a.mp4"); _mk(inp / "b.mp4")
    qm, db = _qm(tmp_path)
    ids = qm.import_paths([str(inp)])["eligible"]
    db.set_edit_status(ids[0], "done")
    run = qm.runnable_ids()
    assert ids[0] not in run and ids[1] in run


def test_import_skips_completed_dedup(tmp_path):
    inp = tmp_path / "in"; inp.mkdir()
    _mk(inp / "a.mp4")
    qm, db = _qm(tmp_path)
    ids = qm.import_paths([str(inp)])["eligible"]
    db.set_edit_status(ids[0], "done")
    res2 = qm.import_paths([str(inp)])                       # lần 2
    assert res2["added"] == 0 and res2["eligible"] == [] and res2["skipped_done"] == 1


def test_resume_interrupts_active(tmp_path):
    inp = tmp_path / "in"; inp.mkdir()
    _mk(inp / "a.mp4")
    qm, db = _qm(tmp_path)
    vid = qm.import_paths([str(inp)])["eligible"][0]
    db.update_job(vid, job_status=Status.RENDERING)
    db.set_edit_status(vid, "processing")
    # mở lại app
    db2 = ExcelStore(tmp_path / "data.xlsx")
    db2.resume_queue()
    row = [r for r in db2.all_video_rows() if r["video_id"] == vid][0]
    assert row["job_status"] == Status.INTERRUPTED       # đang render -> Interrupted
    assert row["edit_status"] == "pending"               # tiếp tục xử lý được
    assert display_status(row) == Status.INTERRUPTED


def test_retry_failed(tmp_path):
    inp = tmp_path / "in"; inp.mkdir()
    _mk(inp / "a.mp4")
    qm, db = _qm(tmp_path)
    vid = qm.import_paths([str(inp)])["eligible"][0]
    db.set_edit_status(vid, "failed", error="boom")
    assert vid not in qm.runnable_ids() or display_status(
        [r for r in db.all_video_rows() if r["video_id"] == vid][0]) == Status.FAILED
    got = qm.retry_failed()
    assert vid in got
    assert vid in qm.runnable_ids()


def test_cancelled_batch_download_becomes_paused(tmp_path):
    cfg = AppConfig()
    cfg.download.root_dir = str(tmp_path / "downloads")
    db = ExcelStore(tmp_path / "data.xlsx")
    db.add_discovered("cancel-me", "UC", "C", "T", "url", "p")
    service = ScanService(cfg, db)

    class CancelledDownloader:
        root_dir = ""
        fmt = ""
        cookies_from_browser = ""
        cookies_file = ""

        def download(self, video_id, channel_name, url, **_kwargs):
            return DownloadResult(video_id, False, error="stopped", cancelled=True)

    service._downloader = CancelledDownloader()
    stats = {"downloaded": 0, "failed": 0, "cancelled": 0}
    service._download_pending(stats)
    assert stats["cancelled"] == 1
    assert db.get_video("cancel-me").download_status == "paused"


def test_clear_and_rebuild_queue_keeps_video_record(tmp_path):
    inp = tmp_path / "in"; inp.mkdir()
    _mk(inp / "keep.mp4")
    qm, db = _qm(tmp_path)
    video_id = qm.import_paths([str(inp)])["eligible"][0]
    assert qm.remove_from_queue([video_id]) == 1
    assert qm.jobs() == []
    assert db.get_video(video_id) is not None
    assert video_id not in qm.runnable_ids()
    assert qm.rebuild_queue() == 1
    assert [job.video_id for job in qm.jobs()] == [video_id]
    assert video_id in qm.runnable_ids()
