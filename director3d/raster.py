"""纯 CPU 光栅化渲染器（不依赖 OpenGL / 无头浏览器）。

够用的原因：场景本质是"几张按深度排好的照片 + 几个替身"，用画家算法逐层合成即可。
输出：预演图 / 深度图 / 法线图 / 线稿 / 主体蒙版 / OpenPose 骨骼。
"""
from __future__ import annotations

import math
import os

import numpy as np

from . import pose as pose_lib
from .paths import OUTPUT_DIR
from .scene import (card_size, dip_to_depth, make_vp, model_matrix, objects_by_kind,
                    project_points)

try:
    import cv2

    HAS_CV2 = True
except Exception:  # pragma: no cover
    HAS_CV2 = False
    cv2 = None

BACKDROP_TOP = np.array([38, 42, 52], dtype=np.float32)
BACKDROP_BOTTOM = np.array([16, 17, 20], dtype=np.float32)


# --------------------------------------------------------------------------- #
# 资源加载
# --------------------------------------------------------------------------- #
def asset_path(obj: dict, scene: dict) -> str:
    image = obj.get("image")
    if not image:
        return ""
    if os.path.isabs(image) and os.path.exists(image):
        return image
    sub = obj.get("subfolder") or scene.get("meta", {}).get("asset_subfolder") or ""
    return os.path.join(OUTPUT_DIR, sub, image)


def _load_rgba(path: str):
    """读成 RGBA uint8（与整条管线的 RGB 顺序一致）；没有 alpha 就补 255。"""
    if not path or not os.path.exists(path):
        return None
    if HAS_CV2:
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGBA)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGBA)
        else:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
        return img
    from PIL import Image

    return np.array(Image.open(path).convert("RGBA"))


