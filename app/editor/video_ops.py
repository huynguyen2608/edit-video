"""Xây dựng filter graph FFmpeg cho phần biến đổi hình.

Toàn bộ hàm hình học ở đây là hàm THUẦN (không I/O) nên unit test được, đây là
nơi dễ sai nhất (crop lệch khung, tràn biên). Việc chạy ffmpeg nằm ở export.py.

Quy ước:
- reframe = đưa video nguồn về đúng khung đích (9:16, 1:1, 16:9).
  * crop-to-fill: cắt vùng lớn nhất đúng tỉ lệ quanh điểm focus -> lấp đầy khung,
    mất bớt nội dung hai bên (hợp với "chọn vùng action chính + zoom fill").
  * blur-fill: giữ toàn bộ nội dung, phần thiếu lấp bằng bản phóng to làm mờ
    (đúng yêu cầu "tạo vùng mờ ở những phần còn thiếu").
"""
from __future__ import annotations

from dataclasses import dataclass

# Độ phân giải chuẩn theo tỉ lệ.
_RES = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}


def target_resolution(aspect: str, short_edge: int = 1080) -> tuple[int, int]:
    if aspect not in _RES:
        raise ValueError(f"target_aspect không hợp lệ: {aspect} (chỉ nhận 9:16, 1:1, 16:9)")
    short_edge = max(2, _even(short_edge))
    if aspect == "1:1":
        return short_edge, short_edge
    long_edge = _even(short_edge * 16 / 9)
    return (short_edge, long_edge) if aspect == "9:16" else (long_edge, short_edge)


def _even(n: float) -> int:
    """Encoder yêu cầu kích thước chẵn."""
    i = int(round(n))
    return i - (i % 2)


