"""Kho SQLite tương thích interface ExcelStore; Excel chỉ còn là import/export."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from openpyxl import Workbook

from .store import (CHANNEL_COLS, EVENT_COLS, EXPORT_COLS, VIDEO_COLS, ExcelStore,
                    CHANNELS_SHEET, EVENTS_SHEET,
                    EXPORTS_SHEET, VIDEOS_SHEET)


class SQLiteStore(ExcelStore):
    def __init__(self, path: str | Path, legacy_excel: str | Path | None = None):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._videos, self._channels, self._exports, self._events = {}, {}, [], []
        self._init_schema()
        self._load()
        if not self._videos and legacy_excel and Path(legacy_excel).exists():
            old = ExcelStore(legacy_excel)
            self._videos = {r["video_id"]: r for r in old.all_video_rows()}
            self._channels = dict(old._channels)
            self._exports = old.recent_exports()
            self._events = old.recent_events()
            self._save()

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=15000")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _init_schema(self):
        with self._connect() as con:
            for table, cols in (("videos", VIDEO_COLS), ("channels", CHANNEL_COLS),
                                ("exports", EXPORT_COLS), ("events", EVENT_COLS)):
                defs = ",".join(f'"{c}"' + (" TEXT PRIMARY KEY" if
                    (table, c) in (("videos", "video_id"), ("channels", "channel_id")) else "")
                    for c in cols)
                con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({defs})')

    def _load(self):
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            self._videos = {r["video_id"]: dict(r) for r in con.execute("SELECT * FROM videos")}
            self._channels = {r["channel_id"]: dict(r) for r in con.execute("SELECT * FROM channels")}
            self._exports = [dict(r) for r in con.execute("SELECT * FROM exports")]
            self._events = [dict(r) for r in con.execute("SELECT * FROM events")]

    def _save(self):
        """Snapshot nguyên tử trong SQLite; không phụ thuộc khóa file Excel."""
        with self._connect() as con:
            for table, cols, rows in (
                ("videos", VIDEO_COLS, self._videos.values()),
                ("channels", CHANNEL_COLS, self._channels.values()),
                ("exports", EXPORT_COLS, self._exports),
                ("events", EVENT_COLS, self._events),
            ):
                con.execute(f'DELETE FROM "{table}"')
                marks = ",".join("?" for _ in cols)
                names = ",".join(f'"{c}"' for c in cols)
                con.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
                                ([row.get(c) for c in cols] for row in rows))

    def export_excel(self, path: str | Path) -> str:
        wb = Workbook()
        for index, (name, cols, rows) in enumerate((
            (VIDEOS_SHEET, VIDEO_COLS, self._videos.values()),
            (CHANNELS_SHEET, CHANNEL_COLS, self._channels.values()),
            (EXPORTS_SHEET, EXPORT_COLS, self._exports),
            (EVENTS_SHEET, EVENT_COLS, self._events),
        )):
            ws = wb.active if index == 0 else wb.create_sheet()
            ws.title = name
            ws.append(cols)
            for row in rows:
                ws.append([row.get(c) for c in cols])
        wb.save(path)
        wb.close()
        return str(path)
