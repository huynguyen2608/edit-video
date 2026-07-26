"""Test đối chiếu file (reconcile) — dùng fake store nên KHÔNG cần openpyxl."""
from app.downloader.reconcile import reconcile_library, is_local_video


class FakeStore:
    """Kho giả tối thiểu: đủ interface cho reconcile_library."""
    def __init__(self, rows):
        self._rows = {r["video_id"]: dict(r) for r in rows}
        self.reset_calls = []
        self.removed = []

    def all_video_rows(self):
        return [dict(r) for r in self._rows.values()]

    def remove_videos(self, ids):
        ids = list(ids)
        for i in ids:
            self._rows.pop(i, None)
        self.removed.extend(ids)
        return len(ids)

    def reset_for_redownload(self, vid):
        self.reset_calls.append(vid)
        row = self._rows.get(vid)
        if not row:
            return False
        row.update(download_status="pending", download_path="", edit_status="pending")
        return True


def _row(vid, status="downloaded", path="x.mp4", channel_id="ch", edit="failed"):
    return {"video_id": vid, "channel_id": channel_id, "download_status": status,
            "download_path": path, "edit_status": edit}


def test_keeps_video_when_file_exists():
    store = FakeStore([_row("a", path="a.mp4")])
    res = reconcile_library(store, exists=lambda p: True)
    assert res == {"reset": [], "removed": []}
    assert store.reset_calls == [] and store.removed == []


def test_resets_youtube_video_when_file_missing():
    store = FakeStore([_row("a", path="a.mp4")])
    res = reconcile_library(store, exists=lambda p: False)
    assert res["reset"] == ["a"] and res["removed"] == []
    assert store._rows["a"]["download_status"] == "pending"
    assert store._rows["a"]["edit_status"] == "pending"


def test_removes_local_video_when_file_missing():
    store = FakeStore([_row("local_x", channel_id="local", path="x.mp4")])
    res = reconcile_library(store, exists=lambda p: False)
    assert res["removed"] == ["local_x"] and res["reset"] == []
    assert "local_x" not in store._rows


def test_removes_local_by_id_prefix():
    store = FakeStore([_row("local_abc", channel_id="ch", path="x.mp4")])
    res = reconcile_library(store, exists=lambda p: False)
    assert res["removed"] == ["local_abc"]


def test_ignores_non_downloaded_rows():
    store = FakeStore([_row("a", status="pending", path=""),
                       _row("b", status="failed", path="")])
    res = reconcile_library(store, exists=lambda p: False)
    assert res == {"reset": [], "removed": []}


def test_empty_path_treated_as_missing():
    store = FakeStore([_row("a", path="")])
    # exists trả True nhưng path rỗng -> vẫn coi là mất file, reset.
    res = reconcile_library(store, exists=lambda p: True)
    assert res["reset"] == ["a"]


def test_mixed_batch():
    store = FakeStore([
        _row("keep", path="keep.mp4"),
        _row("gone", path="gone.mp4"),
        _row("local_gone", channel_id="local", path="lg.mp4"),
    ])
    present = {"keep.mp4"}
    res = reconcile_library(store, exists=lambda p: p in present)
    assert res["reset"] == ["gone"]
    assert res["removed"] == ["local_gone"]
    assert "keep" in store._rows and "gone" in store._rows  # reset giữ dòng
    assert "local_gone" not in store._rows


def test_is_local_video():
    assert is_local_video({"video_id": "local_x"})
    assert is_local_video({"video_id": "y", "channel_id": "local"})
    assert not is_local_video({"video_id": "y", "channel_id": "ch"})
