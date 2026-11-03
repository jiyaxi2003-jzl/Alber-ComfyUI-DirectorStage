"""端到端冒烟测试：视频 -> 深度 -> 场景 -> 渲染输出。

用法：
    python tests/smoke_pipeline.py /path/to/video.mp4 [--backend ground|model|auto]
不依赖 ComfyUI 运行时，只依赖 numpy/opencv/torch。
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN)

OUT = os.environ.setdefault("DSTAGE_OUT", os.path.join(os.environ.get("TEMP", "/tmp"), "dstage"))
os.environ.setdefault("COMFY_ROOT", OUT)
os.makedirs(OUT, exist_ok=True)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from director3d import build, depth, raster, video_io  # noqa: E402


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUT, "test.mp4")
    backend = "auto"
    if "--backend" in sys.argv:
        backend = sys.argv[sys.argv.index("--backend") + 1]

    t0 = time.time()
    frames, info = video_io.read_frames(video, max_frames=12, long_side=768)
    assert frames, "没有解码出帧"
    print(f"[1] 解码 {len(frames)} 帧 {info.width}x{info.height} @ {info.fps:.1f}fps "
          f"({time.time() - t0:.1f}s)")

    t0 = time.time()
    dips, note = depth.estimate(frames, backend=backend)
    print(f"[2] 深度 {len(dips)} 张 / {note} ({time.time() - t0:.1f}s) "
          f"range={float(np.min(dips[0])):.2f}..{float(np.max(dips[0])):.2f}")

    t0 = time.time()
    scene, diag = build.reconstruct(frames, info, dips, note, scene_id="smoke")
    print(f"[3] 场景对象 {len(scene['objects'])} 个 / 轨迹 {len(scene['camera']['path'])} 帧 "
          f"/ 运镜「{scene['meta']['motion']}」 ({time.time() - t0:.1f}s)")

    t0 = time.time()
    renderer = raster.Renderer(scene)
    res = renderer.render()
    print(f"[4] 渲染 {res['previs'].shape} ({time.time() - t0:.1f}s)")

    for key in ("previs", "depth", "pose", "mask", "lineart", "normal"):
        path = os.path.join(OUT, f"out_{key}.png")
        cv2.imwrite(path, res[key][:, :, ::-1])
    print(f"[5] 输出目录 {OUT}")
    print(json_dump(scene))


def json_dump(scene):
    import json

    brief = {
        "camera": scene["camera"]["position"],
        "lens_mm": scene["camera"]["lens_mm"],
        "objects": [(o["id"], o["kind"], o.get("depth")) for o in scene["objects"]],
        "track": scene["meta"].get("camera_track", {}).get("method"),
    }
    return json.dumps(brief, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
