"""单目深度估计。

后端优先级（auto）：
  1. ``input``       —— 用户在外部接进来的深度图批次（最准，配合自己的深度节点）
  2. ``model``       —— Depth-Anything-V2（transformers，本地 models/depth_anything 优先，否则自动下载）
  3. ``ground``      —— 地平线假设的几何近似，纯兜底，不依赖任何模型

统一输出：float32 ``[0,1]``，**0 = 最近**，1 = 最远（即归一化视差的反向）。
"""
from __future__ import annotations

import os

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

from .paths import MODELS_DIR

LOCAL_MODEL_DIRS = [
    os.path.join(MODELS_DIR, "depth_anything"),
    os.path.join(MODELS_DIR, "depth-anything"),
]
HF_CANDIDATES = [
    "depth-anything/Depth-Anything-V2-Small-hf",
    "depth-anything/Depth-Anything-V2-Base-hf",
]

_CACHE: dict = {"model": None, "processor": None, "key": None, "failed": None}


def _to_norm(raw: np.ndarray) -> np.ndarray:
    """任意尺度的相对深度 -> [0,1]，0 最近。"""
    raw = np.asarray(raw, dtype=np.float32)
    lo, hi = float(np.percentile(raw, 1.0)), float(np.percentile(raw, 99.0))
    if hi - lo < 1e-6:
        hi = lo + 1e-6
    norm = np.clip((raw - lo) / (hi - lo), 0.0, 1.0)
    return 1.0 - norm  # 深度越大越远


def ground_heuristic(frame: np.ndarray, horizon: float = 0.45) -> np.ndarray:
    """地平线假设：越靠下越近、越靠上越远，带一点中心向外的径向偏移。"""
    h, w = frame.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ny = yy / max(h - 1, 1)
    nx = (xx / max(w - 1, 1)) - 0.5
    depth = 0.55 + (ny - horizon) * 1.6 + np.abs(nx) * 0.12
    return np.clip(depth, 0.0, 1.0)


def _load_model(device: str | None = None):
    if _CACHE["model"] is not None:
        return _CACHE["model"], _CACHE["processor"]
    if _CACHE["failed"]:
        raise RuntimeError(_CACHE["failed"])

    import torch
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    sources = [d for d in LOCAL_MODEL_DIRS if os.path.isdir(d) and os.listdir(d)] + HF_CANDIDATES
    last_err = None
    for src in sources:
        try:
            processor = AutoImageProcessor.from_pretrained(src)
            model = AutoModelForDepthEstimation.from_pretrained(src)
            if device:
                model = model.to(device)
            model.eval()
            _CACHE.update(model=model, processor=processor, key=src)
            return model, processor
        except Exception as exc:  # 逐个候选重试
            last_err = exc
            continue
    _CACHE["failed"] = f"深度模型加载失败：{last_err}"
    raise RuntimeError(_CACHE["failed"])


def model_name() -> str:
    return str(_CACHE.get("key") or "")


