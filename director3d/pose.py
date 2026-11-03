"""人物替身：18 点 COCO 骨骼（可直接喂给 openpose ControlNet）。

本地坐标：脚底中心为原点，Y 向上，人物面朝 +Z，单位米。
所有关节位置都按身高 ''height'' 的比例给出（基准 1.75m ≥ 常规人体比例）。
"""
from __future__ import annotations

import math

import numpy as np

from .scene import model_matrix

# COCO-18 顺序：0 鼻 1 颈 2 右肩 3 右肘 4 右腕 5 左肩 6 左肘 7 左腕
#             8 右髋 9 右膝 10 右踝 11 左髋 12 左膝 13 左踝 14 右眼 15 左眼 16 右耳 17 左耳
JOINT_NAMES = ["nose", "neck",
               "Rshoulder", "Relbow", "Rwrist",
               "Lshoulder", "Lelbow", "Lwrist",
               "Rhip", "Rknee", "Rankle",
               "Lhip", "Lknee", "Lankle",
               "Reye", "Leye", "Rear", "Lear"]

LIMBS = [(1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9), (9, 10),
         (1, 11), (11, 12), (12, 13), (1, 0), (0, 14), (14, 16), (0, 15), (15, 17),
         (2, 16), (5, 17)]

LIMB_COLORS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
               (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
               (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
               (255, 0, 255), (255, 0, 170), (255, 0, 85), (255, 0, 255)]

# 以身高比例表示的基准站姿
_BASE = np.array([
    [0.000, 1.640, 0.020],   # nose
    [0.000, 1.440, 0.000],   # neck
    [-0.180, 1.400, 0.000],  # R shoulder
    [-0.250, 1.130, 0.020],  # R elbow
    [-0.270, 0.880, 0.030],  # R wrist
    [0.180, 1.400, 0.000],   # L shoulder
    [0.250, 1.130, 0.020],   # L elbow
    [0.270, 0.880, 0.030],   # L wrist
    [-0.110, 0.930, 0.000],  # R hip
    [-0.120, 0.510, 0.010],  # R knee
    [-0.120, 0.080, 0.000],  # R ankle
    [0.110, 0.930, 0.000],   # L hip
    [0.120, 0.510, 0.010],   # L knee
    [0.120, 0.080, 0.000],   # L ankle
    [-0.040, 1.680, 0.070],  # R eye
    [0.040, 1.680, 0.070],   # L eye
    [-0.085, 1.660, 0.000],  # R ear
    [0.085, 1.660, 0.000],   # L ear
], dtype=np.float64)

POSES = ["站立", "行走", "奔跑", "下蹲", "坐姿", "举手", "指向"]

FEET_GROUND = 0.02  # 踝关节到脚底的距离（比例）


def joints_local(mannequin: dict) -> np.ndarray:
    """返回 (18,3) 本地坐标，单位米。"""
    height = float(mannequin.get("height", 1.75))
    pose = mannequin.get("pose", "站立")
    j = _BASE.copy()
    h1 = height / 1.75  # 相对基准身高的缩放

    if pose in ("行走", "奔跑"):
        amp = 0.28 if pose == "行走" else 0.52
        j[9, 2] += amp * 0.6
        j[10, 2] += amp * 0.9
        j[12, 2] -= amp * 0.6
        j[13, 2] -= amp * 0.9
        j[9, 1] += amp * 0.10
        j[3, 2] -= amp * 0.5
        j[6, 2] += amp * 0.5
        j[3, 1] += 0.02
        j[6, 1] += 0.02
        if pose == "奔跑":
            j[1, 2] += 0.06
            j[0, 2] += 0.06
    elif pose == "下蹲":
        j[1, 1] -= 0.36
        j[0, 1] -= 0.36
        j[8:11, 1] -= 0.34
        j[11:14, 1] -= 0.34
        j[9, 2] += 0.30
        j[12, 2] += 0.30
        j[3, 1] -= 0.10
        j[6, 1] -= 0.10
    elif pose == "坐姿":
        j[1, 1] -= 0.32
        j[0, 1] -= 0.32
        j[8:11, 1] -= 0.32
        j[11:14, 1] -= 0.32
        j[8:11, 2] += 0.12
        j[11:14, 2] += 0.12
        j[9, 2] += 0.38
        j[12, 2] += 0.38
        j[10, 1] -= 0.30
        j[13, 1] -= 0.30
    elif pose == "举手":
        j[3] = [-0.240, 1.520, 0.010]
        j[4] = [-0.260, 1.760, 0.020]
        j[6] = [0.240, 1.520, 0.010]
        j[7] = [0.260, 1.760, 0.020]
    elif pose == "指向":
        j[3] = [-0.230, 1.420, 0.060]
        j[4] = [-0.210, 1.380, 0.420]
        j[6] = [0.270, 1.180, 0.020]
        j[7] = [0.280, 0.930, 0.030]

    j *= h1
    return j


def joints_world(mannequin: dict) -> np.ndarray:
    local = joints_local(mannequin)
    m = model_matrix(mannequin)
    hom = np.concatenate([local, np.ones((len(local), 1))], axis=1)
    return (hom @ m.T)[:, :3]


def render_openpose(joints2d: np.ndarray, conf: np.ndarray, width: int, height: int,
                    stickwidth_ratio: float = 0.0075) -> np.ndarray:
    """画 OpenPose 风格骨骼图（黑底彩线），可直接给 openpose ControlNet。"""
    import cv2

    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    radius = max(2, int(min(width, height) * stickwidth_ratio))
    thick = max(2, int(radius * 1.6))
    for (a, b), color in zip(LIMBS, LIMB_COLORS):
        if conf[a] <= 0.05 or conf[b] <= 0.05:
            continue
        pt1 = tuple(np.round(joints2d[a]).astype(int))
        pt2 = tuple(np.round(joints2d[b]).astype(int))
        cv2.line(canvas, pt1, pt2, color, thick, cv2.LINE_AA)
    for idx in range(len(joints2d)):
        if conf[idx] <= 0.05:
            continue
        center = tuple(np.round(joints2d[idx]).astype(int))
        cv2.circle(canvas, center, radius, (0, 0, 255), -1, cv2.LINE_AA)
        cv2.circle(canvas, center, radius, (255, 255, 255), 1, cv2.LINE_AA)
    return canvas


def silhouette(joints2d: np.ndarray, width: int, height: int) -> np.ndarray:
    """替身剪影（uint8 0/255），用于预演合成与蒙版。"""
    import cv2

    mask = np.zeros((height, width), dtype=np.uint8)
    r = max(3, int(min(width, height) * 0.012))
    for a, b in LIMBS:
        cv2.line(mask, tuple(np.round(joints2d[a]).astype(int)),
                 tuple(np.round(joints2d[b]).astype(int)), 255, r * 2, cv2.LINE_AA)
    head = tuple(np.round(joints2d[0]).astype(int))
    cv2.circle(mask, head, int(r * 2.1), 255, -1, cv2.LINE_AA)
    return mask
