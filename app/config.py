"""Đọc/ghi cấu hình YAML thành dataclass có kiểu rõ ràng.

Dùng dataclass thay vì dict trần để lỗi cấu hình lộ ra sớm và IDE gợi ý được.
"""
from __future__ import annotations

import typing
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any


# --------------------------- Download ---------------------------
@dataclass
class ChannelCfg:
    name: str
    url: str = ""
    channel_id: str = ""


@dataclass
class DownloadCfg:
    root_dir: str = "D:/VideoRepurpose/downloads"
    channels: list[ChannelCfg] = field(default_factory=list)
    format: str = "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    quality_height: int = 1080   # 0 = tốt nhất; >0 = cao nhất không vượt mức đã chọn
    since: str = ""
    until: str = ""             # ngày cuối YYYY-MM-DD; rỗng = đến hiện tại
    history_scan: bool = False   # False = RSS nhanh (~15 video), True = duyệt lịch sử yt-dlp
    history_limit: int = 500     # giới hạn số video duyệt mỗi kênh để tránh quét vô hạn
    lookback_days: int = 1       # chỉ lấy video trong N ngày gần nhất (1/7/30/60)
    scan_interval_minutes: int = 30
    cookies_from_browser: str = ""
    cookies_file: str = ""        # Netscape cookies.txt; ưu tiên hơn cookie trình duyệt
    max_parallel: int = 1        # số video tải song song (1 = tuần tự; tăng dễ bị chặn bot)
    retry_failed: bool = True    # mỗi lần quét, thử lại các video tải lỗi trước đó


# --------------------------- Editor ---------------------------
@dataclass
class ColorGradingCfg:
    enabled: bool = False
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0


@dataclass
class OverlayCfg:
    enabled: bool = False
    image_path: str = ""
    position: str = "top-right"    # top-left | top-right | bottom-left | bottom-right
    opacity: float = 0.8
    scale: float = 0.0             # 0 = giữ nguyên kích thước; >0 = tỉ lệ theo CHIỀU RỘNG khung


@dataclass
class TextOverlayCfg:
    """Chữ hiện theo thời gian: hook (giây đầu) / CTA (giây cuối)."""
    enabled: bool = False
    text: str = ""
    seconds: float = 3.0
    position: str = "top"          # top | middle | bottom
    font_size: int = 48
    box: bool = True               # nền hộp bán trong suốt cho dễ đọc
    fade_ms: int = 300             # mờ dần vào/ra (0 = không)
    auto: bool = False             # hook: nếu text rỗng -> tự lấy câu mở đầu transcript


@dataclass
class PipCfg:
    enabled: bool = False
    image_path: str = ""
    scale: float = 0.25
    position: str = "bottom-right"


@dataclass
class AudioCfg:
    separate_speech: bool = False
    # Bộ tách giọng: mdx (audio-separator, NHẸ, chạy CPU tốt, khuyên cho máy yếu) |
    # demucs (chất lượng cao, cần GPU) | vr (VR Architecture, cần torch)
    separator_backend: str = "mdx"
    separator_model: str = ""    # để trống = model mặc định theo backend
    replace_music: str = ""
    music_volume: float = 0.25
    voiceover: str = ""
    pitch_shift_semitones: int = 0
    audio_speed: float = 1.0
    mute_all: bool = False   # xóa HẾT âm thanh của video xuất (kể cả voice)
    duck_music: bool = True  # nhạc nền tự HẠ khi có lời thoại (sidechaincompress)
    # Giữ track lời thoại gốc, chỉ làm sạch/cân bằng nhẹ. Tắt mặc định để config
    # cũ tiếp tục copy nguyên audio cho tới khi người dùng chủ động chọn chế độ.
    enhance_original_voice: bool = False
    gain_db: float = 0.0
    bass_db: float = 0.0
    mid_db: float = 0.5
    treble_db: float = 0.5
    highpass_hz: int = 80       # 0 = tắt
    lowpass_hz: int = 18000     # 0 = tắt
    noise_reduction_percent: int = 10
    compressor_enabled: bool = True
    compressor_threshold_db: float = -18.0
    compressor_ratio: float = 2.0
    compressor_attack_ms: float = 10.0
    compressor_release_ms: float = 100.0
    deesser_db: float = 0.0     # 0 = tắt
    loudness_enabled: bool = True
    loudness_stereo_lufs: float = -16.0
    loudness_mono_lufs: float = -19.0
    limiter_enabled: bool = True
    limiter_ceiling_db: float = -1.0


@dataclass
class SubtitleCfg:
    enabled: bool = False          # thêm phụ đề & xuất .srt hay không (mặc định: không)
    translate_to: str = ""         # "" = giữ NGÔN NGỮ GỐC; vd "en","vi","ja" = dịch sang
    burn_in: bool = True           # hiển thị phụ đề LÊN video (chỉ ngôn ngữ đã chọn)
    position: str = "bottom"       # top | middle | bottom
    font_size: int = 24
    merge_gap_ms: int = 300        # gộp cue cách nhau < ngưỡng (đoạn ngắn gần nhau)
    min_cue_ms: int = 1200         # cue ngắn hơn -> gộp với lân cận để dịch không sai lời
    max_cue_ms: int = 7000         # trần độ dài 1 cue sau khi gộp
    translator: str = "auto"       # auto | google | argos | none


