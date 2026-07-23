# Video Repurpose Studio

Ứng dụng desktop (Windows) tải video **từ kênh YouTube của chính bạn** và biên tập lại
để đăng sang nền tảng khác: reframe 9:16 / 1:1 / 16:9, các biến đổi hình/âm, và xuất
bản ngắn 100s + bản full + file content `.txt`.

> Phạm vi sử dụng: công cụ này dành cho nội dung **bạn sở hữu hoặc có license**. Tải và
> đăng lại video của người khác là vi phạm bản quyền và điều khoản nền tảng.

---

## Trạng thái hiện tại (thành thật)

| Phần                                                                                     | Trạng thái                                                                  |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Giám sát kênh (RSS) + tải yt-dlp + dedup + scheduler 30 phút                             | ✅ Hoàn chỉnh                                                               |
| Kho trạng thái = file Excel nhiều sheet (chỉ edit video mới, bỏ qua đã edit)             | ✅ Hoàn chỉnh                                                               |
| GUI PySide6 (nút Quét, bảng trạng thái, log)                                             | ✅ Chạy được (cần cài PySide6 để mở)                                        |
| Reframe video (crop-to-fill / blur-fill / pad), flip, mirror, color, speed, overlay, PiP | ✅ Đã kiểm thử với FFmpeg thật                                              |
| Audio: pitch shift, đổi tốc độ, giữ audio gốc                                            | ✅ Đã kiểm thử                                                              |
| Xuất bản full + short 100s                                                               | ✅ Đã kiểm thử                                                              |
| Tách giọng/nhạc nền (Demucs)                                                             | ⚠️ Đã nối CLI, cần cài `requirements-ml.txt` + GPU để chạy thực tế          |
| Transcribe -> content.txt (Whisper)                                                      | ⚠️ Đã nối, cần cài `requirements-ml.txt`                                    |
| Smart-crop tự động (dò focus)                                                            | ⚠️ Bản Haar face-detect + fallback tâm khung; nâng cấp YOLO/saliency về sau |
| Trộn nhạc nền thay thế / voiceover vào audio cuối                                        | 🔜 Có khung, hoàn thiện ở giai đoạn sau                                     |
| Chọn vùng crop bằng tay + preview trên GUI                                               | 🔜 Giai đoạn sau                                                            |

14 unit test logic + 8 case integration với FFmpeg thật đều pass (xem `run_tests.py`, `tests/`).

---

## Kiến trúc

```
run.py                    # điểm khởi động: GUI, hoặc --scan / --edit (headless)
app/
  config.py               # đọc config.yaml -> dataclass
  store.py                # kho trạng thái = file Excel (data.xlsx) nhiều sheet
  paths.py                # đường dẫn an toàn Windows (D:\<kênh>\<video_id>\)
  logging_setup.py
  downloader/
    monitor.py            # RSS feed kênh (phát hiện video mới, không tốn quota)
    downloader.py         # yt-dlp wrapper
    scheduler.py          # ScanService: điều phối quét+tải, APScheduler 30 phút, khóa chống chạy đè
  editor/
    video_ops.py          # hình học crop + dựng filter graph (hàm thuần, test kỹ)
    smart_crop.py         # probe kích thước + dò focus (OpenCV)
    audio_ops.py          # Demucs, pitch/tempo
    transcribe.py         # faster-whisper -> content.txt
    export.py             # ghép lệnh ffmpeg cuối + render full/short
    pipeline.py           # điều phối edit 1 video, cập nhật DB
  workers/jobs.py         # QThread cho quét/edit (UI không đơ)
  ui/main_window.py       # GUI 2 tab: Tải / Biên tập
```

Hai trục trạng thái độc lập trong file Excel (sheet `videos`) giúp "chỉ sửa video mới":

- `download_status`: pending → downloading → downloaded | failed
- `edit_status`: pending → processing → done | failed

Module edit chỉ nhặt video `downloaded` + `edit_status='pending'`.

---

## Cài đặt (Windows, có GPU NVIDIA)

