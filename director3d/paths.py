"""ComfyUI 路径解析（在 ComfyUI 外也能独立跑，便于单测）。"""
from __future__ import annotations

import hashlib
import os
import time

try:  # 在 ComfyUI 进程内
    import folder_paths

    OUTPUT_DIR = folder_paths.get_output_directory()
    INPUT_DIR = folder_paths.get_input_directory()
    MODELS_DIR = getattr(folder_paths, "models_dir", os.path.join(os.path.dirname(OUTPUT_DIR), "models"))
except Exception:  # 单测 / 独立脚本
    _root = os.environ.get("COMFY_ROOT", os.path.expanduser("~"))
    OUTPUT_DIR = os.path.join(_root, "output")
    INPUT_DIR = os.path.join(_root, "input")
    MODELS_DIR = os.path.join(_root, "models")

ASSET_ROOT = "director_stage"  # output 下的子目录


def scene_dir(scene_id: str, create: bool = True) -> str:
    path = os.path.join(OUTPUT_DIR, ASSET_ROOT, scene_id)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def new_scene_id(tag: str = "scene") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.md5(f"{stamp}-{time.time()}".encode()).hexdigest()[:4]
    return f"{tag}-{stamp}-{digest}"


def view_url(filename: str, scene_id: str, kind: str = "output") -> str:
    """给前端用的相对 URL，前端会直接拼到 location.origin 后面。"""
    from urllib.parse import quote

    sub = f"{ASSET_ROOT}/{scene_id}"
    return f"/view?filename={quote(filename)}&subfolder={quote(sub)}&type={kind}"


def input_video_path(name: str) -> str:
    """接受 绝对路径 / input 目录下的文件名。"""
    if not name:
        return ""
    name = name.strip().strip('"').strip("'")
    if os.path.isabs(name) and os.path.exists(name):
        return name
    for base in (INPUT_DIR, OUTPUT_DIR):
        candidate = os.path.join(base, name)
        if os.path.exists(candidate):
            return candidate
    return name
