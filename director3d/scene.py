"""场景数据模型与相机数学。

前后端共用同一套语义，务必保持一致：
  * 世界坐标：Y 轴向上，单位米，相机默认朝 -Z 看（与 three.js 一致）
  * 旋转：`rotation` = [pitch, yaw, roll]，单位度，等价 three.js `rotation.order = "YXZ"`
  * 卡片（card）：本地 XY 平面上的矩形，正面朝 +Z
  * 相机：position + target + 垂直视场角，无 roll
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

SCENE_VERSION = 1
SENSOR_HEIGHT_MM = 24.0  # 全画幅 36x24 的短边

ASPECT_PRESETS = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "2.39:1": 2.39,
    "1.85:1": 1.85,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "1:1": 1.0,
}


# --------------------------------------------------------------------------- #
# 相机数学
# --------------------------------------------------------------------------- #
def lens_to_vfov(lens_mm: float, sensor_mm: float = SENSOR_HEIGHT_MM) -> float:
    """焦距(mm) -> 垂直视场角(度)。35mm 全画幅等效。"""
    lens_mm = max(float(lens_mm), 1.0)
    return math.degrees(2.0 * math.atan(sensor_mm / (2.0 * lens_mm)))


def vfov_to_lens(vfov_deg: float, sensor_mm: float = SENSOR_HEIGHT_MM) -> float:
    vfov_deg = min(max(float(vfov_deg), 0.5), 175.0)
    return sensor_mm / (2.0 * math.tan(math.radians(vfov_deg) / 2.0))


def vfov_to_hfov(vfov_deg: float, aspect: float) -> float:
    t = math.tan(math.radians(vfov_deg) / 2.0) * aspect
    return math.degrees(2.0 * math.atan(t))


def look_at(eye, target, up=(0.0, 1.0, 0.0)) -> np.ndarray:
    """返回 world -> camera 的 4x4 视图矩阵（OpenGL 约定，相机看 -Z）。"""
    eye = np.asarray(eye, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    f = target - eye
    n = np.linalg.norm(f)
    f = np.array([0.0, 0.0, -1.0]) if n < 1e-9 else f / n
    up = np.asarray(up, dtype=np.float64)
    s = np.cross(f, up)
    if np.linalg.norm(s) < 1e-6:  # 视线与 up 平行时的兜底
        s = np.cross(f, np.array([0.0, 0.0, 1.0]))
    s = s / np.linalg.norm(s)
    u = np.cross(s, f)

    view = np.eye(4, dtype=np.float64)
    view[0, :3], view[1, :3], view[2, :3] = s, u, -f
    view[:3, 3] = -view[:3, :3] @ eye
    return view


def perspective(vfov_deg: float, aspect: float, near: float = 0.05, far: float = 500.0) -> np.ndarray:
    f = 1.0 / math.tan(math.radians(vfov_deg) / 2.0)
    proj = np.zeros((4, 4), dtype=np.float64)
    proj[0, 0] = f / max(aspect, 1e-6)
    proj[1, 1] = f
    proj[2, 2] = (far + near) / (near - far)
    proj[2, 3] = 2.0 * far * near / (near - far)
    proj[3, 2] = -1.0
    return proj


def euler_yxz(pitch_deg, yaw_deg, roll_deg) -> np.ndarray:
    """R = Ry(yaw) @ Rx(pitch) @ Rz(roll)，与 three.js rotation.order="YXZ" 等价。"""
    p, y, r = (math.radians(float(v)) for v in (pitch_deg, yaw_deg, roll_deg))
    cx, sx = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(r), math.sin(r)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float64)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float64)
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float64)
    return ry @ rx @ rz


def matrix_to_euler_yxz(R: np.ndarray):
    """3x3 旋转矩阵 -> [pitch, yaw, roll] (度)，与 euler_yxz 互逆。"""
    R = np.asarray(R, dtype=np.float64)
    sp = float(np.clip(-R[1, 2], -1.0, 1.0))
    pitch = math.degrees(math.asin(sp))
    yaw = math.degrees(math.atan2(R[0, 2], R[2, 2]))
    roll = math.degrees(math.atan2(R[1, 0], R[1, 1]))
    return [pitch, yaw, roll]


def model_matrix(obj: dict) -> np.ndarray:
    pos = np.asarray(obj.get("position", [0, 0, 0]), dtype=np.float64)
    rot = obj.get("rotation", [0, 0, 0])
    scale = np.asarray(obj.get("scale", [1, 1, 1]), dtype=np.float64)
    if np.isscalar(scale):
        scale = np.array([scale, scale, scale], dtype=np.float64)
    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = euler_yxz(*rot) @ np.diag(scale[:3])
    m[:3, 3] = pos
    return m


def project_points(vp: np.ndarray, pts_world: np.ndarray, width: int, height: int):
    """世界点 -> 屏幕像素坐标 (x, y)，以及相机空间深度 (正值 = 前方)。"""
    pts = np.atleast_2d(np.asarray(pts_world, dtype=np.float64))
    hom = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
    clip = hom @ vp.T
    w = clip[:, 3]
    w = np.where(np.abs(w) < 1e-9, 1e-9, w)
    ndc = clip[:, :3] / w[:, None]
    x = (ndc[:, 0] * 0.5 + 0.5) * width
    y = (0.5 - ndc[:, 1] * 0.5) * height
    return np.stack([x, y], axis=1), clip[:, 3]  # clip.w = -z_cam，即相机前方距离


def make_vp(camera: dict, width: int, height: int) -> np.ndarray:
    """projection @ view，标准 4x4（分辨率由调用方另行传入）。"""
    aspect = width / max(height, 1)
    vfov = camera.get("vfov")
    if vfov is None:
        vfov = lens_to_vfov(camera.get("lens_mm", 35.0))
    view = look_at(camera["position"], camera["target"])
    proj = perspective(vfov, aspect, camera.get("near", 0.05), camera.get("far", 500.0))
    return proj @ view


# --------------------------------------------------------------------------- #
# 场景结构
# --------------------------------------------------------------------------- #
def new_scene(width: int = 1024, height: int = 576, fps: float = 24.0) -> dict:
    vfov = 40.0
    return {
        "version": SCENE_VERSION,
        "fps": float(fps),
        "frame_size": [int(width), int(height)],
        "camera": {
            "position": [0.0, 1.5, 6.0],
            "target": [0.0, 1.3, 0.0],
            "lens_mm": round(vfov_to_lens(vfov), 2),
            "vfov": vfov,
            "near": 0.05,
            "far": 500.0,
            "path": [],
        },
        "objects": [],
        "lights": [
            {"type": "ambient", "intensity": 0.55, "color": [1.0, 0.98, 0.95]},
            {"type": "directional", "direction": [-0.4, -0.9, -0.5], "intensity": 1.1, "color": [1.0, 0.97, 0.9]},
        ],
        "meta": {},
    }


def normalize_scene(scene: dict) -> dict:
    """补齐缺省字段，容忍前端传来的半成品。"""
    base = new_scene(*scene.get("frame_size", [1024, 576]), fps=scene.get("fps", 24.0))
    out = dict(base)
    out.update({k: v for k, v in scene.items() if v is not None})
    out["frame_size"] = list(out.get("frame_size") or [1024, 576])

    cam = dict(base["camera"])
    cam.update(out.get("camera") or {})
    for key in ("position", "target"):
        cam[key] = [float(x) for x in (cam.get(key) or base["camera"][key])]
    if cam.get("lens_mm"):
        cam["vfov"] = lens_to_vfov(cam["lens_mm"])
    out["camera"] = cam

    objs = []
    for raw in out.get("objects") or []:
        obj = dict(raw)
        obj.setdefault("kind", "box")
        obj.setdefault("position", [0.0, 0.0, 0.0])
        obj.setdefault("rotation", [0.0, 0.0, 0.0])
        obj.setdefault("scale", [1.0, 1.0, 1.0])
        obj.setdefault("opacity", 1.0)
        obj.setdefault("visible", True)
        obj["id"] = str(obj.get("id") or f"obj_{len(objs)}")
        objs.append(obj)
    out["objects"] = objs
    out["lights"] = out.get("lights") or base["lights"]
    out["meta"] = out.get("meta") or {}
    return out


def objects_by_kind(scene: dict, *kinds: str):
    return [o for o in scene.get("objects", []) if o.get("kind") in kinds and o.get("visible", True)]


def dip_to_depth(norm: float, near: float, far: float) -> float:
    """归一化深度 [0,1]（0=最近）-> 场景中的 z 距离（米）。"""
    norm = float(min(max(norm, 0.0), 1.0))
    return near + norm * (far - near)


def depth_to_dip(depth: float, near: float, far: float) -> float:
    return float(min(max((depth - near) / max(far - near, 1e-6), 0.0), 1.0))


def card_size(depth: float, vfov_deg: float, aspect: float):
    """让卡片在给定距离上正好铺满画面时的世界尺寸。"""
    h = 2.0 * depth * math.tan(math.radians(vfov_deg) / 2.0)
    return h * aspect, h


def save_scene(scene: dict, path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(scene, fh, ensure_ascii=False, indent=1)
    return path


def load_scene(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return normalize_scene(json.load(fh))