@dataclass
class TtsCfg:
    enabled: bool = False
    # Khi phụ đề bật, ngôn ngữ đọc lấy từ translate_to (hoặc ngôn ngữ được nhận diện).
    # language chỉ dùng khi phụ đề tắt.
    language: str = "vi"
    voice: str = ""                 # ShortName của Edge TTS; rỗng = tự chọn phù hợp
    gender: str = ""                # "" | Female | Male
    rate_percent: int = 0


@dataclass
class ExportCfg:
    make_short: bool = True
    short_seconds: int = 100
    short_mode: str = "start"   # start = 100s đầu | highlight = đoạn sôi động nhất
    short_cut_mode: str = "accurate"  # accurate = đúng frame | fast = stream copy
    make_full: bool = True
    make_content_txt: bool = False
    video_codec: str = "libx264"
    crf_or_cq: int = 20
    encoder_preset: str = "medium"
    output_short_edge: int = 0     # 0 = tự chọn theo nguồn; 720/1080/1440/2160
    audio_bitrate_kbps: int = 256
    copy_audio_when_unchanged: bool = True


@dataclass
class EditorCfg:
    output_dir: str = "D:/VideoRepurpose/output"
    auto_edit_after_download: bool = True   # tải xong -> tự đẩy sang hàng đợi edit
    processing_device: str = "auto"        # auto | cuda | cpu (Whisper/Demucs)
    # Folder video đầu vào cho luồng edit ĐỘC LẬP (không cần tải từ YouTube).
    input_folder: str = ""
    # 0 = off; 4/5 = split videos longer than 10 minutes before processing.
    long_video_segment_minutes: int = 0
    target_aspect: str = "9:16"
    crop_mode: str = "auto"          # auto (dò chủ thể) | manual (dùng manual_focus_*) | center
    fill_missing: str = "blur"       # blur | pad_black | none (crop-to-fill quanh focus)
    zoom_fill_percent: int = 0
    # Crop bớt HAI BÊN của video chính (foreground) TRƯỚC khi scale, ở chế độ blur —
    # để nội dung hiển thị to hơn. Tính theo % CHIỀU RỘNG mỗi bên, chỉnh 0..10 (mặc định 5).
    side_crop_percent: float = 2.0
    side_squeeze_percent: float = 0.0   # nén ngang nguồn ĐÚNG 16:9 trước khi reframe (0..10%)
    manual_focus_x: float = 0.5      # dùng khi crop_mode=manual (toạ độ chuẩn hoá 0..1)
    manual_focus_y: float = 0.5
    fingerprint_enabled: bool = False  # biến đổi nhẹ MỖI video để chống trùng nội dung
    fingerprint_strength: float = 1.0  # 0..2 (hệ số biên độ biến đổi)
    flip_horizontal: bool = False
    mirror_crop: bool = False
    speed: float = 0.98
    color_grading: ColorGradingCfg = field(default_factory=ColorGradingCfg)
    overlay: OverlayCfg = field(default_factory=OverlayCfg)
    picture_in_picture: PipCfg = field(default_factory=PipCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    subtitle: SubtitleCfg = field(default_factory=SubtitleCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    intro_hook: TextOverlayCfg = field(default_factory=lambda: TextOverlayCfg(seconds=3, position="top"))
    outro_cta: TextOverlayCfg = field(default_factory=lambda: TextOverlayCfg(seconds=10, position="bottom", font_size=42))
    export: ExportCfg = field(default_factory=ExportCfg)


@dataclass
class LoggingCfg:
    level: str = "INFO"
    file: str = "logs/app.log"


@dataclass
class AppConfig:
    download: DownloadCfg = field(default_factory=DownloadCfg)
    editor: EditorCfg = field(default_factory=EditorCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)


def _build(dc_type, data: dict[str, Any]):
    """Dựng dataclass lồng nhau từ dict, bỏ qua key thừa, giữ default cho key thiếu.

    Lưu ý: file dùng ``from __future__ import annotations`` nên ``field.type`` là
    CHUỖI, không phải class. Phải dùng ``typing.get_type_hints`` để resolve chuỗi
    thành class thật, nếu không các section lồng (color_grading, audio, export…) sẽ
    bị gán dict thay vì dataclass và app crash khi truy cập thuộc tính.
    """
    if not isinstance(data, dict):
        return dc_type()
    try:
        hints = typing.get_type_hints(dc_type)
    except Exception:
        hints = {}
    kwargs: dict[str, Any] = {}
    for f in fields(dc_type):
        name = f.name
        if name not in data:
            continue
        val = data[name]
        ftype = hints.get(name, f.type)
        # Danh sách kênh
        if name == "channels" and isinstance(val, list):
            valid = {ff.name for ff in fields(ChannelCfg)}
            kwargs[name] = [ChannelCfg(**{k: v for k, v in c.items() if k in valid})
                            for c in val if isinstance(c, dict)]
        elif is_dataclass(ftype) and isinstance(val, dict):
            kwargs[name] = _build(ftype, val)
        else:
            kwargs[name] = val
    return dc_type(**kwargs)


def load_config(path: str | Path) -> AppConfig:
    import yaml  # import trễ: logic _build test được mà không cần cài PyYAML
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Không tìm thấy {p}. Copy config.example.yaml -> config.yaml và chỉnh."
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig(
        download=_build(DownloadCfg, raw.get("download", {})),
        editor=_build(EditorCfg, raw.get("editor", {})),
        logging=_build(LoggingCfg, raw.get("logging", {})),
    )


def save_config(cfg: AppConfig, path: str | Path) -> None:
    import yaml
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(asdict(cfg), allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
