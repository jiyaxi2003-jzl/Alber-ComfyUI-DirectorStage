"""Alber-导演台 节点集合。

6 个节点：
  1. 视频转3D      —— MP4 -> 单目深度 + 相机轨迹 -> 2.5D 分层场景
  2. 场景编辑      —— Alber-3D 导演台交互节点（前端把编辑结果写回 state widget）
  3. 预演渲染      —— 沿相机轨迹出预演/深度/骨骼/线稿/蒙版
  4. H3 参考包     —— 组织成 <Picture N> 参考图 + 运镜提示词
  5. 场景读写      —— 保存 / 载入 scene.json
"""
from __future__ import annotations

import json
import os

import numpy as np

from director3d import build as build_mod
from director3d import depth as depth_mod
from director3d import h3 as h3_mod
from director3d import video_io
from director3d.paths import OUTPUT_DIR, input_video_path, new_scene_id, scene_dir
from director3d.raster import Renderer, sample_cameras
from director3d.scene import (ASPECT_PRESETS, load_scene, normalize_scene, save_scene)
from director3d.tensor import to_numpy, to_tensor

NODE_CATEGORY = "Alber-导演台"
# 兼容：外部若引用 dir_nodes.CATEGORY，仍拿得到同一个值。
# 注意 ComfyUI 取的是**节点类上的** CATEGORY 属性，所以下面每个类都要显式挂一份。
CATEGORY = NODE_CATEGORY
ASPECT_OPTIONS = ["auto"] + list(ASPECT_PRESETS.keys())

# 前端「打开导演台」时通过 /director3d/scene 取回最近一次产出的场景
LAST_SCENE: dict = {}


def remember_scene(unique_id, scene):
    """把场景挂到节点 id 上，供前端按 origin_id 取回。"""
    if unique_id is not None:
        LAST_SCENE[str(unique_id)] = scene
    LAST_SCENE["_latest"] = scene
    return scene