class Renderer:
    def __init__(self, scene: dict):
        self.scene = scene
        self._cache: dict = {}

    def texture(self, obj: dict):
        path = asset_path(obj, self.scene)
        if path not in self._cache:
            self._cache[path] = _load_rgba(path)
        return self._cache[path]

    # ------------------------------------------------------------------ #
    def render(self, camera: dict | None = None, width: int | None = None,
               height: int | None = None, with_pose: bool = True) -> dict:
        scene = self.scene
        camera = camera or scene["camera"]
        W, H = int(width or scene["frame_size"][0]), int(height or scene["frame_size"][1])
        vp = make_vp(camera, W, H)
        cam_pos = np.array(camera["position"], dtype=np.float64)

        color = _backdrop(W, H)
        depth_buf = np.full((H, W), np.nan, dtype=np.float32)
        mask_buf = np.zeros((H, W), dtype=np.uint8)
        pose_canvas = np.zeros((H, W, 3), dtype=np.uint8) if with_pose else None

        grounds = objects_by_kind(scene, "plane")
        cards = objects_by_kind(scene, "card")
        figures = objects_by_kind(scene, "mannequin")
        boxes = objects_by_kind(scene, "box")

        # 1) 地面先画（它永远在最背后）
        for obj in grounds:
            color, depth_buf = self._draw_plane(obj, vp, color, depth_buf, W, H, cam_pos)

        # 2) 其余对象按深度从远到近
        entries = []
        for obj in cards + boxes:
            dist = _view_depth(vp, obj, cam_pos)
            entries.append((dist, "quad", obj))
        for obj in figures:
            dist = float(np.linalg.norm(np.array(obj["position"]) - cam_pos))
            # 替身是构图用的"引导层"：即使站在最近那张卡片后面 0.3m 也让它压在上面
            bias = float(obj.get("sort_bias", 0.35))
            entries.append((dist - bias, "figure", obj))
        entries.sort(key=lambda e: -e[0])

        for dist, kind, obj in entries:
            if kind == "quad":
                color, depth_buf = self._draw_card(obj, vp, color, depth_buf, W, H)
            else:
                color, depth_buf, mask_buf, pose_canvas = self._draw_figure(
                    obj, vp, color, depth_buf, mask_buf, pose_canvas, W, H)

        out = {
            "previs": color,
            "depth": _depth_to_rgb(depth_buf, camera),
            "normal": _normals_to_rgb(depth_buf),
            "lineart": _lineart(color),
            "mask": np.repeat(mask_buf[:, :, None], 3, axis=2),
            "alpha": np.repeat(np.clip(np.nan_to_num(depth_buf, nan=0.0) > 0, 0, 1)[:, :, None] * 255,
                               3, axis=2).astype(np.uint8),
        }
        out["pose"] = pose_canvas if pose_canvas is not None else np.zeros((H, W, 3), np.uint8)
        return {k: np.ascontiguousarray(v.astype(np.uint8)) for k, v in out.items()}

    # ------------------------------------------------------------------ #
    def _draw_card(self, obj, vp, color, depth_buf, W, H):
        tex = self.texture(obj)
        if tex is None:
            return color, depth_buf
        th, tw = tex.shape[:2]
        scale = obj.get("scale", [1, 1, 1])
        sx = float(scale[0]) if np.ndim(scale) else float(scale)
        sy = float(scale[1]) if np.ndim(scale) and len(scale) > 1 else sx
        hw, hh = tw * 0.5, th * 0.5  # 世界尺寸由 model_matrix 里的 scale 决定

        m = model_matrix(obj)
        corners_local = np.array([[-hw, hh, 0], [-hw, -hh, 0], [hw, -hh, 0], [hw, hh, 0]], dtype=np.float64)
        hom = np.concatenate([corners_local, np.ones((4, 1))], axis=1)
        corners_world = (hom @ m.T)[:, :3]
        pts, depths = project_points(vp, corners_world, W, H)
        if np.all(depths < 0.02):
            return color, depth_buf
        if np.any(depths < 0.02):
            return color, depth_buf  # 穿帮就跳过，好过糊一屏

        src = np.float32([[0, 0], [0, th], [tw, th], [tw, 0]])
        Hm = cv2.getPerspectiveTransform(src, pts.astype(np.float32))
        warped = cv2.warpPerspective(tex, Hm, (W, H), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        alpha = (warped[:, :, 3:4].astype(np.float32) / 255.0) * float(obj.get("opacity", 1.0))
        color = (warped[:, :, :3].astype(np.float32) * alpha + color * (1.0 - alpha))

        plane_depth = float(np.mean(depths))
        depth_buf = np.where((alpha[:, :, 0] > 0.5) & (np.isnan(depth_buf) | (depth_buf > plane_depth)),
                             plane_depth, depth_buf).astype(np.float32)
        return color, depth_buf

    def _draw_plane(self, obj, vp, color, depth_buf, W, H, cam_pos):
        size = float(obj.get("size", 20.0))
        y = float(obj.get("position", [0, 0, 0])[1])
        gx, gz = float(obj.get("position", [0, 0, 0])[0]), float(obj.get("position", [0, 0, 0])[2])
        base = np.array(obj.get("color", [70, 74, 82]), dtype=np.float32)
        quad = np.array([[gx - size, y, gz - size], [gx + size, y, gz - size],
                         [gx + size, y, gz + size], [gx - size, y, gz + size]], dtype=np.float64)
        pts, depths = project_points(vp, quad, W, H)
        if np.all(depths < 0.05):
            return color, depth_buf

        # 视锥裁剪：把落在相机背后的点沿视线推到近平面附近
        pts = np.where(np.isfinite(pts), pts, 0).astype(np.float32)
        overlay = color.copy()
        cv2.fillPoly(overlay, [pts.astype(np.int32)], base.tolist())
        fade = np.clip(1.0 - np.linalg.norm(np.array([gx, y, gz]) - cam_pos) / (size * 1.8), 0.35, 1.0)
        color = color * (1 - fade * 0.55) + overlay * (fade * 0.55)

        # 网格线，提供运动参照
        grid = np.arange(-size, size + 1, 2.0)
        for g in grid:
            a, _ = project_points(vp, np.array([[gx + g, y, gz - size], [gx + g, y, gz + size]]), W, H)
            b, _ = project_points(vp, np.array([[gx - size, y, gz + g], [gx + size, y, gz + g]]), W, H)
            for seg in (a, b):
                if np.all(np.abs(seg) < W * H):
                    cv2.line(color, tuple(seg[0].astype(int)), tuple(seg[1].astype(int)),
                             (base * 0.72).tolist(), 1, cv2.LINE_AA)

        plane_depth = float(np.nanmedian(np.where(depths > 0, depths, np.nan)))
        depth_buf = np.where(np.isnan(depth_buf), plane_depth, depth_buf).astype(np.float32)
        return color, depth_buf

    def _draw_figure(self, obj, vp, color, depth_buf, mask_buf, pose_canvas, W, H):
        joints = pose_lib.joints_world(obj)
        pts, depths = project_points(vp, joints, W, H)
        conf = (depths > 0.05).astype(np.float32)
        if conf.sum() < 6:
            return color, depth_buf, mask_buf, pose_canvas

        sil = pose_lib.silhouette(pts, W, H).astype(np.float32) / 255.0
        tint = np.array(obj.get("color", [232, 228, 220]), dtype=np.float32)
        shade = np.clip(0.55 + 0.45 * (pts[:, 1].min() / max(H, 1)), 0.55, 1.0)
        alpha = (sil * float(obj.get("opacity", 1.0)))[:, :, None]
        color = color * (1 - alpha) + (tint * shade) * alpha
        mask_buf = np.maximum(mask_buf, (sil > 0.4).astype(np.uint8) * 255)

        fig_depth = float(np.mean(depths[conf > 0]))
        depth_buf = np.where((sil > 0.5) & (np.isnan(depth_buf) | (depth_buf > fig_depth)),
                             fig_depth, depth_buf).astype(np.float32)
        if pose_canvas is not None:
            pose_canvas = np.maximum(pose_canvas, pose_lib.render_openpose(pts, conf, W, H))
        return color, depth_buf, mask_buf, pose_canvas


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _backdrop(W: int, H: int) -> np.ndarray:
    ramp = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None]
    grad = BACKDROP_TOP[None, None, :] * (1 - ramp[:, :, None]) + BACKDROP_BOTTOM[None, None, :] * ramp[:, :, None]
    return np.repeat(grad, W, axis=1).astype(np.float32)