@dataclass(frozen=True)
class CropRect:
    x: int
    y: int
    w: int
    h: int

    def as_ffmpeg(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


def compute_crop_rect(src_w: int, src_h: int, target_ar: float,
                      focus_x: float = 0.5, focus_y: float = 0.5,
                      zoom_percent: float = 0.0) -> CropRect:
    """Tính vùng cắt lớn nhất theo tỉ lệ target_ar, canh quanh điểm focus, có zoom.

    focus_x/focus_y: toạ độ chuẩn hoá 0..1 (tâm vùng action chính).
    zoom_percent: 0 = vùng lớn nhất; >0 = thu nhỏ vùng cắt (zoom vào) N%.
    Kết quả luôn nằm trong khung nguồn và có kích thước chẵn.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("Kích thước nguồn không hợp lệ")
    src_ar = src_w / src_h

    if src_ar >= target_ar:
        crop_h = float(src_h)
        crop_w = crop_h * target_ar
    else:
        crop_w = float(src_w)
        crop_h = crop_w / target_ar

    z = max(0.0, zoom_percent) / 100.0
    crop_w /= (1.0 + z)
    crop_h /= (1.0 + z)

    crop_w = min(crop_w, src_w)
    crop_h = min(crop_h, src_h)

    # canh tâm quanh focus rồi kẹp biên
    cx = focus_x * src_w
    cy = focus_y * src_h
    x = cx - crop_w / 2.0
    y = cy - crop_h / 2.0
    x = max(0.0, min(x, src_w - crop_w))
    y = max(0.0, min(y, src_h - crop_h))

    return CropRect(_even(x), _even(y), _even(crop_w), _even(crop_h))


def side_crop_expr(side_crop_percent: float) -> str:
    """Filter cắt HAI BÊN của foreground theo % CHIỀU RỘNG mỗi bên (kẹp 0..10%).

    - Dùng biểu thức `iw` nên độc lập độ phân giải (5% của 1920 hay 1280 đều đúng).
    - Ép bề rộng chẵn (trunc(.../2)*2) để tránh cảnh báo scaler.
    - Trả "" nếu 0% (không chèn crop).
    Ví dụ 5%: crop=trunc(iw*0.9000/2)*2:ih:trunc(iw*0.0500):0
    """
    p = max(0.0, min(10.0, float(side_crop_percent))) / 100.0
    if p <= 0:
        return ""
    keep = 1.0 - 2.0 * p
    return f"crop=trunc(iw*{keep:.4f}/2)*2:ih:trunc(iw*{p:.4f}):0,"


def four_edge_crop_expr(crop_percent: float) -> str:
    """Cắt cùng tỷ lệ ở cả 4 cạnh, giữ nguyên tỉ lệ nguồn để zoom không méo hình."""
    p = max(0.0, min(10.0, float(crop_percent))) / 100.0
    if p <= 0:
        return ""
    keep = 1.0 - 2.0 * p
    return (f"crop=trunc(iw*{keep:.4f}/2)*2:trunc(ih*{keep:.4f}/2)*2:"
            f"trunc(iw*{p:.4f}):trunc(ih*{p:.4f}),")


def build_reframe_filter(src_w: int, src_h: int, aspect: str,
                         fill_missing: str = "blur",
                         focus_x: float = 0.5, focus_y: float = 0.5,
                         zoom_percent: float = 0.0,
                         side_crop_percent: float = 0.0,
                         output_short_edge: int = 1080,
                         side_squeeze_percent: float = 0.0) -> str:
    """Trả chuỗi filter_complex đưa [0:v] về khung đích, gán nhãn [rf] ở cuối.

    - fill_missing="none": crop-to-fill quanh (focus_x, focus_y) + zoom -> lấp đầy
      khung, mất bớt nội dung hai bên. Đây là chế độ "chọn vùng action chính".
    - fill_missing="blur": với nguồn khác 9:16, TRƯỚC khi scale cắt hai bên
      foreground `side_crop_percent`% mỗi bên (theo chiều rộng) để nội dung hiển thị
      TO HƠN, rồi fit vào khung canh giữa (giữ tỉ lệ, không méo). Nền là bản sao
      phóng-to-phủ-đầy + làm mờ (KHÔNG cắt) nên không bao giờ có viền đen.
      Riêng nguồn đã là 9:16 và đầu ra 9:16: cắt cùng % ở cả 4 cạnh rồi scale trở
      lại toàn khung; không tạo nền mờ.
    - fill_missing="pad_black": fit vào khung, lấp đen (giữ nguyên như cũ).

    Việc chọn focus (auto dò chủ thể / manual người dùng chọn / center) do pipeline
    quyết và truyền qua focus_x/focus_y; hàm này chỉ dựng filter.
    """
    out_w, out_h = target_resolution(aspect, output_short_edge)
    target_ar = out_w / out_h
    src_ar = src_w / src_h if src_h else 0.0

    # Co ngang: CHỈ nguồn đúng 16:9 — nén bớt chiều rộng TRƯỚC khi reframe/cắt 2 bên,
    # để giữ nhiều nội dung hai bên hơn khi về 9:16 (đổi lại hình hơi thon). Kẹp 0..10%.
    sq = max(0.0, min(10.0, float(side_squeeze_percent))) / 100.0
    is_16_9 = bool(src_ar) and abs(src_ar - 16.0 / 9.0) / (16.0 / 9.0) <= 0.02
    prefix, vin, eff_w = "", "0:v", src_w
    if sq > 0 and is_16_9:
        keep = 1.0 - sq
        prefix = f"[0:v]scale=trunc(iw*{keep:.4f}/2)*2:ih:flags=lanczos[sqz];"
        vin, eff_w = "sqz", int(round(src_w * keep))

    if fill_missing in ("blur", "pad_black"):
        # Giữ toàn nội dung: fit vào khung, lấp nền.
        if fill_missing == "blur":
            same_portrait_frame = (aspect == "9:16" and target_ar > 0 and
                                   abs(src_ar - target_ar) / target_ar <= 0.02)
            if same_portrait_frame:
                crop4 = four_edge_crop_expr(side_crop_percent)
                return f"{prefix}[{vin}]{crop4}scale={out_w}:{out_h}:flags=lanczos[rf]"
            sc = side_crop_expr(side_crop_percent)  # cắt hai bên foreground trước khi scale
            return (
                f"{prefix}[{vin}]split=2[bg][fg];"
                f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={out_w}:{out_h},gblur=sigma=22[bgb];"
                f"[fg]{sc}scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[rf]"
            )
        return (  # pad_black — không đổi (ngoài co ngang)
            f"{prefix}[{vin}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black[rf]"
        )

    # crop-to-fill quanh focus (rect tính theo chiều rộng ĐÃ co để cắt đúng)
    rect = compute_crop_rect(eff_w, src_h, target_ar, focus_x, focus_y, zoom_percent)
    return f"{prefix}[{vin}]{rect.as_ffmpeg()},scale={out_w}:{out_h}:flags=lanczos[rf]"


def build_transform_chain(label_in: str, label_out: str, *,
                          flip_horizontal: bool = False, mirror_crop: bool = False,
                          brightness: float = 0.0, contrast: float = 1.0,
                          saturation: float = 1.0, color_enabled: bool = False) -> str:
    """Nối các biến đổi 1-input sau reframe: lật ngang, mirror, color grading.

    mirror_crop là tham số tương thích cấu hình cũ và không còn tạo hiệu ứng.
    flip_horizontal luôn lật TOÀN BỘ khung bằng hflip.
    """
    parts: list[str] = []
    if flip_horizontal:
        parts.append("hflip")
    if color_enabled:
        parts.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")

    if not parts:
        return f"[{label_in}]copy[{label_out}]"
    return f"[{label_in}]{','.join(parts)}[{label_out}]"


def _fmt_num(f: float) -> str:
    """Định dạng số gọn, luôn giữ ít nhất 1 chữ số thập phân (2.0, 0.5, 1.234567)."""
    s = f"{f:.6f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def speed_filters(speed: float) -> tuple[str, list[str]]:
    """Trả (video setpts, danh sách atempo) cho đổi tốc độ.

    atempo chỉ nhận 0.5..2.0 nên tốc độ ngoài khoảng phải xâu chuỗi nhiều atempo.
    Định dạng đồng nhất để chuỗi filter nhất quán và dễ kiểm thử.
    """
    if speed <= 0:
        raise ValueError("speed phải > 0")
    v = f"setpts={1.0/speed:.6f}*PTS"
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return v, [f"atempo={_fmt_num(f)}" for f in factors]
