from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Sequence

@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)

def format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'
    return f'{minutes:02d}:{secs:02d}'

def segments_to_prompt(segments: Sequence[TranscriptSegment]) -> str:
    return '\n'.join((f'[{format_timestamp(segment.start)}-{format_timestamp(segment.end)}] {segment.text}' for segment in segments))

def format_srt_timestamp(seconds: float) -> str:
    """SubRip 时间码 ``HH:MM:SS,mmm``：毫秒三位补零，小时从 00 起。

    先换算成整数毫秒再拆位，999.7 秒这种小数才不会写出 ``00:16:39,1000``。
    """
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'

def segments_to_srt(segments: Sequence[TranscriptSegment]) -> str:
    """渲染成 SubRip 字幕文本，供 ``/api/task/{id}/transcript?output_format=srt`` 使用。

    条目与传进来的 ``segments`` 一一对应、**不并段**：SRT 与 ``output_format=json`` 要能
    逐条核对（Issue #4 里承诺的契约）。碎行合并是提示词那侧的事（见
    :func:`merge_segment_lines`），字幕要的是和播放器里对得上的原始粒度。
    行尾用 CRLF：这是 SubRip 的通行写法，剪映 / Premiere 对纯 LF 的容忍度不一。
    """
    blocks = ['{}\r\n{} --> {}\r\n{}\r\n'.format(index, format_srt_timestamp(segment.start), format_srt_timestamp(segment.end), segment.text) for index, segment in enumerate(segments, start=1)]
    return '\r\n'.join(blocks)
