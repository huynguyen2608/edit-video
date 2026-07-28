"""Test ProgressTracker: % tổng theo công đoạn + ETA thật, bỏ công đoạn tắt."""
from app.editor.stages import ProgressTracker, Stage, Status, active_stages, fmt_eta
from app.job_state import canonical_job_status, job_status_for_edit, normalize_persisted_row


def test_canonical_job_status_has_one_precedence():
    assert canonical_job_status({
        "download_status": "downloaded", "edit_status": "done",
        "job_status": Status.RENDERING,
    }) == Status.COMPLETED
    assert canonical_job_status({
        "download_status": "downloaded", "edit_status": "failed",
        "job_status": Status.PROCESSING,
    }) == Status.FAILED
    assert canonical_job_status({
        "download_status": "downloaded", "edit_status": "pending",
        "job_status": Status.PAUSED,
    }) == Status.PAUSED


def test_edit_state_synchronizes_job_state_without_losing_user_control():
    assert job_status_for_edit("downloaded", "processing", "") == Status.PREPARING
    assert job_status_for_edit("downloaded", "done", Status.RENDERING) == Status.COMPLETED
    assert job_status_for_edit("downloaded", "failed", Status.PROCESSING) == Status.FAILED
    assert job_status_for_edit("downloaded", "pending", Status.PAUSED) == Status.PAUSED


def test_normalize_persisted_row_recovers_interrupted_and_repairs_done():
    active = {"download_status": "downloaded", "edit_status": "processing",
              "job_status": Status.RENDERING, "stage": Stage.RENDERING, "progress": 80}
    assert normalize_persisted_row(active, interrupted=True)
    assert active["edit_status"] == "pending"
    assert active["job_status"] == Status.INTERRUPTED

    done = {"download_status": "downloaded", "edit_status": "done",
            "job_status": Status.PROCESSING, "stage": "", "progress": 12}
    assert normalize_persisted_row(done)
    assert done["job_status"] == Status.COMPLETED
    assert done["stage"] == Stage.COMPLETED and done["progress"] == 100


def test_active_stages_skips_disabled():
    st = active_stages(smart_crop=False, audio_sep=False, speech=False,
                       subtitle=False, translation=False)
    # luôn có Reading, Rendering, Saving
    assert st == [Stage.READING, Stage.RENDERING, Stage.SAVING]
    full = active_stages(smart_crop=True, audio_sep=True, speech=True,
                         subtitle=True, translation=True, tts=True)
    assert Stage.AUDIO in full and Stage.SPEECH in full and Stage.TRANSLATION in full


def test_overall_increases_across_stages():
    st = active_stages(smart_crop=False, audio_sep=False, speech=False,
                       subtitle=False, translation=False)  # Reading, Rendering, Saving
    t = ProgressTracker(st, start_time=0.0)
    p0 = t.overall()
    t.set_stage(Stage.RENDERING)
    t.set_stage_fraction(0.5)
    p1 = t.overall()
    t.set_stage(Stage.SAVING)
    p2 = t.overall()
    assert 0.0 <= p0 < p1 < p2 <= 1.0
    t.set_stage(Stage.COMPLETED)
    assert t.overall() == 1.0 and t.percent() == 100


def test_render_weight_dominates():
    st = active_stages(smart_crop=False, audio_sep=False, speech=False,
                       subtitle=False, translation=False)
    t = ProgressTracker(st, start_time=0.0)
    t.set_stage(Stage.RENDERING); t.set_stage_fraction(1.0)
    # rendering trọng số 48/(1+48+2)=~0.94 -> gần xong khi render hết
    assert t.overall() > 0.9


def test_eta_and_elapsed():
    st = active_stages(smart_crop=False, audio_sep=False, speech=False,
                       subtitle=False, translation=False)
    t = ProgressTracker(st, start_time=100.0)
    assert t.eta_seconds(now=100.0) is None          # chưa đủ dữ liệu
    t.set_stage(Stage.RENDERING); t.set_stage_fraction(0.5)
    # đã ~48%*... elapsed 10s
    eta = t.eta_seconds(now=110.0)
    assert eta is not None and eta > 0
    assert t.elapsed(now=110.0) == 10.0


def test_overall_robust_to_stage_not_in_active_list():
    # active = [Reading, Rendering, Saving]; pipeline lỡ phát Smart Crop (không active)
    st = active_stages(smart_crop=False, audio_sep=False, speech=False,
                       subtitle=False, translation=False)
    t = ProgressTracker(st, start_time=0.0)
    t.set_stage(Stage.SMART_CROP)     # không nằm trong active
    p = t.overall()
    assert p < 0.1                    # KHÔNG nhảy lên ~100% như bug cũ
    t.set_stage(Stage.RENDERING); t.set_stage_fraction(0.5)
    assert t.overall() > p            # vẫn tăng đúng khi vào Rendering


def test_fmt_eta():
    assert fmt_eta(None) == "--:--"
    assert fmt_eta(65) == "01:05"
    assert fmt_eta(3661) == "1:01:01"


def test_status_sets():
    assert Status.COMPLETED in Status.DONE
    assert Status.WAITING in Status.RUNNABLE and Status.FAILED in Status.RUNNABLE
    assert Status.PROCESSING in Status.ACTIVE
