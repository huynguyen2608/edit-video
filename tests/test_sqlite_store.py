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


def test_sqlite_export_excel(tmp_path):
    db = SQLiteStore(tmp_path / "data.db")
    db.add_discovered("v", "c", "channel", "title", "url", "now")
    out = tmp_path / "snapshot.xlsx"
    assert db.export_excel(out) == str(out)
    assert out.exists()


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
