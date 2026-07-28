from app.sqlite_store import SQLiteStore
from app.store import ExcelStore


def test_sqlite_persist_and_claim(tmp_path):
    path = tmp_path / "data.db"
    db = SQLiteStore(path)
    db.add_local_video("v", "local", "title", str(tmp_path / "v.mp4"))
    assert db.claim_edit("v") is True
    assert db.claim_edit("v") is False
    db.set_edit_status("v", "done")
    again = SQLiteStore(path)
    assert again.get_video("v").edit_status == "done"


def test_reset_for_redownload(tmp_path):
    db = SQLiteStore(tmp_path / "data.db")
    db.add_discovered("v", "c", "channel", "title", "url", "now")
    db.set_download_status("v", "downloaded", path=str(tmp_path / "v.mp4"))
    db.set_edit_status("v", "failed", error="thiếu file")
    assert db.reset_for_redownload("v") is True
    row = db.get_video("v")
    assert row.download_status == "pending"
    assert row.download_path == ""
    assert row.edit_status == "pending"
    raw = next(r for r in db.all_video_rows() if r["video_id"] == "v")
    assert raw.get("error") in (None, "")   # error xóa trong bản ghi gốc
    # bền vững sau khi mở lại
    again = SQLiteStore(tmp_path / "data.db")
    assert again.get_video("v").download_status == "pending"
    assert db.reset_for_redownload("khong-ton-tai") is False


def test_sqlite_export_excel(tmp_path):
    db = SQLiteStore(tmp_path / "data.db")
    db.add_discovered("v", "c", "channel", "title", "url", "now")
    out = tmp_path / "snapshot.xlsx"
    assert db.export_excel(out) == str(out)
    assert out.exists()


def test_sqlite_consistent_backup(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    db = SQLiteStore(source)
    db.add_discovered("v1", "c", "Channel", "Title", "url", "2026-01-01")
    assert db.backup_to(backup) == str(backup)
    restored = SQLiteStore(backup)
    assert restored.video_exists("v1")


def test_migrates_legacy_excel_with_history(tmp_path):
    excel = tmp_path / "data.xlsx"
    old = ExcelStore(excel)
    old.add_discovered("v", "c", "channel", "title", "url", "now")
    old.log_export("v", "channel", str(tmp_path), full_path="full.mp4")
    old.log_event("test", "legacy event")

    db = SQLiteStore(tmp_path / "data.db", legacy_excel=excel)
    assert db.get_video("v") is not None
    assert db.recent_exports()[0]["full_path"] == "full.mp4"
    assert any(e["message"] == "legacy event" for e in db.recent_events())