1. **Python 3.12** (khuyến nghị) và **FFmpeg**. Cài ffmpeg và thêm vào PATH:
   ```
   winget install Python.Python.3.12   # nếu chưa có Python 3.12
   winget install Gyan.FFmpeg          # hoặc: choco install ffmpeg-full
   ffmpeg -version                     # kiểm tra
   ```
2. Tạo môi trường ảo (Python 3.12) và cài phần lõi:
   ```
   py -3.12 -m venv .venv
   source .venv/Scripts/activate
   pip install -r requirements.txt
   ```
3. (Tùy chọn, để bật tách giọng + transcribe) cài phần AI. **Cài torch đúng bản CUDA trước**:
   ```
   pip install torch --index-url https://download.pytorch.org/whl/cu121
   pip install -r requirements-ml.txt
   ```

---

## Cấu hình

Chạy lần đầu sẽ tự tạo `config.yaml` từ `config.example.yaml`. Mở ra và chỉnh:

- `download.root_dir`: thư mục ổ D lưu video tải (vd `D:/VideoRepurpose/downloads`).
- `download.channels`: kênh **của bạn** (dán URL `@handle` hoặc điền sẵn `channel_id: UC...`).
- `download.scan_interval_minutes`: mặc định 30.
- `download.cookies_from_browser`: `chrome`/`edge`/`firefox` để giảm bị chặn bot (khuyến nghị).
- `editor.target_aspect`, `crop_mode` (`auto`/`manual`/`center`), `fill_missing` (`blur`/`pad_black`/`none`).
- `editor.export.video_codec`: `h264_nvenc` (GPU) hoặc `libx264` (CPU).

---

## Chạy

```
python run.py                    # mở GUI
python run.py --scan             # quét + tải 1 lần rồi thoát (dùng cho Task Scheduler)
python run.py --edit             # biên tập toàn bộ video ĐÃ TẢI mới rồi thoát
python run.py --auto             # CHẠY LIÊN TỤC: quét định kỳ + tải xong tự đẩy sang edit
python run.py --edit-folder "D:/videos_in"   # EDIT ĐỘC LẬP mọi video trong folder (không tải)
python run.py --edit --device cpu            # ép CPU nếu không có GPU
```

GUI tự bật quét định kỳ 30 phút ở nền. Nút "Quét ngay" và job tự động dùng chung một
khóa nên không chạy đè lên nhau. Tab "Tải" có ô **"Tự động quét & tải"** để **bật/tắt**
luồng tự động của module cập nhật.

**Luồng auto (1 hàng đợi edit liên tục):** khi mỗi video tải xong, nó được đẩy sang
**hàng đợi edit**; một luồng duy nhất xử lý lần lượt tới hết list (đang bận thì nối đuôi,
không chạy song song). Bật/tắt bằng `editor.auto_edit_after_download` (mặc định bật).
`--auto` chạy headless suốt ngày; GUI cũng phản ứng tương tự.

**Luồng edit ĐỘC LẬP theo folder:** đặt `editor.input_folder` (hoặc bấm "Chọn folder…"
trên GUI), bỏ file video vào folder đó rồi bấm **"Import + Edit video trong folder"** —
app chỉ edit video **mới thêm / chưa edit** (bỏ qua video đã xong), không cần module tải.
Tương đương CLI `--edit-folder`. Output nhóm theo tên folder: `output/<tên folder>/<id>/`.

**Kho dữ liệu Excel `data.xlsx`** gồm các sheet: `videos` (trạng thái), `channels`,
`exports` (log xuất bản: 3 file + thời điểm), `events` (log lịch sử tải/edit/lỗi).

**Đầu ra mỗi video:** `output/<kênh>/<video_id>/` chứa `<id>_full.mp4`, `<id>_short.mp4`,
`<id>_content.txt`, và `<id>.srt` **nếu bật phụ đề** (`editor.subtitle.enabled`).

**Hook / CTA / Logo:** `editor.intro_hook` (chữ giây đầu) + `editor.outro_cta` (chữ giây
cuối) — bật/tắt, đổi nội dung, vị trí (top/middle/bottom), cỡ chữ, nền hộp + fade; hook có
thể `auto` tự lấy câu mở đầu. `editor.overlay` thêm logo với vị trí + `scale` + độ mờ.