def _view_depth(vp, obj, cam_pos) -> float:
    return float(np.linalg.norm(np.array(obj.get("position", [0, 0, 0]), dtype=np.float64) - cam_pos))


def _depth_to_rgb(depth_buf: np.ndarray, camera: dict) -> np.ndarray:
    d = np.nan_to_num(depth_buf, nan=0.0)
    valid = d > 0
    if not valid.any():
        return np.zeros((*d.shape, 3), dtype=np.uint8)
    lo, hi = np.percentile(d[valid], 2), np.percentile(d[valid], 98)
    hi = max(hi, lo + 1e-3)
    norm = np.clip((d - lo) / (hi - lo), 0, 1)
    gray = np.where(valid, (1.0 - norm) * 255.0, 0.0)
    return np.repeat(gray[:, :, None], 3, axis=2).astype(np.uint8)


def _normals_to_rgb(depth_buf: np.ndarray) -> np.ndarray:
    d = np.nan_to_num(depth_buf, nan=0.0).astype(np.float32)
    gy, gx = np.gradient(d)
    nx, ny, nz = -gx * 8.0, -gy * 8.0, np.ones_like(d)
    norm = np.sqrt(nx ** 2 + ny ** 2 + nz ** 2) + 1e-6
    rgb = np.stack([nx / norm, ny / norm, nz / norm], axis=2)
    return np.clip((rgb * 0.5 + 0.5) * 255.0, 0, 255).astype(np.uint8)


def _lineart(color: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(np.clip(color, 0, 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 42, 120)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    inv = 255 - edges
    return np.repeat(inv[:, :, None], 3, axis=2)


def render_camera_path(scene: dict, frames: int = 6) -> list:
    """沿相机轨迹均匀取 frames 个机位渲染，用于预演关键帧。"""
    renderer = Renderer(scene)
    path = scene.get("camera", {}).get("path") or []
    cams = sample_cameras(scene, frames)
    out = []
    for cam in cams:
        res = renderer.render(camera=cam)
        res["camera"] = cam
        out.append(res)
    return out


def sample_cameras(scene: dict, frames: int = 6) -> list:
    camera = dict(scene["camera"])
    path = scene.get("camera", {}).get("path") or []
    if not path:
        return [camera for _ in range(max(1, frames))]
    frames = max(1, int(frames))
    if frames == 1:
        return [camera]
    cams = []
    for i in range(frames):
        u = i / (frames - 1) * (len(path) - 1)
        lo, hi = int(math.floor(u)), int(math.ceil(u))
        w = u - lo
        a, b = path[lo], path[min(hi, len(path) - 1)]
        cam = dict(camera)
        cam["position"] = [a["position"][k] * (1 - w) + b["position"][k] * w for k in range(3)]
        cam["target"] = [a["target"][k] * (1 - w) + b["target"][k] * w for k in range(3)]
        if a.get("vfov") or b.get("vfov"):
            va = a.get("vfov") or camera["vfov"]
            vb = b.get("vfov") or camera["vfov"]
            cam["vfov"] = va * (1 - w) + vb * w
        cams.append(cam)
    return cams
