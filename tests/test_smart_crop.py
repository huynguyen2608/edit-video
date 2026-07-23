from app.editor.smart_crop import smooth_focus_points


def test_smooth_focus_rejects_distant_outlier():
    x, y = smooth_focus_points([(0.48, 0.5), (0.5, 0.51), (0.99, 0.02), (0.52, 0.5)])
    assert 0.45 <= x <= 0.57
    assert 0.45 <= y <= 0.57


def test_smooth_focus_empty_is_center():
    assert smooth_focus_points([]) == (0.5, 0.5)
