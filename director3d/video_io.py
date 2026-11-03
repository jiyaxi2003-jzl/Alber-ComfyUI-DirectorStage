"""视频读取：优先 PyAV，退回 OpenCV。只需要"均匀抽帧 + 时长/帧率"这点能力。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

try:
    import av

    HAS_AV = True
except Exception:  # pragma: no cover
    HAS_AV = False
    av = None

try:
    import cv2

    HAS_CV2 = True
except Exception:  # pragma: no cover
    HAS_CV2 = False
    cv2 = None


@dataclass
class VideoInfo:
    path: str = ""
    width: int = 0
    height: int = 0
    fps: float = 24.0
    duration: float = 0.0
    n_frames: int = 0
    warnings: list = field(default_factory=list)


def probe(path: str) -> VideoInfo:
    info = VideoInfo(path=path)
    if not os.path.exists(path):
        info.warnings.append(f"找不到视频文件：{path}")
        return info
    if HAS_AV:
        try:
            with av.open(path) as container:
                stream = container.streams.video[0]
                info.width = int(stream.codec_context.width)
                info.height = int(stream.codec_context.height)
                fps = stream.average_rate or stream.guessed_rate or 24
                info.fps = float(fps) if fps else 24.0
                if container.duration:
                    info.duration = container.duration / 1_000_000.0
                elif stream.duration and stream.time_base:
                    info.duration = float(stream.duration * stream.time_base)
                info.n_frames = int(round(info.duration * info.fps)) if info.duration else 0
                return info
        except Exception as exc:
            info.warnings.append(f"PyAV 探测失败：{exc}")
    if HAS_CV2:
        cap = cv2.VideoCapture(path)
        info.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        info.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        info.fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
        info.n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        info.duration = info.n_frames / info.fps if info.fps else 0.0
        cap.release()
    return info


def _resize(frame: np.ndarray, long_side: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if not long_side or max(h, w) <= long_side:
        return frame
    scale = long_side / float(max(h, w))
    size = (max(2, int(round(w * scale))), max(2, int(round(h * scale))))
    if HAS_CV2:
        return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    from PIL import Image

    return np.array(Image.fromarray(frame).resize(size, Image.LANCZOS))


def read_frames(path: str, max_frames: int = 24, long_side: int = 960, start: float = 0.0,
                end: float | None = None) -> tuple[list, VideoInfo]:
    """均匀抽 max_frames 帧，返回 RGB uint8 列表。"""
    info = probe(path)
    if not os.path.exists(path):
        return [], info
    frames: list = []
    max_frames = max(2, int(max_frames))

    if HAS_AV:
        try:
            with av.open(path) as container:
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                t_end = end if end and end > 0 else (info.duration or 0.0)
                stride = max(1, int(round((t_end - start) * info.fps / (max_frames * 1.6)))) if t_end else 1
                idx = 0
                for frame in container.decode(stream):
                    t = float(frame.pts * stream.time_base) if frame.pts is not None else idx / max(info.fps, 1e-3)
                    idx += 1
                    if t < start:
                        continue
                    if end and end > 0 and t > end:
                        break
                    if (idx - 1) % stride:
                        continue
                    frames.append(_resize(frame.to_ndarray(format="rgb24"), long_side))
                    if len(frames) >= max_frames:
                        break
        except Exception as exc:
            info.warnings.append(f"PyAV 解码失败：{exc}")

    if not frames and HAS_CV2:
        cap = cv2.VideoCapture(path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        stride = max(1, total // (max_frames * 2)) if total else 1
        idx = 0
        while len(frames) < max_frames:
            ok, bgr = cap.read()
            if not ok:
                break
            if idx % stride == 0:
                frames.append(_resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), long_side))
            idx += 1
        cap.release()

    if not frames:
        info.warnings.append("未能解码出任何帧，请检查编码格式（推荐 H.264 MP4）。")
        return [], info

    info.width, info.height = int(frames[0].shape[1]), int(frames[0].shape[0])
    if not info.n_frames:
        info.n_frames = len(frames)
    if not info.duration:
        info.duration = len(frames) / max(info.fps, 1e-3)
    return frames, info


def detect_cuts(frames: list, threshold: float = 0.42) -> list[int]:
    """基于 HSV 直方图距离的镜头切换检测，返回切点索引（不含 0）。"""
    if len(frames) < 3 or not HAS_CV2:
        return []
    hists = []
    for frame in frames:
        small = cv2.resize(frame, (96, 54))
        hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        hists.append(cv2.normalize(hist, hist).flatten())
    cuts = []
    for i in range(1, len(hists)):
        dist = cv2.compareHist(hists[i - 1].astype(np.float32), hists[i].astype(np.float32),
                               cv2.HISTCMP_BHATTACHARYYA)
        if dist > threshold:
            cuts.append(i)
    return cuts