def _hash(*values) -> str:
    import hashlib

    parts = []
    for v in values:
        if isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(str(v))
        else:
            try:
                parts.append(json.dumps(v, sort_keys=True, default=str))
            except Exception:
                parts.append(repr(type(v)))
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 1. 视频 -> 场景
# --------------------------------------------------------------------------- #
class DirectorVideoToScene:
    CATEGORY = NODE_CATEGORY
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("STRING", {"default": "", "multiline": False,
                                     "tooltip": "MP4 绝对路径，或放在 ComfyUI/input 下的文件名"}),
                "max_frames": ("INT", {"default": 16, "min": 2, "max": 96, "step": 1}),
                "long_side": ("INT", {"default": 960, "min": 320, "max": 1920, "step": 32}),
                "lens_mm": ("FLOAT", {"default": 35.0, "min": 8.0, "max": 200.0, "step": 0.5}),
                "aspect": (ASPECT_OPTIONS, {"default": "auto"}),
                "layers": ("INT", {"default": 3, "min": 1, "max": 4, "step": 1,
                                   "tooltip": "深度分层数：层数越多视差越细，但更吃内存"}),
                "near": ("FLOAT", {"default": 1.5, "min": 0.2, "max": 20.0, "step": 0.1}),
                "far": ("FLOAT", {"default": 26.0, "min": 2.0, "max": 200.0, "step": 0.5}),
                "ref_index": ("INT", {"default": 0, "min": 0, "max": 95, "step": 1,
                                      "tooltip": "用第几帧作为贴图来源（默认首帧）"}),
                "depth_backend": (["auto", "flow", "model", "input", "ground"], {"default": "auto"}),
                "include_ground": ("BOOLEAN", {"default": True}),
                "add_subject": ("BOOLEAN", {"default": True}),
                "scene_tag": ("STRING", {"default": "shot"}),
            },
            "optional": {
                "frames_in": ("IMAGE", {"tooltip": "可选：直接用已有的帧批次，跳过解码"}),
                "depth_in": ("IMAGE", {"tooltip": "可选：外部深度图批次（0=近 或 0-255）"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("DIRECTOR_SCENE", "IMAGE", "IMAGE", "STRING")
    RETURN_NAMES = ("场景", "参考帧", "深度图", "报告")
    FUNCTION = "run"

    @classmethod
    def IS_CHANGED(cls, video, **kwargs):
        return _hash(video, kwargs.get("max_frames"), kwargs.get("ref_index"),
                     kwargs.get("layers"), kwargs.get("long_side"), kwargs.get("depth_backend"))

    def run(self, video, max_frames, long_side, lens_mm, aspect, layers, near, far,
            ref_index, depth_backend, include_ground, add_subject, scene_tag,
            frames_in=None, depth_in=None, unique_id=None):
        notes = []
        info = None
        if frames_in is not None:
            frames = to_numpy(frames_in)
            from director3d.video_io import VideoInfo

            info = VideoInfo(path="(frames_in)", width=frames[0].shape[1], height=frames[0].shape[0],
                             fps=24.0, duration=len(frames) / 24.0, n_frames=len(frames))
            notes.append(f"使用上游帧批次：{len(frames)} 帧")
        else:
            path = input_video_path(video)
            frames, info = video_io.read_frames(path, max_frames=max_frames, long_side=long_side)
            notes.extend(getattr(info, "warnings", []))
            if not frames:
                raise RuntimeError("读不到任何视频帧：" + (path or "(空路径)") +
                                   "\n请填写 MP4 绝对路径，或把文件放到 ComfyUI/input 目录。")
            notes.append(f"解码 {len(frames)} 帧 / {info.width}x{info.height} / {info.fps:.2f}fps")

        cuts = video_io.detect_cuts(frames)
        if cuts:
            notes.append(f"检测到 {len(cuts)} 处镜头切换（第 {cuts[:6]} 帧）。当前按单镜头重建，"
                         f"可用 ref_index 选择不同镜头的参考帧。")

        dips_in = to_numpy(depth_in) if depth_in is not None else None
        if dips_in and depth_backend == "input":
            notes.append(f"使用外部深度图：{len(dips_in)} 张")

        dips, depth_note = depth_mod.estimate(frames, backend=depth_backend, depth_inputs=dips_in)
        notes.append(f"深度后端：{depth_note}")

        scene_id = new_scene_id(scene_tag or "shot")
        scene, diag = build_mod.reconstruct(
            frames, info, dips, depth_note, scene_id=scene_id, lens_mm=lens_mm,
            near=near, far=far, layers=layers, ref_index=ref_index,
            include_ground=include_ground, add_subject=add_subject, aspect=aspect)

        renderer = Renderer(scene)
        previs = renderer.render()["previs"]
        depth_rgb = depth_mod.save_depth_preview(np.asarray(dips[min(ref_index, len(dips) - 1)]))
        depth_rgb = np.ascontiguousarray(
            np.repeat(depth_rgb[:, :, :1], 3, axis=2) if depth_rgb.ndim == 3 else depth_rgb)

        save_scene(scene, os.path.join(scene_dir(scene_id), "scene.json"))
        remember_scene(unique_id, scene)
        notes.append(f"场景 {scene_id}：{len(scene['objects'])} 个对象，运镜判定「{scene['meta']['motion']}」")
        notes.append(f"资源目录：{os.path.join(OUTPUT_DIR, scene['meta']['asset_subfolder'])}")
        return (scene, to_tensor([previs]), to_tensor([depth_rgb]), "\n".join(notes))


# --------------------------------------------------------------------------- #
# 2. 导演台（交互节点）
# --------------------------------------------------------------------------- #
class DirectorStage:
    CATEGORY = NODE_CATEGORY
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "scene": ("DIRECTOR_SCENE",),
                "state": ("STRING", {"default": "", "multiline": False}),
                "lens_mm": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 200.0, "step": 0.5,
                                      "tooltip": "0 = 沿用场景焦距"}),
                "shot_count": ("INT", {"default": 4, "min": 1, "max": 9, "step": 1}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("DIRECTOR_SCENE", "STRING", "STRING")
    RETURN_NAMES = ("场景", "相机JSON", "报告")
    FUNCTION = "run"

    @classmethod
    def IS_CHANGED(cls, scene, state, lens_mm, shot_count, **kw):
        return _hash(scene, state, lens_mm, shot_count)

    def run(self, scene, state, lens_mm, shot_count, unique_id=None):
        from director3d.scene import lens_to_vfov

        scene = apply_state_safe(scene, state)
        if lens_mm and lens_mm > 0:
            scene["camera"]["lens_mm"] = float(lens_mm)
            scene["camera"]["vfov"] = lens_to_vfov(float(lens_mm))
        scene["meta"]["shot_count"] = int(shot_count)
        cam = scene["camera"]
        cam_json = json.dumps({"position": cam["position"], "target": cam["target"],
                               "lens_mm": cam["lens_mm"], "vfov": round(cam["vfov"], 2)},
                              ensure_ascii=False)
        report = (f"机位 {[round(v, 2) for v in cam['position']]} -> "
                  f"{[round(v, 2) for v in cam['target']]}，{cam['lens_mm']:.0f}mm，"
                  f"轨迹 {len(cam.get('path') or [])} 帧，对象 {len(scene['objects'])} 个")
        return (scene, cam_json, report)


def apply_state_safe(scene, state):
    from director3d.scene import normalize_scene

    try:
        scene = normalize_scene(scene)
    except Exception:
        scene = normalize_scene({})
    if not state:
        return scene
    try:
        payload = json.loads(state) if isinstance(state, str) else dict(state)
    except Exception:
        return scene
    cam = payload.get("camera") or {}
    for key in ("position", "target"):
        if isinstance(cam.get(key), (list, tuple)) and len(cam[key]) == 3:
            scene["camera"][key] = [float(x) for x in cam[key]]
    if cam.get("lens_mm"):
        from director3d.scene import lens_to_vfov

        scene["camera"]["lens_mm"] = float(cam["lens_mm"])
        scene["camera"]["vfov"] = lens_to_vfov(float(cam["lens_mm"]))
    if isinstance(payload.get("path"), list) and payload["path"]:
        scene["camera"]["path"] = payload["path"]

    patches = payload.get("objects") or {}
    if isinstance(patches, dict):
        for obj in scene["objects"]:
            patch = patches.get(obj["id"])
            if not isinstance(patch, dict):
                continue
            for key in ("position", "rotation"):
                if isinstance(patch.get(key), (list, tuple)) and len(patch[key]) == 3:
                    obj[key] = [float(x) for x in patch[key]]
            for key in ("height", "opacity"):
                if patch.get(key) is not None:
                    obj[key] = float(patch[key])
            if patch.get("pose"):
                obj["pose"] = str(patch["pose"])
            if "visible" in patch:
                obj["visible"] = bool(patch["visible"])

    removed = set(payload.get("removed") or [])
    if removed:
        scene["objects"] = [o for o in scene["objects"] if o["id"] not in removed]

    existing = {o["id"] for o in scene["objects"]}
    for extra in payload.get("added") or []:
        if not isinstance(extra, dict):
            continue
        extra = dict(extra)
        extra.setdefault("kind", "mannequin")
        extra.setdefault("position", [0.0, 0.0, 0.0])
        extra.setdefault("rotation", [0.0, 0.0, 0.0])
        extra.setdefault("height", 1.75)
        extra.setdefault("pose", "站立")
        extra["id"] = str(extra.get("id") or f"added_{len(existing)}")
        if extra["id"] not in existing:
            scene["objects"].append(extra)
            existing.add(extra["id"])
    scene["meta"]["edited"] = True
    return scene


# --------------------------------------------------------------------------- #
# 3. 预演渲染
# --------------------------------------------------------------------------- #
class DirectorPrevisRender:
    CATEGORY = NODE_CATEGORY
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "scene": ("DIRECTOR_SCENE",),
                "shot_count": ("INT", {"default": 4, "min": 1, "max": 12, "step": 1}),
                "width": ("INT", {"default": 0, "min": 0, "max": 2048, "step": 64,
                                  "tooltip": "0 = 沿用场景分辨率"}),
                "height": ("INT", {"default": 0, "min": 0, "max": 2048, "step": 64}),
                "with_pose": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "IMAGE", "IMAGE", "STRING")
    RETURN_NAMES = ("预演图", "深度图", "骨骼图", "线稿", "主体蒙版", "报告")
    FUNCTION = "run"

    @classmethod
    def IS_CHANGED(cls, scene, **kw):
        return _hash(scene, kw.get("shot_count"), kw.get("width"), kw.get("height"))

    def run(self, scene, shot_count, width, height, with_pose):
        scene = normalize_scene(scene)
        W, H = (width or scene["frame_size"][0]), (height or scene["frame_size"][1])
        renderer = Renderer(scene)
        cams = sample_cameras(scene, shot_count)
        previs, depth, poses, lineart, masks = [], [], [], [], []
        for cam in cams:
            res = renderer.render(camera=cam, width=W, height=H, with_pose=with_pose)
            previs.append(res["previs"])
            depth.append(res["depth"])
            poses.append(res["pose"])
            lineart.append(res["lineart"])
            masks.append(res["mask"])
        report = (f"渲染 {len(cams)} 个机位 @ {W}x{H}；"
                  f"对象 {len(scene['objects'])} 个；基于 {scene['meta'].get('depth_backend', '未知深度')}")
        return (to_tensor(previs), to_tensor(depth), to_tensor(poses),
                to_tensor(lineart), to_tensor(masks), report)


# --------------------------------------------------------------------------- #
# 4. H3 参考包
# --------------------------------------------------------------------------- #
class DirectorH3Reference:
    CATEGORY = NODE_CATEGORY
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "scene": ("DIRECTOR_SCENE",),
                "images": ("IMAGE", {"tooltip": "接 预演图 输出"}),
                "shot_count": ("INT", {"default": 4, "min": 1, "max": 4, "step": 1}),
                "duration": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 15.0, "step": 0.5,
                                       "tooltip": "0 = 按轨迹时长推导"}),
                "style_note": ("STRING", {"default": "", "multiline": True}),
                "subject_note": ("STRING", {"default": "", "multiline": True}),
                "extra_note": ("STRING", {"default": "", "multiline": True}),
            }
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("参考1", "参考2", "参考3", "参考4", "提示词", "运镜", "AddGuide建议")
    FUNCTION = "run"

    def run(self, scene, images, shot_count, duration, style_note, subject_note, extra_note):
        scene = normalize_scene(scene)
        imgs = to_numpy(images)
        if not imgs:
            raise RuntimeError("没有可用的预演图，请先接 预演图 输出")
        duration = duration or h3_mod.duration_from_scene(scene)
        plan = h3_mod.build_prompt(scene, shot_count=shot_count, duration=duration,
                                   style_note=style_note, subject_note=subject_note,
                                   extra=extra_note)
        refs = []
        for i in range(4):
            refs.append(imgs[min(i, len(imgs) - 1)])
        prompt = plan["prompt"]
        guide = h3_mod.guide_suggestion(plan["plan"])
        tail = (f"\n\n【机位表】共 {len(plan['plan'])} 个参考机位，"
                f"对应时间轴 {[p['time'] for p in plan['plan']]}s。")
        warn = ""
        if shot_count > 4:
            warn = "\n（H3 ref2va 最多 9 张参考图，本节点先给你 4 张，更多请复制节点或拆分镜头。）"
        return (to_tensor([refs[0]]), to_tensor([refs[1]]), to_tensor([refs[2]]),
                to_tensor([refs[3]]), prompt + tail + warn, plan["motion"], guide)


