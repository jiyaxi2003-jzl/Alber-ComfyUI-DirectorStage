"""视频 -> 导演台场景（2.5D 分层 + 相机轨迹 + 主体替身）。"""
from __future__ import annotations

import os

import numpy as np

try:
    import cv2

    HAS_CV2 = True
except Exception:  # pragma: no cover
    HAS_CV2 = False
    cv2 = None

from . import track
from .paths import ASSET_ROOT, OUTPUT_DIR, scene_dir
from .scene import (ASPECT_PRESETS, card_size, depth_to_dip, dip_to_depth,
                    lens_to_vfov, matrix_to_euler_yxz, new_scene)

# 层数 -> 深度分带边界（视差从近到远，按经验不均匀分布）
BAND_EDGES = {
    1: [0.0, 1.0],
    2: [0.0, 0.62, 1.0],
    3: [0.0, 0.44, 0.74, 1.0],
    4: [0.0, 0.30, 0.52, 0.75, 1.0],
}


def _detect_subject(dip_ref: np.ndarray, W: int, H: int, near_quantile: float = 0.24):
    """在最近的一段深度里找一块"像人"的连通域，返回 bbox（像素，左闭右开）。

    单张深度图的分布很不稳定，所以按 10% / 18% / 26% 三档由近及远尝试，
    谁的候选最先通过"紧凑 + 竖直"的检查就用谁。
    """
    valid = dip_ref[np.isfinite(dip_ref)]
    if not valid.size:
        return None
    dmin = float(np.min(valid))
    for delta in (0.06, 0.12, 0.20):
        thr = dmin + delta
        fg = (dip_ref <= thr).astype(np.uint8) * 255
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        # 竖向开运算：压掉"又宽又扁"的地面/水面块，留下竖直的人形
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((max(9, H // 26), 5), np.uint8))
        fg = cv2.dilate(fg, np.ones((7, 7), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
        best = None
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if area < W * H * 0.004:
                continue
            if w > W * 0.62 or h < H * 0.14:     # 横跨大半幅的通常是地面/背景，不是人
                continue
            ratio = h / max(w, 1)
            if not (1.15 < ratio < 4.5):
                continue
            if y < H * 0.03 or (y + h) < H * 0.55 or (y + h) > H * 0.995:
                continue  # 站立的主体应该落在大半幅面里，且不被下边缘截断
            score = area * min(ratio, 3.0)
            if best is None or score > best[0]:
                best = (score, (int(x), int(y), int(x + w), int(y + h)))
        if best:
            return best[1]
    return None


def _save_rgba(path: str, rgb: np.ndarray, alpha: np.ndarray):
    bgra = np.dstack([rgb[:, :, ::-1], alpha])
    cv2.imwrite(path, np.ascontiguousarray(bgra))


def _camera_pose(camera: dict):
    """返回 (eye, R_cam_to_world)。"""
    from .scene import look_at

    eye = np.asarray(camera["position"], dtype=np.float64)
    view = look_at(eye, camera["target"])
    R = view[:3, :3].T  # world -> cam 的旋转转置即 cam -> world
    return eye, R


def _pixel_ray(u: float, v: float, W: int, H: int, vfov: float, R, eye, depth: float):
    f = 0.5 * H / np.tan(np.radians(vfov) / 2.0)
    x_c = (u - W / 2.0) * depth / f
    y_c = -(v - H / 2.0) * depth / f
    return eye + R @ np.array([x_c, y_c, -depth])


def reconstruct(frames: list, info, dips: list, depth_note: str, *,
                scene_id: str, lens_mm: float = 35.0, near: float = 1.5, far: float = 26.0,
                layers: int = 3, ref_index: int = 0, include_ground: bool = True,
                add_subject: bool = True, aspect: str = "auto",
                path_override: list | None = None) -> tuple[dict, dict]:
    if not frames:
        raise ValueError("没有可用帧，无法重建场景")

    H, W = frames[0].shape[:2]
    aspect_value = ASPECT_PRESETS.get(aspect, W / H) if aspect != "auto" else W / H
    report_aspect = aspect_value
    vfov = lens_to_vfov(lens_mm)

    ref_index = int(min(max(ref_index, 0), len(frames) - 1))
    ref_frame = frames[ref_index]
    dip_ref = np.asarray(dips[ref_index], dtype=np.float32)
    dip_ref = cv2.resize(dip_ref, (W, H), interpolation=cv2.INTER_LINEAR) if dip_ref.shape[:2] != (H, W) else dip_ref

    path, track_info = track.estimate_camera_path(frames, dips, vfov, near, far)
    if path_override:
        path = path_override
    ref_cam = {**path[ref_index]} if path else {"position": [0.0, 1.5, far * 1.2], "target": [0.0, 1.4, 0.0]}
    ref_cam.setdefault("target", [0.0, 1.4, 0.0])
    eye, R_cam = _camera_pose(ref_cam)

    out_dir = scene_dir(scene_id)
    subfolder = f"{ASSET_ROOT}/{scene_id}"
    edges = BAND_EDGES.get(int(np.clip(layers, 1, 4)), BAND_EDGES[3])

    objects = []
    valid = dip_ref[np.isfinite(dip_ref)]
    qs = np.quantile(valid, edges) if valid.size else np.linspace(0, 1, len(edges))

    for j in range(len(edges) - 1):
        lo, hi = float(qs[j]), float(qs[j + 1])
        band = (dip_ref >= lo) & (dip_ref < hi if j < len(edges) - 2 else dip_ref <= hi)
        if band.sum() < (W * H) * 0.004:
            continue
        is_far = (j == len(edges) - 2)
        if is_far:
            # 最远一层做成不透底的"背景板"，既保证画面完整，也盖住层与层之间的接缝
            alpha = np.full((H, W), 255.0, dtype=np.float32)
        else:
            alpha = (band.astype(np.float32) * 255.0)
            alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=max(1.4, W / 420.0))
        name = f"card_{j}.png"
        _save_rgba(os.path.join(out_dir, name), ref_frame, alpha.astype(np.uint8))

        centre_dip = float((lo + hi) * 0.5)
        depth = dip_to_depth(centre_dip, near, far)
        world_w, world_h = card_size(depth, vfov, report_aspect)
        pos = eye + R_cam @ np.array([0.0, 0.0, -depth])
        objects.append({
            "id": f"card_{j}",
            "kind": "card",
            "label": f"深度层 {j + 1}（{'近' if j == 0 else '远' if j == len(edges) - 2 else '中'}）",
            "image": name,
            "subfolder": subfolder,
            "depth": round(depth, 3),
            "position": [round(float(v), 3) for v in pos],
            "rotation": [round(float(v), 2) for v in matrix_to_euler_yxz(R_cam)],
            "scale": [world_w / W, world_h / H, 1.0],
            "opacity": 1.0,
        })

    if include_ground:
        objects.append({
            "id": "ground",
            "kind": "plane",
            "label": "地面",
            "size": max(24.0, far * 1.2),
            "position": [round(float(eye[0]), 2), 0.0, round(float(eye[2] - far * 0.4), 2)],
            "color": [58, 62, 72],
        })

    subject_box = None
    if add_subject:
        subject_box = _detect_subject(dip_ref, W, H)
        if subject_box:
            x0, y0, x1, y1 = subject_box
            cx = (x0 + x1) / 2.0
            depth = dip_to_depth(float(np.median(dip_ref[y0:y1, x0:x1])), near, far)
            f_px = 0.5 * H / np.tan(np.radians(vfov) / 2.0)
            man_h = float((y1 - y0) * depth / max(f_px, 1e-3) * 1.02)
            if not (1.25 <= man_h <= 2.05):
                man_h = 1.75  # 估算明显不合理时退回成年人均值，而不是硬塞一个 1m 的小人
            base = _pixel_ray(cx, y1, W, H, vfov, R_cam, eye, depth)
            base[1] = 0.0
            yaw = np.degrees(np.arctan2(eye[0] - base[0], eye[2] - base[2]))
            objects.append({
                "id": "subject",
                "kind": "mannequin",
                "label": "主体替身",
                "position": [round(float(v), 3) for v in base],
                "rotation": [0.0, round(float(yaw), 2), 0.0],
                "height": round(man_h, 2),
                "pose": "站立",
                "opacity": 1.0,
            })
            crop = ref_frame[max(y0, 0):y1, max(x0, 0):x1]
            if crop.size and crop.shape[0] > 16 and crop.shape[1] > 16:
                cv2.imwrite(os.path.join(out_dir, "subject_ref.png"), crop[:, :, ::-1])

    cv2.imwrite(os.path.join(out_dir, "ref_frame.png"), ref_frame[:, :, ::-1])
    for i in (0, len(frames) // 2, len(frames) - 1):
        cv2.imwrite(os.path.join(out_dir, f"keyframe_{i}.png"), frames[i][:, :, ::-1])

    scene = new_scene(W, H, info.fps)
    scene["camera"] = {
        "position": [round(float(v), 3) for v in eye],
        "target": [round(float(v), 3) for v in ref_cam.get("target", [0, 1.4, 0])],
        "lens_mm": float(lens_mm),
        "vfov": vfov,
        "near": 0.05,
        "far": max(far * 4.0, 200.0),
        "path": path,
    }
    scene["objects"] = objects
    scene["meta"] = {
        "scene_id": scene_id,
        "asset_subfolder": subfolder,
        "source": os.path.basename(getattr(info, "path", "") or ""),
        "frames_used": len(frames),
        "depth_backend": depth_note,
        "camera_track": track_info,
        "motion": track.describe_motion(path),
        "depth_range": [near, far],
        "subject_box": subject_box,
        "aspect": round(W / H, 4),
        "notes": [],
        "ref_frame": "ref_frame.png",
        "subject_ref": "subject_ref.png" if subject_box else "",
    }
    if depth_note and "近似" in depth_note:
        scene["meta"]["notes"].append("深度来自几何近似，视差结构仅供构图参考")
    return scene, {"path": path, "track": track_info, "depth_note": depth_note,
                   "cut_count": len(getattr(info, "warnings", []))}
