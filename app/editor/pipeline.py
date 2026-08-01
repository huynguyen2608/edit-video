"""Điều phối biên tập MỘT video.

Thứ tự: probe -> focus -> [CHUẨN BỊ: tách giọng + tạo content từ lời thoại + lồng
âm thanh/cài đặt chung] -> render full/short -> ghi log xuất bản -> cập nhật trạng thái.

Chỉ nhận video download_status='downloaded' & edit_status='pending' (chỉ sửa video mới,
bỏ qua đã sửa). Phần lồng âm thanh & cấu hình chung được xử lý ở GIAI ĐOẠN CHUẨN BỊ
(trước khi edit video). Tách giọng dùng lại khi ghép: giữ NGUYÊN nội dung hội thoại
(vocals ở âm lượng gốc), và content .txt được transcribe TỪ giọng đã tách để bám đúng
ngữ cảnh câu từ.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import shutil
import tempfile
from typing import Callable, Optional

from ..config import AppConfig
from ..store import ExcelStore, VideoRow
from ..logging_setup import get_logger
from ..paths import ensure_dir, safe_name, output_video_path
from . import (analyze, audio_ops, edge_tts_service, export, fingerprint, smart_crop, subtitles,
               transcribe, translate, video_ops, work_cache)
from .export import RenderInputs
from .stages import Stage
from .cancel import EditCancelled, run_cancellable

log = get_logger("pipeline")
LogCb = Callable[[str], None]
StageCb = Callable[[str, str], None]        # (video_id, stage)
RenderProgressCb = Callable[[str, float], None]  # (video_id, fraction 0..1)


@dataclass
class EditOutputs:
    full: Optional[str] = None
    short: Optional[str] = None
    content_txt: Optional[str] = None
    srt: Optional[str] = None


class EditPipeline:
    def __init__(self, cfg: AppConfig, db: ExcelStore, on_log: Optional[LogCb] = None,
                 device: str = "auto", on_stage: Optional[StageCb] = None,
                 on_render_progress: Optional[RenderProgressCb] = None,
                 cancel_cb=None):
        self.cfg = cfg
        self.db = db
        self.on_log = on_log or (lambda m: None)
        self.device = device
        # Quan sát tiến trình (không đổi logic xử lý): báo công đoạn + % render THẬT.
        self.on_stage = on_stage or (lambda vid, stage: None)
        self.on_render_progress = on_render_progress or (lambda vid, frac: None)
        self.cancel_cb = cancel_cb
        self._segment_mode = False

    def _check_cancel(self) -> None:
        if self.cancel_cb and self.cancel_cb():
            raise EditCancelled("Đã dừng video hiện tại")

    def _stage(self, vid: str, stage: str) -> None:
        self._check_cancel()
        self.on_stage(vid, stage)

    # ---------------- điều phối danh sách ----------------
    def process_pending(self) -> list[EditOutputs]:
        """Edit lần lượt toàn bộ backlog (video downloaded & chưa edit)."""
        pending = self.db.pending_edits()
        self._log(f"Có {len(pending)} video chờ edit.")
        return [r for r in (self._process_row_safe(row) for row in pending) if r]

    def process_one_by_id(self, video_id: str) -> Optional[EditOutputs]:
        """Edit đúng 1 video theo id (dùng cho hàng đợi khi tải xong báo sang)."""
        row = self.db.get_video(video_id)
        if not row:
            self._log(f"Không thấy video {video_id} trong kho.")
            return None
        if row.download_status != "downloaded" or row.edit_status != "pending":
            self._log(f"Bỏ qua {video_id}: {row.download_status}/{row.edit_status} "
                      f"(không ở trạng thái cần edit).")
            return None
        return self._process_row_safe(row)

    def _process_row_safe(self, row: VideoRow) -> Optional[EditOutputs]:
        if not self.db.claim_edit(row.video_id):
            self._log(f"Bỏ qua {row.video_id}: job đã được worker khác nhận.")
            return None
        try:
            return self.process_one(row)
        except EditCancelled:
            self.db.set_edit_status(row.video_id, "pending", error=None)
            self.db.log_event("edit", f"Đã dừng edit: {row.title or row.video_id}")
            self._log(f"  ‖ đã dừng video {row.video_id}; có thể chạy lại từ hàng đợi.")
            raise
        except Exception as e:
            self.db.set_edit_status(row.video_id, "failed", error=str(e))
            self.db.log_event(
                "edit", f"Edit lỗi: {row.title or row.video_id} — {e}", level="ERROR")
            self._log(f"  ! edit lỗi {row.video_id}: {e}")
            log.exception("edit fail %s", row.video_id)
            return None

    # ---------------- edit 1 video ----------------
    def process_one(self, row: VideoRow) -> EditOutputs:
        if not row.download_path or not Path(row.download_path).exists():
            raise FileNotFoundError(f"Không thấy file tải: {row.download_path}")

        self._log(f"Đang edit: {row.title} ({row.video_id})")
        ecfg = self.cfg.editor
        if ecfg.fingerprint_enabled:      # biến đổi nhẹ MỖI video để chống trùng
            ecfg = fingerprint.apply(ecfg, row.video_id)
            self._log(f"  ↪ fingerprint (speed={ecfg.speed:.3f}, flip={ecfg.flip_horizontal})")

        # Folder riêng theo KÊNH / <video_id> để dễ kiểm soát.
        out_base = ensure_dir(Path(ecfg.output_dir) / safe_name(row.channel_name) / row.video_id)
        src = row.download_path

        self._stage(row.video_id, Stage.READING)
        dims = smart_crop.probe_dimensions(src)
        self._check_cancel()
        segment_minutes = int(getattr(ecfg, "long_video_segment_minutes", 0) or 0)
        if (not self._segment_mode and segment_minutes in (4, 5)
                and float(dims.duration) > 600.0):
            return self._process_split_video(
                ecfg, row, src, out_base, segment_minutes)
        # Chỉ ghi sau khi đã xác định đây là một file đầu ra độc lập. Video dài
        # được chia sẽ để từng part_row tự ghi cặp bắt đầu/kết thúc riêng.
        shown_name = row.title.strip() or row.video_id
        self.db.log_event("edit", f"Bắt đầu edit: {shown_name}")
        if ecfg.fill_missing == "none" and ecfg.crop_mode == "auto":   # smart-crop thực chạy
            self._stage(row.video_id, Stage.SMART_CROP)
        fx, fy = self._resolve_focus(ecfg, src)
        self._check_cancel()

        # ===== GIAI ĐOẠN CHUẨN BỊ (làm TRƯỚC khi edit video) =====
        # Lồng/định hình âm thanh + tạo nội dung được thực hiện ở đây, không xen vào
        # lúc render, nên có thể cấu hình chung một lần cho cả list.
        vocals, content_txt, srt_path, hook_sug, generated_voiceover = \
            self._prepare_audio_and_content(ecfg, row, src, out_base)
        synced_masks = [
            mask for mask in (getattr(ecfg, "mask_regions", []) or [])
            if getattr(mask, "visible", True)
            and getattr(mask, "timing_mode", "full") == "subtitle"]
        if synced_masks and not srt_path:
            self._log(
                "  ! Vùng che phụ đề cũ đang chọn 'Khi có phụ đề mới' nhưng "
                "không có SRT/ASS; vùng này sẽ không che xuyên suốt video.")
        elif synced_masks:
            try:
                cue_count = len(subtitles.read_subtitle(srt_path))
            except OSError:
                cue_count = 0
            self._log(f"  ✔ vùng che phụ đề cũ đồng bộ theo {cue_count} câu phụ đề")
        artifact_id = safe_name(
            Path(src).stem if self._segment_mode else row.video_id, max_len=120)
        overlay_ass = self._build_overlay_ass(
            ecfg, row, out_base, dims.duration, hook_sug, artifact_id=artifact_id)

        ri = RenderInputs(video=src, src_w=dims.width, src_h=dims.height,
                          src_fps=dims.fps,
                          focus_x=fx, focus_y=fy, vocals_wav=vocals,
                          voiceover_path=generated_voiceover,
                          has_audio=smart_crop.has_audio(src), subtitle_path=srt_path,
                          audio_codec=smart_crop.audio_codec(src),
                          audio_channels=smart_crop.audio_channels(src),
                          overlay_ass_path=overlay_ass)

        # ===== GIAI ĐOẠN EDIT VIDEO + EXPORT =====
        outs = EditOutputs(content_txt=content_txt, srt=srt_path)
        full_path = str(output_video_path(out_base, src, row.video_id, "full"))
        short_path = str(output_video_path(out_base, src, row.video_id, "short"))
        self._stage(row.video_id, Stage.RENDERING)
        out_dur = float(dims.duration) / max(1e-6, float(ecfg.speed))
        if ecfg.export.make_full:
            outs.full = export.render(
                ecfg, ri, full_path,
                progress_cb=lambda f: self.on_render_progress(row.video_id, f),
                duration_hint=out_dur, cancel_cb=self.cancel_cb)
            self._log(f"  ✔ full: {outs.full}")
        if ecfg.export.make_short:
            # Render trực tiếp từ nguồn: short chỉ trải qua MỘT lần encode, không lấy
            # từ bản full đã nén như trước.
            start = 0.0
            if ecfg.export.short_mode == "highlight":
                start = analyze.pick_highlight_start(src, ecfg.export.short_seconds)
                self._log(f"  ↪ short lấy đoạn sôi động tại {start:.1f}s từ nguồn")
            outs.short = export.render(
                ecfg, ri, short_path, duration=ecfg.export.short_seconds, start=start,
                duration_hint=ecfg.export.short_seconds, cancel_cb=self.cancel_cb)
            self._log(f"  ✔ short {ecfg.export.short_seconds}s (encode 1 lần từ nguồn): {outs.short}")
        else:
            self._log("  ↪ không xuất video ngắn: bỏ lần mã hóa thứ hai để hoàn thành nhanh hơn.")

        # Phụ đề, transcript, audio tách, Edge-TTS và ASS chỉ là dữ liệu trung gian
        # phục vụ hai bản render. Thư mục kết quả cuối chỉ giữ video full + short.
        if not self._segment_mode:
            self._cleanup_output_artifacts(out_base, (outs.full, outs.short))
            outs.content_txt = None
            outs.srt = None

        # Ghi LOG XUẤT BẢN (sheet 'exports') + đánh dấu done.
        self._stage(row.video_id, Stage.SAVING)
        self.db.log_export(row.video_id, row.channel_name, str(out_base),
                           full_path=outs.full, short_path=outs.short,
                           content_txt=outs.content_txt, srt_path=outs.srt,
                           video_title=row.title)
        if not self._segment_mode:
            self.db.set_edit_status(row.video_id, "done")
            self._stage(row.video_id, Stage.COMPLETED)
        n_files = sum(1 for f in (outs.full, outs.short, outs.content_txt, outs.srt) if f)
        self.db.log_event("edit", f"Kết thúc edit: {shown_name}")
        self._log(f"  ✔ xong; đã lưu {n_files} file vào {out_base} và ghi log xuất bản.")
        return outs

    # ---------------- các bước con ----------------
    def _process_split_video(self, ecfg, row: VideoRow, src: str, out_base: Path,
                             segment_minutes: int) -> EditOutputs:
        """Split a long source losslessly and process one bounded part at a time."""
        temp_dir = Path(tempfile.mkdtemp(prefix=".vrs_segments_", dir=str(out_base)))
        source_stem = safe_name(Path(src).stem, max_len=100)
        suffix = Path(src).suffix or ".mp4"
        pattern = str(temp_dir / f"{source_stem} - phần %d{suffix}")
        cmd = [
            "ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?",
            "-c", "copy",
            "-f", "segment", "-segment_time", str(segment_minutes * 60),
            "-segment_start_number", "1", "-reset_timestamps", "1", pattern,
        ]
        self._log(
            f"  ↪ video dài hơn 10 phút: chia thành các phần khoảng {segment_minutes} phút…")
        try:
            result = run_cancellable(
                cmd, cancel_cb=self.cancel_cb, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(
                    "Không thể chia video dài: " + (result.stderr or "")[-800:])
            parts = sorted(
                temp_dir.glob(f"{source_stem} - phần *"),
                key=lambda p: int(p.stem.rsplit("phần ", 1)[-1]))
            if not parts:
                raise RuntimeError("FFmpeg không tạo được phần video nào.")
            source_subtitle, source_lang = subtitles.find_source_subtitle(src)
            source_cues = (
                subtitles.normalize_cues(
                    subtitles.read_subtitle(source_subtitle), float(duration))
                if source_subtitle else [])
            timeline = 0.0
            if source_cues:
                self._log(
                    f"  ↪ chia phụ đề nguồn {source_subtitle.name} theo từng phần; "
                    "không chạy lại Whisper.")
                for part in parts:
                    part_duration = float(
                        smart_crop.probe_dimensions(str(part)).duration or 0.0)
                    part_cues = subtitles.slice_cues(
                        source_cues, timeline, timeline + part_duration)
                    if part_cues:
                        suffix = f".{source_lang}" if source_lang else ""
                        part_srt = part.with_name(part.stem + suffix + ".srt")
                        part_srt.write_text(
                            subtitles.to_srt(part_cues), encoding="utf-8")
                    timeline += part_duration
            last = EditOutputs()
            self._segment_mode = True
            try:
                for index, part in enumerate(parts, 1):
                    self._check_cancel()
                    self._log(f"  ▶ xử lý phần {index}/{len(parts)}: {part.name}")
                    part_row = replace(
                        row, download_path=str(part),
                        title=f"{row.title} - phần {index}")
                    last = self.process_one(part_row)
            finally:
                self._segment_mode = False
            # Mỗi phần dài có một cặp full/short; xóa toàn bộ sidecar và thư mục
            # tạm của tách giọng/TTS sau khi tất cả phần đã render thành công.
            # Folder tạm chia đoạn nằm riêng; mọi MP4 trực tiếp trong out_base
            # đều là bản chỉnh sửa chính hoặc short của từng phần.
            keep = [str(path) for path in out_base.rglob("*.mp4")]
            self._cleanup_output_artifacts(out_base, keep)
            self.db.set_edit_status(row.video_id, "done")
            self._stage(row.video_id, Stage.COMPLETED)
            self._log(
                f"  ✔ hoàn thành {len(parts)} phần; các file nằm chung trong {out_base}")
            return last
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _cleanup_output_artifacts(self, out_base: Path, keep_paths) -> None:
        """Chỉ giữ các video đầu ra đã yêu cầu trong folder riêng của một job."""
        base = Path(out_base)
        keep = {
            str(Path(path).resolve()).lower()
            for path in keep_paths
            if path and Path(path).exists()
        }
        removed = 0
        for path in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if path.is_file():
                    if str(path.resolve()).lower() not in keep:
                        path.unlink(missing_ok=True)
                        removed += 1
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                # File đang được thư viện ngoài nhả chậm: không làm hỏng kết quả
                # render; lần xử lý sau sẽ dọn lại folder job này.
                continue
        if removed:
            self._log(f"  ↪ đã dọn {removed} file trung gian; chỉ giữ video full và short.")

    def _build_overlay_ass(self, ecfg, row: VideoRow, out_base, src_duration: float,
                           hook_suggestion: str = "", artifact_id: str = ""):
        """Dựng file .ass cho hook (giây đầu) + CTA (giây cuối). None nếu không bật.

        hook_suggestion: dùng khi hook bật 'auto' và chưa nhập text (lấy câu mở đầu).
        """
        hook, cta = ecfg.intro_hook, ecfg.outro_cta
        hook_text = hook.text.strip() or (hook_suggestion if hook.auto else "")
        want = (hook.enabled and hook_text) or (cta.enabled and cta.text.strip())
        if not want:
            return None
        out_dur = float(src_duration) / max(1e-6, float(ecfg.speed))  # thời lượng SAU speed
        items = []
        if hook.enabled and hook_text:
            items.append({"start": 0.0, "end": min(float(hook.seconds), out_dur),
                          "text": hook_text, "position": hook.position,
                          "font_size": hook.font_size, "box": hook.box,
                          "style_preset": hook.style_preset,
                          "safe_margin_percent": hook.safe_margin_percent,
                          "fade_ms": hook.fade_ms})
        if cta.enabled and cta.text.strip():
            items.append({"start": max(0.0, out_dur - float(cta.seconds)), "end": out_dur,
                          "text": cta.text, "position": cta.position,
                          "font_size": cta.font_size, "box": cta.box,
                          "style_preset": cta.style_preset,
                          "safe_margin_percent": cta.safe_margin_percent,
                          "fade_ms": cta.fade_ms})
        ow, oh = video_ops.target_resolution(ecfg.target_aspect)
        path = str(out_base / f"{artifact_id or row.video_id}_overlay.ass")
        Path(path).write_text(subtitles.build_overlay_ass(items, ow, oh), encoding="utf-8")
        self._log(f"  ✔ hook/CTA overlay: {path}")
        return path

    def _resolve_focus(self, ecfg, src: str) -> tuple[float, float]:
        # Focus CHỈ dùng ở crop-to-fill (fill_missing="none"); blur/pad canh giữa.
        if ecfg.fill_missing != "none":
            return 0.5, 0.5
        if ecfg.crop_mode == "auto":
            signature = work_cache.source_signature(src, "smart-crop-v1")
            cache = work_cache.artifact_dir(
                ecfg.output_dir, "focus", Path(src).stem, signature) / "focus.json"
            saved = work_cache.load_json(cache, signature)
            if saved:
                self._log("  ↪ dùng lại tâm crop đã phân tích.")
                return float(saved["x"]), float(saved["y"])
            fx, fy = smart_crop.detect_focus(src)
            work_cache.save_json(cache, signature, x=fx, y=fy)
            return fx, fy
        if ecfg.crop_mode == "manual":
            self._log(f"  vùng crop thủ công: focus=({ecfg.manual_focus_x:.2f}, "
                      f"{ecfg.manual_focus_y:.2f})")
            return ecfg.manual_focus_x, ecfg.manual_focus_y
        return 0.5, 0.5  # center

    def _prepare_audio_and_content(self, ecfg, row: VideoRow, src: str, out_base):
        """Chuẩn bị TRƯỚC khi render: tách giọng + transcribe + gộp cue + dịch + .srt.

        Trả (vocals, content_txt, srt_path).
        - Tách giọng (Demucs) nếu bật -> nguồn audio giữ nguyên hội thoại.
        - Transcribe TỪ vocals (nếu có) để bám đúng lời thoại.
        - Gộp các cue ngắn/sát nhau trước khi dịch -> tránh dịch SAI lời thoại.
        - Phụ đề hiển thị/burn CHỈ ngôn ngữ đã chọn (dịch) hoặc ngôn ngữ gốc.
        """
        artifact_id = safe_name(
            Path(src).stem if self._segment_mode else row.video_id, max_len=120)
        vocals: Optional[str] = None
        if ecfg.audio.separate_speech:
            self._stage(row.video_id, Stage.AUDIO)
            be = ecfg.audio.separator_backend
            variant = f"{be}|{ecfg.audio.separator_model}|{self.device}"
            signature = work_cache.source_signature(src, variant)
            cache_dir = work_cache.artifact_dir(
                ecfg.output_dir, "audio", row.video_id, signature)
            manifest = work_cache.load_json(cache_dir / "manifest.json", signature)
            cached_vocals = (
                cache_dir / str(manifest.get("vocals", ""))
                if manifest else None)
            if cached_vocals and cached_vocals.is_file():
                vocals = str(cached_vocals)
                self._log(f"  ↪ dùng lại lời thoại đã tách ({be}).")
            else:
                self._log(f"  [chuẩn bị] tách giọng thoại ({be}) — giữ nguyên hội thoại…")
                cache_dir.mkdir(parents=True, exist_ok=True)
                wav = audio_ops.extract_audio(
                    src, str(cache_dir / f"{artifact_id}_audio.wav"),
                    cancel_cb=self.cancel_cb)
                vocals = audio_ops.separate_speech(
                    wav, str(cache_dir / f"{artifact_id}_sep"), device=self.device,
                    backend=be, model=ecfg.audio.separator_model, cancel_cb=self.cancel_cb)
                vocals_path = Path(vocals)
                try:
                    relative = str(vocals_path.relative_to(cache_dir))
                except ValueError:
                    stable = cache_dir / "vocals.wav"
                    stable.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(vocals_path, stable)
                    vocals = str(stable)
                    relative = stable.name
                work_cache.save_json(
                    cache_dir / "manifest.json", signature, vocals=relative)
            self._check_cancel()

        scfg = ecfg.subtitle
        hook = ecfg.intro_hook
        want_auto_hook = hook.enabled and hook.auto and not hook.text.strip()
        want_subtitle = scfg.enabled and scfg.burn_in
        need_segments = (ecfg.export.make_content_txt or want_subtitle
                         or ecfg.tts.enabled or want_auto_hook)
        content_txt: Optional[str] = None
        srt_path: Optional[str] = None
        hook_suggestion = ""
        if not need_segments:
            return vocals, content_txt, srt_path, hook_suggestion, None

        source_subtitle, subtitle_lang = subtitles.find_source_subtitle(src)
        cues = subtitles.read_subtitle(source_subtitle) if source_subtitle else []
        lang = subtitle_lang
        self._stage(row.video_id, Stage.SPEECH)
        if cues:
            self._log(f"  [chuẩn bị] dùng phụ đề nguồn đã tải: {source_subtitle.name}")
        else:
            if source_subtitle:
                self._log(f"  ! không đọc được {source_subtitle.name} — dùng nhận diện lời thoại.")
            else:
                self._log("  [chuẩn bị] không có phụ đề nguồn — nhận diện lời thoại…")
            cached = None
            cache_path = None
            if not self._segment_mode and Path(src).is_file():
                variant = (
                    f"speech={bool(ecfg.audio.separate_speech)}|"
                    f"backend={ecfg.audio.separator_backend}|"
                    f"model={ecfg.audio.separator_model}")
                signature = work_cache.source_signature(src, variant)
                cache_path = work_cache.transcript_path(
                    ecfg.output_dir, row.video_id)
                cached = work_cache.load_transcript(cache_path, signature)
            if cached:
                cues, lang = cached
                self._log("  ↪ dùng lại transcript đã nhận diện trước đó.")
            else:
                cues, lang = transcribe.transcribe_segments(vocals or src, device=self.device)
                if cache_path and cues:
                    work_cache.save_transcript(cache_path, signature, cues, lang)
                    self._log("  ↪ đã lưu transcript để dùng lại khi render lại.")
            self._check_cancel()
        cues = subtitles.normalize_cues(
            cues, float(getattr(row, "duration", 0.0) or 0.0))
        lang = subtitles.normalize_language(lang)
        if want_auto_hook:
            hook_suggestion = subtitles.pick_auto_hook(cues)

        target_lang = (scfg.translate_to if want_subtitle
                       else (ecfg.tts.language if ecfg.tts.enabled else ""))
        want_tr = bool(
            target_lang and not subtitles.languages_equivalent(target_lang, lang))
        if want_subtitle:
            # Giữ timestamp nguồn. Việc trình bày hai dòng được thực hiện lúc ghi SRT,
            # không phân lại thời gian theo độ dài ký tự.
            work = [subtitles.Cue(c.start, c.end, c.text, c.text2) for c in cues]
        elif ecfg.tts.enabled:
            work = subtitles.merge_short_cues(
                cues, scfg.merge_gap_ms, scfg.min_cue_ms, scfg.max_cue_ms)
        else:
            work = cues
        if want_tr:
            self._stage(row.video_id, Stage.TRANSLATION)
            self._log(f"  [chuẩn bị] dịch nội dung sang '{target_lang}'…")
            work, ok = translate.translate_cues(work, target_lang, source=lang,
                                                backend=scfg.translator)
            if not ok:
                want_tr = False
                self._log("  ! không có công cụ dịch — dùng phụ đề ngôn ngữ gốc.")

        if want_subtitle and work:
            self._stage(row.video_id, Stage.SUBTITLE)
            max_chars = 40 if ecfg.target_aspect in {"9:16", "1:1"} else 50
            if source_subtitle and not want_tr:
                # Luôn tạo bản làm việc để chuẩn hóa cue và ngắt dòng, không sửa sidecar gốc.
                working_srt = out_base / f"{artifact_id}_working.srt"
                working_srt.write_text(
                    subtitles.to_srt(
                        work, max_chars=max_chars, max_lines=2),
                    encoding="utf-8")
                srt_path = str(working_srt)
                self._log(
                    f"  ✔ dùng timestamp phụ đề nguồn, tối ưu trình bày: {source_subtitle.name}")
            else:
                # Dịch hoặc Whisper dự phòng cần SRT làm việc tạm cho FFmpeg.
                srt_path = str(out_base / f"{artifact_id}.srt")
                Path(srt_path).write_text(
                    subtitles.to_srt(
                        work, use_translation=want_tr,
                        max_chars=max_chars, max_lines=2),
                    encoding="utf-8")
                origin = "bản dịch" if want_tr else "Whisper dự phòng"
                self._log(f"  ✔ phụ đề {origin}: {srt_path}")
        elif want_subtitle:
            # Không nhận được lời thoại (video không có tiếng, hoặc thiếu faster-whisper):
            # BỎ QUA phụ đề để không burn file .srt rỗng gây lỗi FFmpeg/libass.
            self._log("  ! không nhận được lời thoại — bỏ qua phụ đề cho video này.")

        if ecfg.export.make_content_txt:
            content_txt = str(out_base / f"{artifact_id}_content.txt")
            subtitles.write_content_txt(content_txt, work, lang,
                                        target_lang if want_tr else "")
            self._log(f"  ✔ content: {content_txt}")
        generated_voiceover = None
        if ecfg.tts.enabled:
            self._stage(row.video_id, Stage.TTS)
            spoken_lang = target_lang if want_tr else lang
            spoken_cues = [
                (cue.start, cue.end,
                 (cue.text2 if want_tr and cue.text2 else cue.text).strip())
                for cue in work
                if (cue.text2 if want_tr and cue.text2 else cue.text).strip()
            ]
            if not spoken_cues:
                self._log("  ! không có nội dung lời thoại — bỏ qua lồng tiếng.")
                return vocals, content_txt, srt_path, hook_suggestion, None
            self._log(f"  [chuẩn bị] tạo lồng tiếng Edge TTS ({spoken_lang})…")
            if vocals:
                self._log("  ↪ lời đã tách chỉ dùng nhận diện transcript; đầu ra chỉ dùng Edge TTS.")
            signature = work_cache.value_signature(
                spoken_cues, spoken_lang, ecfg.tts.voice,
                ecfg.tts.gender, ecfg.tts.rate_percent)
            cache_dir = work_cache.artifact_dir(
                ecfg.output_dir, "tts", row.video_id, signature)
            manifest = work_cache.load_json(cache_dir / "manifest.json", signature)
            cached_audio = (
                cache_dir / str(manifest.get("audio", ""))
                if manifest else None)
            if cached_audio and cached_audio.is_file():
                generated_voiceover = str(cached_audio)
                selected_voice = str(manifest.get("voice", ecfg.tts.voice))
                self._log(f"  ↪ dùng lại giọng Edge TTS: {selected_voice}")
            else:
                cache_dir.mkdir(parents=True, exist_ok=True)
                selected_voice = edge_tts_service.choose_voice(
                    spoken_lang, ecfg.tts.gender, ecfg.tts.voice)
                timed_items = []
                for index, (start, end, text) in enumerate(spoken_cues, 1):
                    self._check_cancel()
                    cue_path = cache_dir / f"cue_{index:04d}.mp3"
                    edge_tts_service.synthesize_selected(
                        text, str(cue_path), selected_voice=selected_voice,
                        rate_percent=ecfg.tts.rate_percent,
                        cancel_cb=self.cancel_cb)
                    timed_items.append((str(cue_path), start, end))
                generated_voiceover = audio_ops.compose_timed_voiceover(
                    timed_items,
                    str(cache_dir / f"{artifact_id}_edge_tts_timed.wav"),
                    cancel_cb=self.cancel_cb)
                generated_path = Path(generated_voiceover)
                work_cache.save_json(
                    cache_dir / "manifest.json", signature,
                    audio=generated_path.name, voice=selected_voice)
                self._log(
                    f"  ✔ giọng Edge TTS: {selected_voice} · "
                    f"đã đồng bộ {len(spoken_cues)} cue")
            self._check_cancel()
        return vocals, content_txt, srt_path, hook_suggestion, generated_voiceover

    def _log(self, msg: str) -> None:
        log.info(msg)
        self.on_log(msg)
