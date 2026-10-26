from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class JobOptions(BaseModel):
    # Super-resolution
    sr_engine: Literal["off", "fast", "ai"] = "fast"
    ai_model: Literal["general", "anime", "fast"] = "general"
    scale: float = Field(default=2.0, ge=1.0, le=8.0)
    target_height: int | None = Field(default=None, ge=120, le=4320)

    # Frame interpolation
    interp: Literal["off", "smooth", "balanced", "fast"] = "off"
    fps_mode: Literal["multiple", "fixed"] = "multiple"
    fps_value: float = Field(default=2.0, gt=0.0, le=240.0)
    order: Literal["interp_first", "sr_first"] = "interp_first"

    # Restoration
    denoise: Literal["off", "light", "medium", "strong"] = "off"
    sharpen: float = Field(default=0.35, ge=0.0, le=1.5)

    # Encoding
    codec: Literal["h264", "h265", "av1"] = "h264"
    encoder: str | None = None
    quality: Literal["high", "balanced", "small"] = "high"
    crf: int | None = Field(default=None, ge=0, le=63)
    preset: Literal["slow", "medium", "fast"] = "medium"
    container: Literal["mp4", "mkv"] = "mp4"
    audio: Literal["copy", "aac", "none"] = "copy"

    # Range / output
    start: float = Field(default=0.0, ge=0.0)
    end: float | None = Field(default=None, gt=0.0)
    output_dir: str | None = None
    output_name: str | None = None

    @field_validator("encoder")
    @classmethod
    def clean_encoder(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("output_name")
    @classmethod
    def clean_output_name(cls, value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.strip().replace("/", "_").replace("\\", "_")
        for char in '<>:"|?*':
            cleaned = cleaned.replace(char, "_")
        return cleaned[:160] or None

    @model_validator(mode="after")
    def check_range(self) -> "JobOptions":
        if self.end is not None and self.end <= self.start:
            raise ValueError("结束时间必须大于开始时间")
        if self.sr_engine == "off" and self.interp == "off":
            raise ValueError("请至少启用「超分」或「补帧」中的一项")
        if self.interp != "off" and self.fps_mode == "fixed" and self.fps_value < 12:
            raise ValueError("目标帧率不能低于 12 fps")
        return self


class JobCreate(BaseModel):
    source: str
    options: JobOptions = Field(default_factory=JobOptions)
    preview: bool = False


class PreviewRequest(BaseModel):
    path: str
    time: float = Field(default=1.0, ge=0.0)
    options: JobOptions = Field(default_factory=JobOptions)


class OpenPathRequest(BaseModel):
    path: str


class ProbeRequest(BaseModel):
    path: str


class TargetInfo(BaseModel):
    width: int
    height: int
    source_width: int
    source_height: int
    fps: float
    source_fps: float
    scale_x: float
    scale_y: float
    duration: float
    start: float
    end: float | None


def resolve_target(probe: dict, options: JobOptions) -> dict:
    source_width = int(probe.get("width") or 0)
    source_height = int(probe.get("height") or 0)
    source_fps = float(probe.get("fps") or 0.0)
    duration = float(probe.get("duration") or 0.0)
    if source_width <= 0 or source_height <= 0:
        raise ValueError("无法读取视频分辨率")

    if options.target_height:
        target_height = int(options.target_height)
        target_width = int(round(target_height * source_width / source_height))
    elif options.sr_engine == "off":
        target_width = source_width
        target_height = source_height
    else:
        target_width = int(round(source_width * options.scale))
        target_height = int(round(source_height * options.scale))

    target_width = max(2, target_width // 2 * 2)
    target_height = max(2, target_height // 2 * 2)
    if options.sr_engine == "off":
        target_width = source_width
        target_height = source_height
    if target_width > 7680 or target_height > 4320:
        raise ValueError("目标分辨率超过 8K 上限，请降低倍率")
    if target_width == source_width and target_height == source_height and options.interp == "off":
        raise ValueError("超分倍率为 1× 且未开启补帧，不会有任何变化")

    if options.interp == "off":
        target_fps = source_fps
    elif options.fps_mode == "multiple":
        target_fps = source_fps * options.fps_value
    else:
        target_fps = options.fps_value

    if options.interp != "off" and target_fps <= source_fps + 0.01:
        raise ValueError(
            f"目标帧率 {target_fps:.2f} 必须高于源帧率 {source_fps:.2f}；"
            "如需保持帧率，请关闭补帧"
        )

    if options.interp != "off" and target_fps > 240.5:
        raise ValueError("目标帧率不能超过 240 fps")

    start = max(0.0, float(options.start))
    end = float(options.end) if options.end is not None else None
    if duration > 0:
        if start >= duration:
            raise ValueError("开始时间超出视频时长")
        if end is not None and end > duration + 0.05:
            end = duration
    if end is not None and end <= start + 0.02:
        raise ValueError("处理区间太短，请调整开始/结束时间")

    return {
        "width": target_width,
        "height": target_height,
        "source_width": source_width,
        "source_height": source_height,
        "fps": round(target_fps, 4) if target_fps else 0.0,
        "source_fps": round(source_fps, 4),
        "scale_x": round(target_width / source_width, 4),
        "scale_y": round(target_height / source_height, 4),
        "duration": round(duration, 3),
        "start": round(start, 3),
        "end": round(end, 3) if end is not None else None,
    }
