"""MiniMax H3（ref2va / fl2va）参考包：把导演台的预演关键帧组织成 H3 认识的输入。

H3 的参考图机制靠提示词里的 ``<Picture 1>`` ... ``<Picture 9>`` 占位来对应
``ref_image_0`` ... ``ref_image_8``，所以"图 + 带编号的提示词"必须成套产出。
"""
from __future__ import annotations

import math

from .scene import ASPECT_PRESETS


def _shot_role(i: int, n: int) -> str:
    if n <= 1:
        return "开场机位"
    if i == 0:
        return "开场机位"
    if i == n - 1:
        return "收尾机位"
    return f"第 {i + 1} 个机位（片中过渡）"


def shot_plan(scene: dict, shot_count: int, duration: float) -> list:
    """按时间轴均分机位，返回每张参考图的秒数位置。"""
    n = max(1, int(shot_count))
    out = []
    for i in range(n):
        t = 0.0 if n == 1 else i * duration / (n - 1)
        out.append({"index": i, "time": round(t, 2), "frame": int(round(t * 24)),
                    "role": _shot_role(i, n)})
    return out


def build_prompt(scene: dict, *, shot_count: int = 4, duration: float = 5.0,
                 style_note: str = "", subject_note: str = "", extra: str = "",
                 aspect: str = "auto") -> dict:
    meta = scene.get("meta", {})
    cam = scene.get("camera", {})
    lens = float(cam.get("lens_mm", 35))
    vfov = float(cam.get("vfov", 40))
    motion = meta.get("motion") or "固定机位"
    plan = shot_plan(scene, shot_count, duration)
    aspect_txt = aspect if aspect != "auto" else f"{meta.get('aspect', 16 / 9):.2f}:1"

    ref_lines = []
    for p in plan:
        ref_lines.append(f"<Picture {p['index'] + 1}> 对应 {p['time']}s 的{ p['role']}")

    lines = [
        "【参考图含义】",
        *[f"- {line}" for line in ref_lines],
        "",
        "【机位与运镜】",
        f"- 主焦段 {lens:.0f}mm（垂直视场约 {vfov:.1f}°），{aspect_txt} 画幅；",
        f"- 整段运镜：{motion}；镜头沿一条连续路径运动，不得跳切、不得瞬移；",
        "- 透视关系、地平线高度、主体在画面中的相对位置，逐帧与参考图对齐。",
        "",
        "【主体一致性】",
        "- 人物/主体的五官、发型、体型、服装与 <Picture 1> 完全一致；"
        "多机位之间不得换脸、换装、换体型；",
        "- 主体的身高比例与画面占比按参考图还原，脚部接地，不悬空、不穿模。",
    ]
    if subject_note.strip():
        lines.append(f"- {subject_note.strip()}")

    lines += [
        "",
        "【场景】",
        f"- 场景结构、景深层次与空间纵深关系沿用参考图：{meta.get('depth_backend', '单目深度分层')}重建；",
        "- 远景保持空气透视，近景细节清晰，光源方向在各机位间保持一致。",
    ]
    if style_note.strip():
        lines += ["", "【风格】", f"- {style_note.strip()}"]
    lines += [
        "",
        "【输出要求】",
        f"- 时长 {duration:g} 秒，24fps；画面稳定，无闪烁、无 morph 拖影；",
        "- 运动模糊符合所选快门的自然观感；如需声音，按画面内容生成环境声与拟音。",
    ]
    if extra.strip():
        lines += ["", "【补充】", extra.strip()]

    prompt = "\n".join(lines)
    return {
        "prompt": prompt,
        "motion": motion,
        "plan": plan,
        "meta": {
            "shot_count": len(plan),
            "duration": duration,
            "aspect": aspect_txt,
            "lens_mm": lens,
            "vfov": vfov,
            "guide_frames": [p["frame"] for p in plan],
            "scene_id": meta.get("scene_id", ""),
        },
    }


def pick_references(images: list, plan: list, plan_images: dict | None = None) -> tuple[list, list]:
    """给每张参考图配上说明，返回 (图片列表, 说明列表)。"""
    picked, notes = [], []
    for p in plan:
        idx = p["index"]
        if plan_images and idx in plan_images:
            picked.append(plan_images[idx])
        elif idx < len(images):
            picked.append(images[idx])
        else:
            picked.append(images[-1] if images else None)
        notes.append(f"<Picture {idx + 1}> {p['role']} @ {p['time']}s")
    return [img for img in picked if img is not None], notes


def guide_suggestion(plan: list) -> str:
    """把机位表翻译成 AddGuide 节点的挂法建议。"""
    if len(plan) <= 1:
        return "单机位：直接把 <Picture 1> 接到 MiniMaxH3ReferenceToVideo 的 ref_image_0。"
    parts = []
    for p in plan[1:]:
        parts.append(f"<Picture {p['index'] + 1}> -> MiniMaxH3AddGuide @ frame_idx={p['frame']}（{p['time']}s）")
    return ("首帧走 ref_image_0 锁人物，其余机位用 MiniMaxH3AddGuide 钉在时间轴上：\n  "
            + "\n  ".join(parts))


def aspect_from_scene(scene: dict) -> str:
    w, h = scene.get("frame_size", [1024, 576])
    ratio = w / max(h, 1)
    best, best_err = "16:9", 9e9
    for name, value in ASPECT_PRESETS.items():
        err = abs(value - ratio)
        if err < best_err:
            best, best_err = name, err
    return best


def duration_from_scene(scene: dict, default: float = 5.0) -> float:
    fps = float(scene.get("fps", 24) or 24)
    n = len(scene.get("camera", {}).get("path") or [])
    if n <= 1:
        return default
    return round(min(max((n - 1) / fps, 2.0), 15.0), 2)