# --------------------------------------------------------------------------- #
# 5. 场景保存 / 载入
# --------------------------------------------------------------------------- #
class DirectorSceneSave:
    CATEGORY = NODE_CATEGORY
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "scene": ("DIRECTOR_SCENE",),
                "filename_prefix": ("STRING", {"default": "director_scene"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "DIRECTOR_SCENE")
    RETURN_NAMES = ("文件", "场景")
    FUNCTION = "run"
    OUTPUT_NODE = True

    def run(self, scene, filename_prefix, unique_id=None):
        scene = normalize_scene(scene)
        sid = scene["meta"].get("scene_id") or new_scene_id(filename_prefix)
        path = os.path.join(scene_dir(sid), f"{filename_prefix}.json")
        save_scene(scene, path)
        remember_scene(unique_id, scene)
        return (path, scene)


class DirectorSceneLoad:
    CATEGORY = NODE_CATEGORY
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "scene_json": ("STRING", {"default": "", "multiline": False,
                                          "tooltip": "scene.json 绝对路径"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("DIRECTOR_SCENE", "STRING")
    RETURN_NAMES = ("场景", "报告")
    FUNCTION = "run"

    def run(self, scene_json, unique_id=None):
        scene = load_scene(scene_json.strip())
        remember_scene(unique_id, scene)
        return (scene, f"载入对象 {len(scene['objects'])} 个，来源 {scene_json}")


NODE_CLASS_MAPPINGS = {
    "DirectorVideoToScene": DirectorVideoToScene,
    "DirectorStage": DirectorStage,
    "DirectorPrevisRender": DirectorPrevisRender,
    "DirectorH3Reference": DirectorH3Reference,
    "DirectorSceneSave": DirectorSceneSave,
    "DirectorSceneLoad": DirectorSceneLoad,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DirectorVideoToScene": "Alber-导演台 · 视频转3D",
    "DirectorStage": "Alber-导演台 · 场景编辑",
    "DirectorPrevisRender": "Alber-导演台 · 预演渲染",
    "DirectorH3Reference": "Alber-导演台 · H3参考包",
    "DirectorSceneSave": "Alber-导演台 · 保存场景",
    "DirectorSceneLoad": "Alber-导演台 · 载入场景",
}