def flow_depth(frames: list, ref_index: int = 0) -> tuple[list, str]:
    """运动视差深度（不依赖任何模型/下载）。

    思路：先用特征点把相邻帧之间的全局旋转/缩放消掉，剩下的残余光流就是视差，
    |残余光流| 与 1/深度 近似成正比。相机以横移/前后移动为主时效果最好；
    纯旋转镜头会退化（此时自动改用几何近似）。
    """
    n = len(frames)
    if n < 2:
        return [ground_heuristic(f) for f in frames], "几何近似（单帧）"
    ref = frames[ref_index]
    h, w = ref.shape[:2]
    small = 320
    scale = small / float(w)
    ref_small = cv2.resize(ref, (small, max(2, int(h * scale))))
    gray_ref = cv2.cvtColor(ref_small, cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(1200)
    kp_ref, des_ref = orb.detectAndCompute(gray_ref, None)

    out = [None] * n
    out[ref_index] = ground_heuristic(ref)

    for i, frame in enumerate(frames):
        if i == ref_index:
            continue
        tgt = cv2.resize(frame, (gray_ref.shape[1], gray_ref.shape[0]))
        gray_t = cv2.cvtColor(tgt, cv2.COLOR_RGB2GRAY)
        kp_t, des_t = orb.detectAndCompute(gray_t, None)
        dip = None
        if des_ref is not None and des_t is not None and len(kp_t) > 24:
            bf = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(des_ref, des_t, k=2)
            good = [(m, n2) for m, n2 in (p for p in bf if len(p) == 2) if m.distance < 0.8 * n2.distance]
            if len(good) >= 12:
                src = np.float32([kp_ref[m.queryIdx].pt for m, _ in good])
                dst = np.float32([kp_t[m.trainIdx].pt for m, _ in good])
                aff, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                           ransacReprojThreshold=3.0)
                if aff is not None:
                    warped = cv2.warpAffine(gray_t, aff, (gray_ref.shape[1], gray_ref.shape[0]))
                    flow = cv2.calcOpticalFlowFarneback(gray_ref, warped, None, 0.5, 4, 21, 3, 5, 1.2, 0)
                    mag = np.linalg.norm(flow, axis=2)
                    mag = cv2.GaussianBlur(mag, (0, 0), sigmaX=3)
                    raw = cv2.GaussianBlur(1.0 / (mag + 0.02), (0, 0), sigmaX=2)
                    dip = _to_norm(raw)
        out[i] = dip if dip is not None else ground_heuristic(frame)

    filled = [d if d is not None else ground_heuristic(f) for d, f in zip(out, frames)]
    resized = []
    for d, f in zip(filled, frames):
        if d.shape[:2] != f.shape[:2]:
            d = cv2.resize(d, (f.shape[1], f.shape[0]), interpolation=cv2.INTER_LINEAR)
        resized.append(d.astype(np.float32))
    return resized, "运动视差（无需模型）"

def estimate(frames: list, backend: str = "auto", device: str | None = None,
             depth_inputs: list | None = None) -> tuple[list, str]:
    """返回 (dip 列表, 实际使用的后端说明)。"""
    if not frames:
        return [], "empty"

    if depth_inputs:
        out = []
        for i, frame in enumerate(frames):
            d = np.asarray(depth_inputs[min(i, len(depth_inputs) - 1)], dtype=np.float32)
            d = np.squeeze(d)
            if d.max() > 1.0:
                d = d / 255.0
            if d.shape[:2] != frame.shape[:2]:
                import cv2

                d = cv2.resize(d, (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_LINEAR)
            out.append(_to_norm(d))
        return out, "外部深度图"

    if backend in ("auto", "model"):
        try:
            model, processor = _load_model(device)
            import torch
            import torch.nn.functional as F

            out = []
            for frame in frames:
                inputs = processor(images=frame, return_tensors="pt")
                if device:
                    inputs = {k: v.to(device) for k, v in inputs.items() if hasattr(v, "to")}
                with torch.inference_mode():
                    pred = model(**inputs).predicted_depth
                pred = F.interpolate(pred.unsqueeze(1), size=frame.shape[:2], mode="bicubic",
                                     align_corners=False).squeeze()
                out.append(_to_norm(pred.detach().float().cpu().numpy()))
            return out, f"Depth-Anything（{os.path.basename(_CACHE.get('key') or '')}）"
        except Exception as exc:
            if backend == "model":
                raise RuntimeError(
                    f"深度模型不可用：{exc}\n"
                    "离线环境建议：把已有的深度节点输出接到本节点的 depth_in，"
                    "或把 depth_backend 改成 flow（无需下载模型）。")
            note = f"深度模型不可用（{type(exc).__name__}），改用运动视差"
            if backend == "auto" and len(frames) > 1:
                return flow_depth(frames)
            return [ground_heuristic(f) for f in frames], note

    if backend == "flow" and len(frames) > 1:
        return flow_depth(frames)
    return [ground_heuristic(f) for f in frames], "几何近似（地平线假设）"


def save_depth_preview(dip: np.ndarray) -> np.ndarray:
    """0=近 1=远 -> 灰度图（近亮远暗，符合常规深度图习惯）。"""
    gray = (1.0 - np.clip(dip, 0, 1)) * 255.0
    return np.repeat(gray[:, :, None].astype(np.uint8), 3, axis=2)
