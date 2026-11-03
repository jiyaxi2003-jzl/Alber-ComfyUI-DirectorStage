"""生成一个演示用的"电影感静止帧"+ 逐像素深度，再走一遍重建流程，
产出 demo/scene.json 与 demo/assets/*.png，供 demo/director.html 直接演示。

    python demo/make_demo.py
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN)

OUT_ROOT = os.path.join(HERE, "_out")
os.environ.setdefault("COMFY_ROOT", OUT_ROOT)
os.makedirs(OUT_ROOT, exist_ok=True)

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from director3d import build  # noqa: E402
from director3d.paths import scene_dir  # noqa: E402
from director3d.video_io import VideoInfo  # noqa: E402

W, H = 1280, 720
HORIZON = int(H * 0.52)


def sky(img: Image.Image):
    arr = np.zeros((H, W, 3), dtype=np.float32)
    top = np.array([26, 38, 62], dtype=np.float32)
    mid = np.array([92, 74, 96], dtype=np.float32)
    low = np.array([224, 138, 92], dtype=np.float32)
    for y in range(H):
        t = y / H
        if t < 0.55:
            c = top * (1 - t / 0.55) + mid * (t / 0.55)
        else:
            k = min(1.0, (t - 0.55) / 0.30)
            c = mid * (1 - k) + low * k
        arr[y, :, :] = c
    img.paste(Image.fromarray(arr.astype(np.uint8)))


def sun(img: Image.Image):
    glow = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(glow)
    cx, cy, r = int(W * 0.63), int(H * 0.47), 46
    for rr, v in ((r * 6, 40), (r * 3, 70), (r * 1.6, 120), (r, 235)):
        d.ellipse([cx - rr, cy - rr * 0.9, cx + rr, cy + rr * 0.9], fill=v)
    glow = glow.filter(ImageFilter.GaussianBlur(34))
    warm = Image.new("RGB", (W, H), (255, 214, 158))
    img.paste(warm, (0, 0), glow.point(lambda v: int(v * 0.85)))


def ridge(depth_img: Image.Image, img: Image.Image, base_y: int, amp: int, color, depth, seed):
    rnd = random.Random(seed)
    pts = [(0, H)]
    x = 0
    level = base_y
    while x <= W:
        level = base_y + rnd.randint(-amp, amp) * 0.6 + int(amp * np.sin(x / 190.0 + seed) * 0.8)
        pts.append((x, level))
        x += 26
    pts.append((W, H))
    ImageDraw.Draw(img).polygon(pts, fill=color)
    ImageDraw.Draw(depth_img).polygon(pts, fill=int(round(depth * 255)))


def water(depth_img, img):
    arr = np.array(img, dtype=np.float32)
    for y in range(HORIZON, H):
        t = (y - HORIZON) / (H - HORIZON)
        c = np.array([70, 78, 104], dtype=np.float32) * (1 - t) + np.array([20, 24, 34], dtype=np.float32) * t
        arr[y, :, :] = arr[y, :, :] * 0.25 + c * 0.75
    img.paste(Image.fromarray(arr.astype(np.uint8)))
    d = ImageDraw.Draw(img)
    for i in range(70):
        y = HORIZON + int((H - HORIZON) * (i / 70.0) ** 1.7)
        x0 = random.randint(0, W - 200)
        d.line([x0, y, x0 + random.randint(60, 320), y], fill=(150, 150, 170), width=1)
    ImageDraw.Draw(depth_img).rectangle([0, HORIZON, W, H], fill=140)


def person(depth_img, img):
    cx = int(W * 0.255)
    feet = int(H * 0.87)
    hgt = int(H * 0.42)
    head_r = int(hgt * 0.075)
    neck = feet - int(hgt * 0.86)
    hip = feet - int(hgt * 0.45)
    sh = feet - int(hgt * 0.80)
    d = ImageDraw.Draw(img)
    dd = ImageDraw.Draw(depth_img)
    body = (12, 14, 18)
    d.ellipse([cx - head_r, neck - head_r * 2.1, cx + head_r, neck - head_r * 0.1], fill=body)
    dd.ellipse([cx - head_r, neck - head_r * 2.1, cx + head_r, neck - head_r * 0.1], fill=13)
    d.polygon([(cx - head_r * 1.9, sh), (cx + head_r * 1.9, sh), (cx + head_r * 1.5, hip), (cx - head_r * 1.5, hip)], fill=body)
    dd.polygon([(cx - head_r * 1.9, sh), (cx + head_r * 1.9, sh), (cx + head_r * 1.5, hip), (cx - head_r * 1.5, hip)], fill=13)
    for sx in (-1, 1):
        d.line([cx, sh + 6, cx + sx * head_r * 2.3, hip - 2], fill=body, width=8)
        d.line([cx + sx * head_r * 0.75, hip, cx + sx * head_r * 1.5, feet], fill=body, width=11)


def rocks(depth_img, img):
    d = ImageDraw.Draw(img)
    dd = ImageDraw.Draw(depth_img)
    for i in range(9):
        cx = random.randint(0, W)
        cy = random.randint(int(H * 0.86), H + 20)
        rx = random.randint(40, 150)
        ry = random.randint(18, 46)
        col = (9, 11, 14) if i % 2 else (16, 19, 24)
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=col)
        dd.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=15)


def grade(img: Image.Image) -> Image.Image:
    arr = np.array(img, dtype=np.float32) / 255.0
    arr = np.clip(arr * 1.06, 0, 1) ** 0.94
    vign = np.ones((H, W), dtype=np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    vign = np.clip(1.12 - 0.42 * r ** 2, 0.55, 1.0)
    arr *= vign[:, :, None]
    return Image.fromarray(np.clip(arr * 255, 0, 255).astype(np.uint8))


def main():
    random.seed(7)
    img = Image.new("RGB", (W, H))
    dep = Image.new("L", (W, H), 255)
    sky(img)
    sun(img)
    ridge(dep, img, int(H * 0.50), 26, (150, 132, 148), 0.88, 3)
    ridge(dep, img, int(H * 0.505), 18, (104, 96, 116), 0.80, 11)
    ridge(dep, img, int(H * 0.512), 12, (62, 60, 78), 0.72, 23)
    water(dep, img)
    person(dep, img)
    rocks(dep, img)
    img = grade(img)

    # 深度图：0=近 1=远（灰度 255 表示远，这里直接取浮点）
    dip = np.array(dep, dtype=np.float32) / 255.0
    dip[HORIZON:] = np.clip(0.42 + 0.16 * (np.arange(H - HORIZON) / (H - HORIZON))[:, None], 0, 1) * np.ones((H - HORIZON, W), np.float32)
    # 前景人物/岩石重新盖回近处（上面整段覆盖会抹掉，所以按原 dep 的暗部恢复）
    mask_near = np.array(dep, dtype=np.float32) / 255.0 < 0.2
    dip[mask_near] = 0.05

    demo_dir = HERE
    still_path = os.path.join(demo_dir, "still.png")
    img.save(still_path)

    frames = [np.array(img)]
    info = VideoInfo(path="demo/still.png", width=W, height=H, fps=24.0, duration=1.0, n_frames=1)
    scene, _ = build.reconstruct(frames, info, [dip], "演示用手工深度", scene_id="demo",
                                lens_mm=40.0, near=1.2, far=30.0, layers=3,
                                include_ground=True, add_subject=False)

    # 资源搬到 demo/assets，并改成相对文件名
    src_dir = scene_dir("demo")
    dst_dir = os.path.join(demo_dir, "assets")
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        if name.startswith("card_") or name.endswith(".png"):
            shutil.copyfile(os.path.join(src_dir, name), os.path.join(dst_dir, name))
    for obj in scene["objects"]:
        if obj["kind"] == "card":
            obj["subfolder"] = ""
    scene["meta"]["asset_subfolder"] = ""
    scene["meta"]["scene_id"] = "demo"

    # 没有自动检测到人物时，手动补一个替身，保证演示有"人"可调
    if not any(o["kind"] == "mannequin" for o in scene["objects"]):
        # 演示：把替身摆在画面里那个剪影的位置（深度取最近一层）
        cam_z = scene["camera"]["position"][2]
        scene["objects"].append({
            "id": "subject", "kind": "mannequin", "label": "主体替身",
            "position": [-1.15, 0.0, round(cam_z - 9.98, 2)], "rotation": [0.0, 16.0, 0.0],
            "height": 1.78, "pose": "站立", "opacity": 1.0,
        })
    scene["meta"]["motion"] = "固定机位"

    with open(os.path.join(demo_dir, "scene.json"), "w", encoding="utf-8") as fh:
        json.dump(scene, fh, ensure_ascii=False, indent=1)
    print(f"demo 场景已生成：{len(scene['objects'])} 个对象 -> demo/scene.json")
    for obj in scene["objects"]:
        print("  -", obj["id"], obj["kind"], obj.get("image", ""), obj.get("depth", ""))


if __name__ == "__main__":
    main()