**Tính năng nâng cao khác:**

- **Short highlight**: `export.short_mode: highlight` tự chọn đoạn sôi động nhất (theo độ to) thay vì 100s đầu.
- **Ducking**: `audio.duck_music` — nhạc nền tự hạ khi có lời thoại.
- **Preset nền tảng**: TikTok / Reels / Shorts / 1:1 / YouTube (1 cú áp khung + độ dài + an toàn chữ).
- **Fingerprint chống trùng**: `editor.fingerprint_enabled` — biến đổi nhẹ (xác định theo video_id) mỗi bản xuất.
- **Xem trước 1 khung**: từng video hoặc **hàng loạt** (không cần render đầy đủ); nút "Đặt vị trí trực quan" bấm trên preview.
- **Tải song song + retry**: `download.max_parallel`, `download.retry_failed`.
- **Xuất báo cáo**: tổng hợp log `exports`/`events` theo ngày (nút trong tab Lịch sử).

**Phụ đề & dịch:** bật `editor.subtitle` để xuất `.srt` + in phụ đề lên video. Mặc định
là **ngôn ngữ gốc**; đặt `translate_to` (vd `en`) để **dịch** (dịch máy miễn phí, không
key: deep-translator/argostranslate) và chỉ **hiển thị bản dịch** lên màn hình. Chọn được
vị trí (`top/middle/bottom`) và cỡ chữ. Các cue hội thoại ngắn/sát nhau được **gộp trước
khi dịch** để bản dịch bám đúng câu (không sai lời). Content.txt có 2 cột: gốc + bản dịch.

---

## Lưu ý vận hành (đọc kỹ)

- **yt-dlp hỏng theo YouTube**: chạy `pip install -U yt-dlp` định kỳ. Đây là nguồn lỗi
  phổ biến nhất của phần tải.
- **Chống bot YouTube**: tải tự động 30 phút/lần từ một IP có thể bị gắn cờ
  ("Sign in to confirm you're not a bot"). Dùng `cookies_from_browser` và giãn tần suất
  nếu bị chặn.
- **Demucs tách giọng không sạch tuyệt đối**: sẽ có artifact khi nền nhạc phức tạp. Cần GPU.
- **Smart-crop tự động** hiện bám khuôn mặt; video không có mặt sẽ rơi về canh giữa.
  Với nội dung quan trọng nên dùng `crop_mode: manual` (chọn vùng tay — bản GUI chọn vùng
  sẽ bổ sung ở giai đoạn sau).
- **Whisper/Demucs trên CPU rất chậm** — dùng GPU cho 15–30 video/ngày.

---

## Chạy nhanh bằng cú click (không cần build)

Bấm đúp **`VideoRepurposeStudio.bat`** — nó tự dùng `.venv` (nếu có) hoặc `py -3.12`
để mở GUI.

## Đóng gói thành .exe (1 cú click)

Bấm đúp **`build_exe.bat`** (tự cài `pyinstaller` + dependencies theo Python 3.12, rồi
build `--windowed`). Kết quả:

```
dist\VideoRepurposeStudio\VideoRepurposeStudio.exe   <- click để chạy
```

Tương đương lệnh:

```
py -3.12 -m pip install -r requirements.txt pyinstaller
py -3.12 -m PyInstaller --noconfirm --clean --windowed --name VideoRepurposeStudio ^
    --add-data "config.example.yaml;." run.py
```

`config.yaml`, `data.xlsx`, `logs/` sẽ được tạo cạnh file `.exe` khi chạy. Lưu ý: app
kèm torch/CUDA sẽ ra file rất lớn và dễ bị antivirus báo nhầm; cân nhắc bundle FFmpeg
và ký số nếu phát hành.

---

## Kiểm thử

```
python run_tests.py      # harness stdlib (không cần pytest)
# hoặc trên máy đủ mạng:
pip install pytest && pytest -q
```
