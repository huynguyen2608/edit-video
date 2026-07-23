"""Test kho Excel: dedup + chuyển trạng thái tải/edit + reload từ file."""
from app.store import ExcelStore


def test_dedup_and_pending(tmp_path):
    db = ExcelStore(tmp_path / "data.xlsx")
    # lần đầu = mới
    assert db.add_discovered("vid1", "UC1", "Chan", "Title", "url", "2026-01-01") is True
    # lần hai cùng id = không mới (dedup)
    assert db.add_discovered("vid1", "UC1", "Chan", "Title", "url", "2026-01-01") is False

    pend = db.pending_downloads()
    assert len(pend) == 1 and pend[0].video_id == "vid1"

    # tải xong -> ra khỏi pending_downloads, vào pending_edits
    db.set_download_status("vid1", "downloaded", path="D:/x/vid1.mp4")
    assert db.pending_downloads() == []
    edits = db.pending_edits()
    assert len(edits) == 1 and edits[0].download_path == "D:/x/vid1.mp4"

    # edit xong -> hết pending_edits
    db.set_edit_status("vid1", "done")
    assert db.pending_edits() == []


def test_claim_download_atomic(tmp_path):
    db = ExcelStore(tmp_path / "data.xlsx")
    db.add_discovered("v", "UC", "C", "T", "u", "p")
    assert db.claim_download("v") is True    # phiên đầu giành được
    assert db.claim_download("v") is False   # phiên sau: đã downloading


def test_claim_edit_atomic(tmp_path):
    import threading
    db = ExcelStore(tmp_path / "data.xlsx")
    db.add_local_video("v", "local", "one", str(tmp_path / "one.mp4"))
    results = []
    gate = threading.Barrier(3)

    def claim():
        gate.wait()
        results.append(db.claim_edit("v"))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_reset_stuck(tmp_path):
    db = ExcelStore(tmp_path / "data.xlsx")
    db.add_discovered("v", "UC", "C", "T", "u", "p")
    db.set_download_status("v", "downloading")
    db.set_download_status("v", "downloaded")
    db.set_edit_status("v", "processing")
    # giả lập crash: reset đưa *ing về pending
    db.reset_stuck()
    # download đã downloaded nên giữ nguyên; edit processing -> pending
    assert db.pending_edits()[0].video_id == "v"


def test_paused_download_resumes_to_pending(tmp_path):
    db = ExcelStore(tmp_path / "data.xlsx")
    db.add_discovered("paused", "UC", "C", "T", "url", "p")
    db.set_download_status("paused", "paused")
    assert db.pending_downloads() == []
    assert db.resume_paused_downloads(["paused"]) == 1
    assert [row.video_id for row in db.pending_downloads()] == ["paused"]


def test_crashed_download_becomes_paused(tmp_path):
    db = ExcelStore(tmp_path / "data.xlsx")
    db.add_discovered("active", "UC", "C", "T", "url", "p")
    db.set_download_status("active", "downloading")
    db.reset_stuck()
    assert db.get_video("active").download_status == "paused"


def test_download_transition_does_not_overwrite_completed(tmp_path):
    db = ExcelStore(tmp_path / "data.xlsx")
    db.add_discovered("race", "UC", "C", "T", "url", "p")
    db.set_download_status("race", "downloaded", path="video.mp4")
    assert db.transition_download_status("race", "downloading", "cancelling") is False
    assert db.get_video("race").download_status == "downloaded"


def test_persist_and_reload(tmp_path):
    """Ghi -> mở lại từ FILE Excel phải khôi phục đúng trạng thái (nhiều sheet)."""
    path = tmp_path / "data.xlsx"
    db = ExcelStore(path)
    db.upsert_channel("UC9", "Kênh 9", "https://x")
    db.add_discovered("vA", "UC9", "Kênh 9", "Tiêu đề", "url", "2026-02-02")
    db.set_download_status("vA", "downloaded", path="D:/v/vA.mp4")

    db2 = ExcelStore(path)  # đọc lại từ đĩa
    rows = db2.all_videos()
    assert len(rows) == 1 and rows[0].video_id == "vA"
    assert rows[0].download_status == "downloaded"
    assert db2.pending_edits()[0].download_path == "D:/v/vA.mp4"


def test_get_video(tmp_path):
    db = ExcelStore(tmp_path / "data.xlsx")
    assert db.get_video("nope") is None
    db.add_discovered("vX", "UC", "C", "T", "u", "p")
    row = db.get_video("vX")
    assert row is not None and row.video_id == "vX" and row.edit_status == "pending"


def test_export_and_event_log_persist(tmp_path):
    path = tmp_path / "data.xlsx"
    db = ExcelStore(path)
    db.add_discovered("vE", "UC", "Kênh", "T", "u", "p")
    db.log_event("download", "tải xong vE")
    db.log_export("vE", "Kênh", "D:/out/Kênh/vE",
                  full_path="D:/out/Kênh/vE/vE_full.mp4",
                  short_path="D:/out/Kênh/vE/vE_short.mp4",
                  content_txt="D:/out/Kênh/vE/vE_content.txt")
    # log_export tự thêm 1 event -> tổng 2 event
    assert len(db.recent_events()) == 2
    exp = db.recent_exports()
    assert len(exp) == 1 and exp[0]["video_id"] == "vE"
    assert exp[0]["full_path"].endswith("vE_full.mp4")

    # đọc lại từ đĩa: exports + events còn nguyên
    db2 = ExcelStore(path)
    assert len(db2.recent_exports()) == 1
    assert len(db2.recent_events()) == 2


def test_save_survives_locked_file(tmp_path):
    """Nếu data.xlsx đang bị khóa (mở trong Excel) -> ghi thất bại KHÔNG làm crash;
    trạng thái vẫn trong RAM và persist ở lần lưu sau khi mở khóa."""
    import app.store as sm
    path = tmp_path / "data.xlsx"
    db = sm.ExcelStore(path)
    orig = sm.os.replace
    calls = {"n": 0}

    def boom(a, b):
        calls["n"] += 1
        raise PermissionError("locked")

    sm.os.replace = boom
    try:
        db.add_discovered("v", "UC", "C", "T", "u", "p")   # _save fail nhưng KHÔNG raise
    finally:
        sm.os.replace = orig
    assert calls["n"] >= 1
    assert db.get_video("v") is not None                    # state còn trong RAM

    db.set_edit_status("v", "done")                         # đã mở khóa -> lưu được
    db2 = sm.ExcelStore(path)
    assert db2.get_video("v") is not None                   # đã persist


def test_clear_history_keeps_videos(tmp_path):
    path = tmp_path / "data.xlsx"
    db = ExcelStore(path)
    db.add_discovered("vK", "UC", "C", "T", "u", "p")
    db.log_event("edit", "xong vK")
    db.log_export("vK", "C", "D:/out/C/vK", full_path="D:/out/C/vK/vK_full.mp4")
    assert db.recent_events() and db.recent_exports()

    db.clear_history()
    assert db.recent_exports() == [] and db.recent_events() == []
    # video KHÔNG bị xoá
    assert db.get_video("vK") is not None
    # persist sau khi xoá
    db2 = ExcelStore(path)
    assert db2.recent_exports() == [] and db2.recent_events() == []
    assert db2.get_video("vK") is not None
