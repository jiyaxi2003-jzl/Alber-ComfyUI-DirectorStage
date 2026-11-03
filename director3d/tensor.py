"""numpy <-> ComfyUI IMAGE 张量的小工具。"""
from __future__ import annotations

import numpy as np

try:
    import torch
except Exception:  # pragma: no cover
    torch = None

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def to_tensor(images: list):
    """list[RGB uint8 HxWx3] -> [B,H,W,3] float32 0..1（尺寸不一致时自动缩放）。"""
    if torch is None:
        raise RuntimeError("需要 torch 才能转换张量")
    if not images:
        return torch.zeros((1, 64, 64, 3), dtype=torch.float32)
    arrays = [np.asarray(img, dtype=np.float32) / 255.0 for img in images]
    h = max(a.shape[0] for a in arrays)
    w = max(a.shape[1] for a in arrays)
    padded = []
    for a in arrays:
        if a.shape[0] != h or a.shape[1] != w:
            if cv2 is not None:
                a = cv2.resize(a, (w, h), interpolation=cv2.INTER_LINEAR)
            else:
                from PIL import Image

                a = np.array(Image.fromarray((a * 255).astype(np.uint8)).resize((w, h))) / 255.0
        padded.append(a)
    return torch.from_numpy(np.stack(padded, axis=0)).float()


def to_numpy(images) -> list:
    """[B,H,W,3] 0..1 -> list[uint8 HxWx3]。"""
    if images is None:
        return []
    if torch is not None and torch.is_tensor(images):
        arr = images.detach().cpu().float().numpy()
    else:
        arr = np.asarray(images, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[None]
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    return [np.clip(a * 255.0, 0, 255).astype(np.uint8) for a in arr]
