from app.config import ChannelCfg
from app.source_controller import SourceController, SourceError


def test_source_reference_validation():
    assert SourceController.valid_reference("@valid_handle")
    assert SourceController.valid_reference("https://www.youtube.com/@valid")
    assert SourceController.valid_reference("UC" + "a" * 22)
    assert not SourceController.valid_reference("aaa")


def test_source_add_update_remove_and_duplicate_protection():
    channels = []
    controller = SourceController(channels)
    added = controller.add("Kênh", "@channel")
    assert added.channel.name == "Kênh"
    assert channels[0].url == "@channel"

    try:
        controller.add("Trùng", "@channel")
        assert False, "duplicate source must be rejected"
    except SourceError as exc:
        assert str(exc) == "duplicate"

    controller.update(0, "Tên mới", "UC" + "b" * 22)
    assert channels[0].name == "Tên mới"
    assert channels[0].url == "" and channels[0].channel_id.startswith("UC")
    assert controller.remove(0).name == "Tên mới"
    assert channels == []


def test_source_applies_resolved_channel_id():
    channels = [ChannelCfg(name="Kênh", url="@channel")]
    controller = SourceController(channels)
    assert controller.apply_resolved_id("@channel", "UC" + "c" * 22) == 0
    assert channels[0].channel_id == "UC" + "c" * 22
