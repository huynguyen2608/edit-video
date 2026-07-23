"""Test nạp video từ folder local: lọc đuôi, id ổn định, dedup + tính đủ điều kiện edit."""
from app.store import ExcelStore
from app.editor import local_source as ls


def _mk(p):
    p.write_bytes(b"\x00\x00")


def test_list_videos_filters_extensions(tmp_path):
    _mk(tmp_path / "a.mp4")
    _mk(tmp_path / "b.txt")
    _mk(tmp_path / "c.MKV")               # phân biệt hoa/thường
    (tmp_path / "sub").mkdir()
    _mk(tmp_path / "sub" / "d.mp4")       # ĐỆ QUY -> tìm thấy trong thư mục con
    _mk(tmp_path / "x_full.mp4")          # file OUTPUT do app tạo -> bỏ qua
    _mk(tmp_path / "sub" / "y_short.mp4")
    names = sorted(f.name for f in ls.list_videos(tmp_path))
    assert names == ["a.mp4", "c.MKV", "d.mp4"]
    # có thể tắt đệ quy
    top = sorted(f.name for f in ls.list_videos(tmp_path, recursive=False))
    assert top == ["a.mp4", "c.MKV"]


def test_video_id_stable_and_prefixed(tmp_path):
    f = tmp_path / "clip một.mp4"
    _mk(f)
    assert ls.video_id_for(f) == ls.video_id_for(f)   # ổn định qua nhiều lần
    assert ls.video_id_for(f).startswith("local_")


def test_ingest_dedup_eligibility_and_retry(tmp_path):
    vids = tmp_path / "vids"
    vids.mkdir()
    _mk(vids / "one.mp4")
    _mk(vids / "two.mp4")
    store = ExcelStore(tmp_path / "data.xlsx")

    r1 = ls.ingest_folder(store, vids)
    assert r1["added"] == 2 and len(r1["eligible"]) == 2
    assert r1["channel"] == "vids"

    # lần 2: không thêm mới nhưng vẫn cần edit (chưa edit)
    r2 = ls.ingest_folder(store, vids)
    assert r2["added"] == 0 and len(r2["eligible"]) == 2

    # 1 video 'done' -> lần sau bỏ qua
    store.set_edit_status(r1["eligible"][0], "done")
    r3 = ls.ingest_folder(store, vids)
    assert len(r3["eligible"]) == 1 and r3["skipped_done"] == 1

    # video 'failed' -> reset về pending để edit lại
    store.set_edit_status(r1["eligible"][1], "failed")
    r4 = ls.ingest_folder(store, vids)
    assert r1["eligible"][1] in r4["eligible"]


def test_local_video_marked_downloaded(tmp_path):
    vids = tmp_path / "in"
    vids.mkdir()
    _mk(vids / "x.mp4")
    store = ExcelStore(tmp_path / "data.xlsx")
    ls.ingest_folder(store, vids)
    row = store.pending_edits()
    assert len(row) == 1 and row[0].download_status == "downloaded"
    assert row[0].download_path.endswith("x.mp4")
