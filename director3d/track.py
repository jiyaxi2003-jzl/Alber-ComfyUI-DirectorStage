"""从视频估计相机运动轨迹（ORB + 本质矩阵 + 深度定标）。

单目重建的尺度天然缺失，这里用同一帧的深度图做一次性定标：
在 [0.01, 2]×场景中位深度 的范围内搜索平移尺度，取重投影误差最小的那个。
结果只保证"方向与相对幅度合理"，不保证绝对精度——预演够用。
"""
from __future__ import annotations

import math

import numpy as np

try:
    import cv2

    HAS_CV2 = True
except Exception:  # pragma: no cover
    HAS_CV2 = False
    cv2 = None

from .scene import dip_to_depth


def _intrinsics(width: int, height: int, vfov_deg: float) -> np.ndarray:
    f = 0.5 * height / math.tan(math.radians(vfov_deg) / 2.0)
    return np.array([[f, 0, width / 2.0], [0, f, height / 2.0], [0, 0, 1]], dtype=np.float64)


def _fallback_path(n_frames: int, dips: list) -> list:
    """跟踪失败时的兜底：缓慢推镜 + 轻微下摇，至少保证时间轴完整。"""
    median_dip = float(np.median([float(np.median(d)) for d in dips])) if dips else 0.5
    z0 = dip_to_depth(median_dip, 1.5, 25.0) * 1.6
    path = []
    for i in range(n_frames):
        t = i / max(n_frames - 1, 1)
        path.append({
            "t": float(i),
            "position": [0.0, 1.5 - 0.02 * t, z0 - 0.35 * z0 * t],
            "target": [0.0, 1.4, 0.0],
            "vfov": None,
        })
    return path


