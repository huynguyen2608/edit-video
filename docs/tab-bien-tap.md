# Tab "Biên tập" — mô tả từng giá trị

Tài liệu này giải thích **chức năng** từng tùy chọn ở tab Biên tập, cùng:

- **Giá trị GIỮ NGUYÊN** = giá trị làm cho phần đó **không chỉnh sửa video** (no‑op).
- **Mặc định app** = giá trị app đặt sẵn khi chưa chỉnh (có trong `config.example.yaml`).

> ⚠️ **Lưu ý:** một số mặc định của app **KHÁC** "giữ nguyên" — tức là dù bạn không đụng gì,
> video vẫn bị đổi. Các dòng đó được đánh dấu **⚠️**. Muốn xuất bản video **gần như y hệt
> nguồn**, xem mục [Xuất video không chỉnh sửa](#xuất-video-không-chỉnh-sửa-cheat-sheet).

Cột **Khoá config** là tên trong `config.yaml` (dưới `editor:`). Chữ *(chỉ config)* nghĩa là
mục đó **chưa có trên GUI**, phải sửa trong `config.yaml`.

---

## 1) Đầu ra & khung hình

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Thư mục xuất file | `output_dir` | Nơi lưu file đã edit (`<kênh>/<id>/`) | — (chỉ là đường dẫn) | `D:/VideoRepurpose/output` |
| Khung hình đầu ra | `target_aspect` | Tỉ lệ khung: `9:16` / `1:1` / `16:9` | *Không có "giữ nguyên"* — bắt buộc reframe; chọn khung **trùng tỉ lệ nguồn** để ít đổi nhất | ⚠️ `9:16` |
| *(chỉ config)* | `fill_missing` | Cách lấp phần thiếu khi đổi khung: `blur` (nền mờ) / `pad_black` (viền đen) / `none` (crop‑to‑fill, cắt bớt) | *Không có no‑op* — mọi chế độ đều đổi. `none` = cắt sát; `blur/pad` = giữ đủ nội dung | ⚠️ `blur` |
| Cắt 2 bên % | `side_crop_percent` | (chế độ blur) cắt bớt hai bên video chính để hiển thị to hơn | **0** | ⚠️ `5` |
| Zoom fill % | `zoom_fill_percent` | (chế độ `none`) zoom thêm để lấp khung | **0** | `7` |
| *(chỉ config)* | `crop_mode` | Cách chọn vùng crop: `auto` (dò chủ thể) / `manual` / `center` | Không đổi nội dung, chỉ đổi vị trí crop khi `fill_missing=none` | `auto` |
| *(nút "Chọn vùng crop")* | `manual_focus_x/y` | Tâm vùng crop thủ công (0..1) | `0.5 / 0.5` (canh giữa) | `0.5 / 0.5` |

---

## 2) Codec & xuất bản

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Codec xuất video | `export.video_codec` | Bộ mã hoá: `h264_nvenc` (GPU) / `libx264` (CPU)… | — (luôn phải encode) | `h264_nvenc` |
| *(chỉ config)* | `export.crf_or_cq` | Chất lượng nén (thấp = đẹp hơn, file to hơn) | ~18 (gần như không mất chất) | `23` |
| Cách lấy bản short | `export.short_mode` | `start` = 100s đầu / `highlight` = đoạn sôi động nhất | `start` | `start` |
| *(chỉ config)* | `export.short_seconds` | Độ dài bản short (giây) | — | `100` |
| *(chỉ config)* | `export.make_full` | Có xuất bản full không | `true` | `true` |
| *(chỉ config)* | `export.make_short` | Có xuất bản short không | — | `true` |
| *(chỉ config)* | `export.make_content_txt` | Có xuất file nội dung `.txt` (cần Whisper) | `false` (nếu không muốn) | `true` |

> Codec/CRF/chọn bản xuất **không làm biến đổi hình ảnh nội dung**, chỉ ảnh hưởng chất lượng
> nén và số file xuất ra.

---

## 3) Biến đổi video (nhóm "Cài đặt nâng cao")

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Lật ngang (flip) | `flip_horizontal` | Lật gương trái↔phải | **false** | `false` |
| Mirror + crop | `mirror_crop` | Cắt nửa trái, lật đối xứng sang phải | **false** | `false` |
| Tốc độ video | `speed` | Nhanh/chậm video (kéo audio theo để đồng bộ) | **1.0** | `1.0` |
| Color grading bật | `color_grading.enabled` | Bật chỉnh màu | **false** | `false` |
| Brightness | `color_grading.brightness` | Độ sáng (−1..1) | **0.0** | `0.0` |
| Contrast | `color_grading.contrast` | Tương phản (0..2) | **1.0** | `1.0` |
| Saturation | `color_grading.saturation` | Độ bão hoà màu (0..3) | **1.0** | `1.0` |
| Fingerprint (chống trùng) | `fingerprint_enabled` | Biến đổi NHẸ mỗi video (speed/màu/zoom) để né bộ lọc trùng | **false** | `false` |
| *(chỉ config)* | `fingerprint_strength` | Biên độ biến đổi fingerprint (0..2) | `0` (không đổi) | `1.0` |

---

## 4) Âm thanh (nhóm "Cài đặt nâng cao")

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Tách giọng (bỏ nhạc nền) | `audio.separate_speech` | Demucs tách giữ lời thoại, bỏ nhạc (cần GPU) | **false** (giữ audio gốc) | `false` |
| Xóa HẾT âm thanh | `audio.mute_all` | Bỏ toàn bộ tiếng, kể cả voice | **false** | `false` |
| Ducking | `audio.duck_music` | Nhạc nền THAY THẾ tự hạ khi có lời thoại | Chỉ tác động khi có `replace_music`; không có nhạc thay → vô hại | `true` |
| Nhạc nền thay | `audio.replace_music` | File nhạc chèn dưới nền | **rỗng ""** (không thay) | `""` |
| *(trong nhóm)* Âm lượng nhạc | `audio.music_volume` | Âm lượng nhạc thay (0..1) | — (chỉ khi có nhạc thay) | `0.25` |
| Voiceover | `audio.voiceover` | File lồng tiếng thay toàn bộ audio gốc | **rỗng ""** | `""` |
| Pitch shift | `audio.pitch_shift_semitones` | Dịch cao độ giọng (nửa cung) | **0** | `0` |
| Tốc độ audio (thêm) | `audio.audio_speed` | Tinh chỉnh tempo RIÊNG cho audio | **1.0** | `1.0` |

---

## 5) Phụ đề & dịch (nhóm "Phụ đề & dịch")

Chỉ tác động khi **Bật phụ đề = true**. Nếu tắt, không đổi video và không xuất `.srt`.

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Bật phụ đề | `subtitle.enabled` | Tạo phụ đề + xuất `.srt` + burn lên video | **false** | `false` |
| Dịch sang | `subtitle.translate_to` | `""` = ngôn ngữ gốc; vd `en` = dịch & chỉ hiển thị bản dịch | `""` (giữ gốc) | `""` |
| Burn vào video | `subtitle.burn_in` | In phụ đề lên hình (nếu tắt: chỉ xuất `.srt`, không đổi hình) | `false` (không burn) | `true` |
| Vị trí dòng phụ đề | `subtitle.position` | `top` / `middle` / `bottom` | — | `bottom` |
| Cỡ chữ phụ đề | `subtitle.font_size` | Cỡ chữ | — | `24` |
| *(chỉ config)* | `subtitle.merge_gap_ms` | Gộp cue cách nhau < ngưỡng (ms) | — | `300` |
| *(chỉ config)* | `subtitle.min_cue_ms` | Cue ngắn hơn → gộp với lân cận (ms) | — | `1200` |
| *(chỉ config)* | `subtitle.max_cue_ms` | Trần độ dài 1 cue sau khi gộp (ms) | — | `7000` |
| *(chỉ config)* | `subtitle.translator` | Công cụ dịch: `auto`/`google`/`argos`/`none` | — | `auto` |

---

## 6) Hook / CTA / Logo (nhóm "Hook (giây đầu) / CTA (giây cuối) / Logo")

### Logo / watermark

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Bật logo | `overlay.enabled` | Chèn logo/watermark lên video | **false** | `false` |
| Ảnh logo (PNG) | `overlay.image_path` | File logo (nên có alpha) | rỗng | `""` |
| Vị trí logo | `overlay.position` | 4 góc | — | `top-right` |
| Size logo | `overlay.scale` | 0 = giữ nguyên kích thước ảnh; >0 = tỉ lệ theo chiều rộng khung | **0.0** | `0.0` |
| Độ mờ logo | `overlay.opacity` | 0..1 (1 = đục hẳn) | — (chỉ khi bật logo) | `0.8` |

### Hook (chữ giây đầu) — `intro_hook`

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Bật Hook | `intro_hook.enabled` | Hiện chữ hook ở giây đầu | **false** | `false` |
| Nội dung hook | `intro_hook.text` | Chữ hiển thị | rỗng | `""` |
| Auto hook | `intro_hook.auto` | Text trống → tự lấy câu mở đầu transcript | **false** | `false` |
| Vị trí hook | `intro_hook.position` | `top`/`middle`/`bottom` | — | `top` |
| Số giây hook | `intro_hook.seconds` | Hiện trong bao nhiêu giây đầu | — | `3` |
| Cỡ chữ hook | `intro_hook.font_size` | Cỡ chữ | — | `48` |
| Nền hook | `intro_hook.box` | Nền hộp cho dễ đọc | — | `true` |
| *(chỉ config)* | `intro_hook.fade_ms` | Mờ dần vào/ra (ms, 0 = không) | `0` | `300` |

### CTA (chữ giây cuối) — `outro_cta`

| Nhãn GUI | Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|---|
| Bật CTA | `outro_cta.enabled` | Hiện chữ CTA ở cuối | **false** | `false` |
| Nội dung cta | `outro_cta.text` | Chữ hiển thị | rỗng | `""` |
| Vị trí cta | `outro_cta.position` | `top`/`middle`/`bottom` | — | `bottom` |
| Số giây cta | `outro_cta.seconds` | Hiện trong bao nhiêu giây cuối | — | `10` |
| Cỡ chữ cta | `outro_cta.font_size` | Cỡ chữ | — | `42` |
| Nền cta | `outro_cta.box` | Nền hộp | — | `true` |
| *(chỉ config)* | `outro_cta.fade_ms` | Mờ dần vào/ra (ms) | `0` | `300` |

### Picture‑in‑Picture *(chỉ config, chưa có GUI)* — `picture_in_picture`

| Khoá config | Chức năng | Giá trị GIỮ NGUYÊN | Mặc định app |
|---|---|---|---|
| `picture_in_picture.enabled` | Chèn ảnh PiP nhỏ | **false** | `false` |
| `picture_in_picture.image_path` | Ảnh PiP | rỗng | `""` |
| `picture_in_picture.scale` | Tỉ lệ so với khung | — | `0.25` |
| `picture_in_picture.position` | Vị trí | — | `bottom-right` |

---

## 7) Nút hành động (không phải "giá trị")

| Nhãn | Việc |
|---|---|
| **Áp dụng preset** | Áp bộ cài TikTok/Reels/Shorts/1:1/YouTube (đổi khung + độ dài + an toàn chữ) |
| **Xem trước (video đã tải)** | Render 1 khung xem thử của 1 video |
| **Xem trước hàng loạt** | Render 1 khung cho mọi video đã tải |
| **Đặt vị trí trực quan** | Bấm lên preview để đặt vị trí Logo/Hook/CTA |
| **Khôi phục mặc định** | Đưa nhóm "Cài đặt nâng cao" về mặc định app |
| **Edit video mới đã tải** | Đẩy video chưa edit vào hàng đợi |
| **Chọn vùng crop (manual)** | Chọn tâm vùng crop → đặt `crop_mode=manual`, `fill_missing=none` |
| **Import + Edit video trong folder** | Edit video trong `input_folder` (luồng độc lập) |

---

## Xuất video KHÔNG chỉnh sửa (cheat‑sheet)

Vì một số **mặc định app vẫn đổi video**, muốn xuất bản **gần như y hệt nguồn** thì đặt:

```yaml
editor:
  target_aspect: "16:9"        # hoặc tỉ lệ trùng nguồn của bạn
  fill_missing: "none"         # crop-to-fill; nếu nguồn đã đúng tỉ lệ -> không thêm viền/mờ
  side_crop_percent: 0         # ⬅ mặc định 5 -> cắt cạnh; đặt 0
  zoom_fill_percent: 0         # ⬅ mặc định 7
  flip_horizontal: false
  mirror_crop: false
  speed: 1.0
  fingerprint_enabled: false
  color_grading: { enabled: false }
  audio:
    separate_speech: false
    mute_all: false
    replace_music: ""
    voiceover: ""
    pitch_shift_semitones: 0
    audio_speed: 1.0
  subtitle: { enabled: false }
  overlay: { enabled: false }
  intro_hook: { enabled: false }
  outro_cta: { enabled: false }
  picture_in_picture: { enabled: false }
```

Tóm tắt **các giá trị "giữ nguyên video"**: mọi công tắc **tắt (false)**, `speed`/`contrast`/
`saturation`/`audio_speed` = **1.0**, `brightness`/`pitch`/`side_crop_percent`/`zoom_fill_percent`
= **0**, các đường dẫn file = **rỗng**. Riêng **khung hình** và **fill_missing** luôn có tác
động (không có lựa chọn "không reframe"); chọn khung trùng nguồn + `fill_missing: none` để ít đổi nhất.
