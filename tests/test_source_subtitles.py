from app.downloader.downloader import YtDlpDownloader
from app.editor import subtitles


def test_downloader_requests_manual_and_auto_srt(tmp_path):
    opts = YtDlpDownloader(str(tmp_path), "best")._ydl_opts(tmp_path, "vid", None)
    assert opts["writesubtitles"] is True
    assert opts["writeautomaticsub"] is True
    assert opts["subtitlesformat"] == "srt/best"
    assert "-live_chat" in opts["subtitleslangs"]


def test_downloader_selects_one_source_language():
    info = {
        "language": "vi",
        "subtitles": {"vi": [{}], "en": [{}]},
        "automatic_captions": {"vi-orig": [{}], "fr": [{}]},
    }
    assert YtDlpDownloader._select_subtitle_languages(info) == ["vi"]
    auto_only = {"language": "ja", "automatic_captions": {"ja-orig": [{}], "en": [{}]}}
    assert YtDlpDownloader._select_subtitle_languages(auto_only) == ["ja-orig"]


def test_find_and_parse_downloaded_srt(tmp_path):
    video = tmp_path / "My Video.mp4"
    video.touch()
    sidecar = tmp_path / "My Video.vi-orig.srt"
    sidecar.write_text(
        "1\n00:00:01,000 --> 00:00:03,250\n<b>Xin chào</b>\n",
        encoding="utf-8",
    )
    found, lang = subtitles.find_source_subtitle(video)
    assert found == sidecar and lang == "vi"
    cues = subtitles.read_subtitle(found)
    assert len(cues) == 1
    assert cues[0].start == 1.0 and cues[0].end == 3.25
    assert cues[0].text == "Xin chào"


def test_parse_ass_for_tts(tmp_path):
    ass = tmp_path / "clip.en.ass"
    ass.write_text(
        "[Events]\n"
        "Dialogue: 0,0:00:02.00,0:00:04.50,Default,,0,0,0,,{\\i1}Hello\\Nworld\n",
        encoding="utf-8",
    )
    cues = subtitles.read_subtitle(ass)
    assert len(cues) == 1
    assert cues[0].start == 2.0 and cues[0].end == 4.5
    assert cues[0].text == "Hello\nworld"