def estimate_camera_path(frames: list, dips: list, vfov_deg: float, near: float = 1.5,
                         far: float = 25.0) -> tuple[list, dict]:
    info = {"method": "fallback", "matches": [], "inliers": [], "translation": 0.0, "notes": []}
    n = len(frames)
    if n < 2 or not HAS_CV2:
        info["notes"].append("帧数不足或缺少 OpenCV，使用兜底轨迹")
        return _fallback_path(n, dips), info

    gray = [cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in frames]
    h, w = gray[0].shape[:2]
    K = _intrinsics(w, h, vfov_deg)
    Kinv = np.linalg.inv(K)

    orb = cv2.ORB_create(nfeatures=2000, fastThreshold=12)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    kps, descs = [], []
    for g in gray:
        kp, des = orb.detectAndCompute(g, None)
        kps.append(kp)
        descs.append(des)

    C = np.eye(4)  # cam -> world（第 0 帧位于原点）
    poses = [C.copy()]
    ok_pairs = 0

    for i in range(1, n):
        if descs[i] is None or descs[i - 1] is None or len(kps[i]) < 12 or len(kps[i - 1]) < 12:
            poses.append(poses[-1].copy())
            info["matches"].append(0)
            info["inliers"].append(0)
            continue
        raw = matcher.knnMatch(descs[i - 1], descs[i], k=2)
        good = [m for m, n2 in (p for p in raw if len(p) == 2) if m.distance < 0.75 * n2.distance]
        info["matches"].append(len(good))
        if len(good) < 20:
            poses.append(poses[-1].copy())
            info["inliers"].append(0)
            continue

        p1 = np.float64([kps[i - 1][m.queryIdx].pt for m in good])
        p2 = np.float64([kps[i][m.trainIdx].pt for m in good])
        E, mask = cv2.findEssentialMat(p1, p2, K, method=cv2.RANSAC, prob=0.999, threshold=1.2, maxIters=3000)
        if E is None or E.shape != (3, 3):
            poses.append(poses[-1].copy())
            info["inliers"].append(0)
            continue
        inl, R, t, mask2 = cv2.recoverPose(E, p1, p2, K, mask=mask.copy())
        info["inliers"].append(int(inl))
        if inl < 15:
            poses.append(poses[-1].copy())
            continue

        t = t.reshape(3)
        d1 = np.asarray(dips[i - 1], dtype=np.float32)
        depths = np.array([dip_to_depth(d1[int(round(y)) % h, int(round(x)) % w], near, far) for x, y in p1])
        scale = _solve_scale(p1, p2, depths, R, t, Kinv, K)
        rel = np.eye(4)
        rel[:3, :3] = R
        rel[:3, 3] = t * scale  # cam_i -> cam_{i-1} 的平移已含尺度
        # P_rel 是 cam_{i-1} -> cam_i：X_i = R X_{i-1} + t
        poses.append(poses[-1] @ np.linalg.inv(rel))
        ok_pairs += 1

    if ok_pairs < max(3, n // 4):
        info["notes"].append(f"成功跟踪的帧对仅 {ok_pairs}/{n - 1}，改用兜底轨迹")
        return _fallback_path(n, dips), info

    med = float(np.median([float(np.median(d)) for d in dips]))
    scene_scale = dip_to_depth(med, near, far)
    path = _poses_to_path(poses, n, scene_scale)
    info["method"] = "sift" if False else "orb-essential"
    info["translation"] = float(np.linalg.norm(
        np.array([p["position"] for p in path])[-1] - np.array([p["position"] for p in path])[0]))
    return path, info


def _solve_scale(p1, p2, depths, R, t, Kinv, K) -> float:
    """在候选平移尺度上最小化像素重投影误差，得到有量纲的平移向量长度。"""
    n = min(len(p1), 400)
    idx = np.linspace(0, len(p1) - 1, n).astype(int)
    uv1 = np.hstack([p1[idx], np.ones((len(idx), 1))])
    bearing = (Kinv @ uv1.T).T
    bearing[:, 2] = -1.0  # 相机看 -Z，深度取正
    X1 = bearing * depths[idx][:, None]
    med_depth = float(np.median(depths[idx])) if len(depths) else 1.0
    fx, fy = K[0, 0], K[1, 1]
    cx, cy = K[0, 2], K[1, 2]

    best_s, best_err = 1.0, float("inf")
    for s in np.geomspace(0.01, 1.5, 40):
        X2 = (R @ X1.T).T + t.reshape(1, 3) * s * med_depth
        z = np.where(np.abs(X2[:, 2]) < 1e-4, -1e-4, X2[:, 2])
        u = fx * X2[:, 0] / -z + cx
        v = fy * X2[:, 1] / -z + cy
        err = float(np.mean(np.linalg.norm(np.stack([u, v], 1) - p2[idx], axis=1)))
        if err < best_err:
            best_err, best_s = err, float(s)
    return best_s * med_depth


def _poses_to_path(poses: list, n: int, scene_scale: float) -> list:
    positions = []
    targets = []
    for C in poses:
        Rcw, tcw = C[:3, :3], C[:3, 3]
        eye = -Rcw.T @ tcw
        fwd = Rcw.T @ np.array([0, 0, -1.0])
        positions.append(eye)
        targets.append(eye + fwd * max(scene_scale * 0.6, 0.5))
    positions = np.array(positions)

    span = float(np.linalg.norm(positions[-1] - positions[0]))
    limit = max(scene_scale * 0.9, 0.4)
    if span > limit:  # 抑制跟踪尺度漂移
        positions *= limit / span

    # 把第 0 帧摆回"人在画面前方"的默认位置，避免轨迹在原点附近抖动
    offset = np.array([0.0, 1.5, scene_scale * 1.5]) - positions[0]
    positions += offset
    targets = [t + offset for t in targets]

    path = []
    for i in range(n):
        j = min(i, len(positions) - 1)
        path.append({
            "t": float(i),
            "position": [round(float(v), 4) for v in positions[j]],
            "target": [round(float(v), 4) for v in targets[j]],
            "vfov": None,
        })
    return _smooth(path)


def _smooth(path: list, k: int = 3) -> list:
    if len(path) < 3:
        return path
    pos = np.array([p["position"] for p in path])
    ker = np.ones(k) / k
    sm = np.stack([np.convolve(pos[:, i], ker, mode="same") for i in range(3)], axis=1)
    edge = k // 2
    sm[:edge] = pos[:edge]
    sm[-edge:] = pos[-edge:]
    for i, p in enumerate(path):
        p["position"] = [round(float(v), 4) for v in sm[i]]
    return path


def describe_motion(path: list) -> str:
    """把轨迹翻译成 H3 能听懂的运镜描述。"""
    if len(path) < 2:
        return "固定机位"
    pos = np.array([p["position"] for p in path])
    delta = pos[-1] - pos[0]
    lateral = float(delta[0])
    vertical = float(delta[1])
    depth = float(abs(delta[2]))
    total = float(np.linalg.norm(delta))
    if total < 0.12:
        return "固定机位，轻微手持晃动"
    if depth > max(abs(lateral), abs(vertical)) * 1.6:
        # 相机朝 -Z 看，z 变小 = 靠近主体 = 推进
        return "镜头向前推进（dolly in）" if delta[2] < 0 else "镜头向后拉远（dolly out）"
    if abs(lateral) > abs(vertical) * 1.6:
        return "镜头向右横移（truck right）" if lateral > 0 else "镜头向左横移（truck left）"
    if abs(vertical) > total * 0.6:
        return "镜头上升（crane up）" if vertical > 0 else "镜头下降（crane down）"
    return "镜头缓慢环绕（arc shot）"
