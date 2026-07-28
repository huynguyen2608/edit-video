"""Business rules for the list of monitored YouTube sources."""
from __future__ import annotations

from dataclasses import dataclass

from .config import ChannelCfg


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceChange:
    channel: ChannelCfg
    reference: str


class SourceController:
    def __init__(self, channels: list[ChannelCfg]):
        self.channels = channels

    @staticmethod
    def valid_reference(reference: str) -> bool:
        ref = reference.strip()
        low = ref.lower()
        return (
            (ref.startswith("UC") and len(ref) == 24 and " " not in ref)
            or (ref.startswith("@") and len(ref) > 1 and " " not in ref)
            or (
                low.startswith(("http://", "https://"))
                and ("youtube.com/" in low or "youtu.be/" in low)
            )
        )

    @staticmethod
    def make_channel(name: str, reference: str) -> ChannelCfg:
        ref = reference.strip()
        if not ref:
            raise SourceError("missing")
        if not SourceController.valid_reference(ref):
            raise SourceError("invalid")
        is_id = ref.startswith("UC") and len(ref) == 24 and " " not in ref
        return ChannelCfg(
            name=name.strip() or ref,
            url="" if is_id else ref,
            channel_id=ref if is_id else "",
        )

    def add(self, name: str, reference: str) -> SourceChange:
        channel = self.make_channel(name, reference)
        ref = channel.channel_id or channel.url
        if any((item.channel_id or item.url) == ref for item in self.channels):
            raise SourceError("duplicate")
        self.channels.append(channel)
        return SourceChange(channel, ref)

    def update(self, index: int, name: str, reference: str) -> SourceChange:
        if not 0 <= index < len(self.channels):
            raise SourceError("selection")
        channel = self.make_channel(name, reference)
        ref = channel.channel_id or channel.url
        if any(
            idx != index and (item.channel_id or item.url) == ref
            for idx, item in enumerate(self.channels)
        ):
            raise SourceError("duplicate")
        self.channels[index] = channel
        return SourceChange(channel, ref)

    def remove(self, index: int) -> ChannelCfg:
        if not 0 <= index < len(self.channels):
            raise SourceError("selection")
        return self.channels.pop(index)

    def apply_resolved_id(self, reference: str, channel_id: str) -> int:
        for index, channel in enumerate(self.channels):
            if reference in (channel.url, channel.channel_id):
                channel.channel_id = channel_id
                return index
        return -1
