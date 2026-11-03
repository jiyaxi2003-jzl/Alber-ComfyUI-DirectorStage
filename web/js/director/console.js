// Alber-3D 导演台控制台：全屏 three.js 场景 + 机位/人物/光线调整，结果回写到节点 state。
import * as THREE from "../vendor/three.module.js";
import { OrbitControls } from "../vendor/OrbitControls.js";
import { TransformControls } from "../vendor/TransformControls.js";
import {
  buildMannequin, refreshMannequin, buildCameraRig, buildTargetHandle,
  applyCamera, POSES,
} from "./rig.js";

const CSS_HREF = new URL("../../css/director.css", import.meta.url).href;

function ensureCss() {
  if (document.querySelector("link[data-dstage]")) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = CSS_HREF;
  link.dataset.dstage = "1";
  document.head.appendChild(link);
}

const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const deg = (r) => (r * 180) / Math.PI;

function texUrl(obj, scene, assetBase) {
  if (assetBase) return `${assetBase.replace(/\/$/, "")}/${obj.image}`;
  const sub = obj.subfolder || (scene.meta && scene.meta.asset_subfolder) || "";
  const q = new URLSearchParams({ filename: obj.image, subfolder: sub, type: "output" });
  return `/view?${q.toString()}`;
}

// 面板里的画面需要能被截图导出，渲染器始终保留帧缓冲（见 WebGLRenderer 参数）。

export function openDirectorConsole({ scene, onSave, onClose, assetBase }) {
  ensureCss();
  const state = {
    scene,
    selected: null,
    mode: "orbit",
    // 默认按机位看：打开就是「重建出来的那一帧」，取消勾选再自由环绕看分层
    lockView: true,
    added: [],
    removed: [],
    patches: {},
    dirty: false,
  };

  // ---------------- DOM ----------------
  const root = document.createElement("div");
  root.className = "ds-overlay";
  root.innerHTML = `
  <div class="ds-window">
    <header class="ds-head">
      <div class="ds-title">Alber-3D 导演台<span class="ds-sub" id="ds-scene-id"></span></div>
      <div class="ds-actions">
        <label class="ds-check"><input type="checkbox" id="ds-lock"> 机位视角</label>
        <button class="ds-btn" id="ds-reset">重置视角</button>
        <button class="ds-btn ds-primary" id="ds-save">保存到节点</button>
        <button class="ds-btn ds-close" id="ds-close">×</button>
      </div>
    </header>
    <div class="ds-body">
      <aside class="ds-left">
        <div class="ds-tabs" id="ds-modes">
          <button data-mode="orbit" class="on">环绕</button>
          <button data-mode="camera">机位</button>
          <button data-mode="object">物体</button>
        </div>
        <section class="ds-panel">
          <h4>镜头</h4>
          <div class="ds-field"><label>焦距</label>
            <input type="range" id="ds-lens" min="12" max="135" step="1">
            <output id="ds-lensv"></output>
          </div>
          <div class="ds-field"><label>视场</label><output id="ds-fov"></output></div>
          <div class="ds-grid" id="ds-campos">
            <label>X</label><input type="number" step="0.05" data-cam="position.0">
            <label>Y</label><input type="number" step="0.05" data-cam="position.1">
            <label>Z</label><input type="number" step="0.05" data-cam="position.2">
            <label>看X</label><input type="number" step="0.05" data-cam="target.0">
            <label>看Y</label><input type="number" step="0.05" data-cam="target.1">
            <label>看Z</label><input type="number" step="0.05" data-cam="target.2">
          </div>
          <div class="ds-row">
            <button class="ds-btn" id="ds-pick">取当前视角为机位</button>
            <button class="ds-btn" id="ds-addkf">加关键帧</button>
          </div>
        </section>
        <section class="ds-panel ds-grow">
          <h4>对象 <span class="ds-hint" id="ds-count"></span></h4>
          <ul class="ds-list" id="ds-objects"></ul>
          <div class="ds-row">
            <button class="ds-btn" id="ds-addman">+ 人物替身</button>
          </div>
        </section>
      </aside>
      <main class="ds-viewport" id="ds-view">
        <div class="ds-hud" id="ds-hud"></div>
        <div class="ds-loading" id="ds-loading">正在载入场景…</div>
      </main>
      <aside class="ds-right" id="ds-inspector">
        <h4>属性</h4>
        <p class="ds-empty">在画面里点选物体，或从左侧列表选择</p>
      </aside>
    </div>
    <footer class="ds-timeline">
      <div class="ds-tl-head"><span>机位轨迹</span><button class="ds-btn ds-mini" id="ds-play">播放</button></div>
      <div class="ds-tl-track" id="ds-track"></div>
    </footer>
  </div>`;
  document.body.appendChild(root);

  const $ = (id) => root.querySelector("#" + id);
  $("ds-scene-id").textContent = (scene.meta && scene.meta.scene_id) || "";
  const view = $("ds-view");

  // ---------------- three 基础 ----------------
  // preserveDrawingBuffer 常开：面板里的画面要能被截图/导出（含无头截图验证），
  // 场景基本是静态的，这点开销可以忽略。
  const renderer = new THREE.WebGLRenderer({
    antialias: true, powerPreference: "high-performance", preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x0b0d10, 1);
  view.appendChild(renderer.domElement);

  const world = new THREE.Scene();
  world.background = new THREE.Color(0x0b0d10);

  const viewCam = new THREE.PerspectiveCamera(45, 16 / 9, 0.05, 500);
  const orbit = new OrbitControls(viewCam, renderer.domElement);
  orbit.enableDamping = true;
  orbit.dampingFactor = 0.08;
  orbit.maxPolarAngle = Math.PI * 0.495;

  world.add(new THREE.HemisphereLight(0xbcd4ff, 0x1a1d22, 1.05));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(6, 10, 8);
  world.add(key);
  const fill = new THREE.DirectionalLight(0x9fc4ff, 0.5);
  fill.position.set(-8, 4, -6);
  world.add(fill);

  const transform = new TransformControls(viewCam, renderer.domElement);
  transform.setSize(0.9);
  world.add(transform.getHelper());
  transform.addEventListener("dragging-changed", (e) => { orbit.enabled = !e.value; });
  transform.addEventListener("objectChange", () => {
    state.dirty = true;
    if (transform.object === shotTarget) syncShotFromTarget();
    if (transform.object === shotRig.group) syncShotFromRig();
    paintCameraInputs();
    if (state.selected) paintInspector();
  });

  // ---------------- 机位 ----------------
  const shotCam = new THREE.PerspectiveCamera(40, 16 / 9, 0.05, 500);
  const shotRig = buildCameraRig();
  const shotTarget = buildTargetHandle();
  world.add(shotRig.group);
  world.add(shotTarget);
  const frustumAspect = () => (scene.frame_size[0] / scene.frame_size[1]) || 16 / 9;

  function syncRigPose() {
    const p = shotCam.position;
    shotRig.group.position.copy(p);
    shotRig.group.lookAt(shotTarget.position);
    shotRig.update(shotCam.fov, frustumAspect());
  }
  function syncShotFromTarget() {
    shotCam.lookAt(shotTarget.position);
    syncRigPose();
  }
  function syncShotFromRig() {
    shotCam.position.copy(shotRig.group.position);
    shotCam.lookAt(shotTarget.position);
  }

  function readCamera() {
    return {
      position: shotCam.position.toArray().map((v) => +v.toFixed(3)),
      target: shotTarget.position.toArray().map((v) => +v.toFixed(3)),
      lens_mm: Math.round(lensMm * 10) / 10,
      vfov: +deg(shotCam.fov).toFixed(2),
    };
  }

  // ---------------- 场景内容 ----------------
  const pickables = [];
  const objects = new Map(); // id -> {data, mesh}
  const outlines = []; // 卡片描边：自由视角下用来看清分层
  let lensMm = Number(scene.camera.lens_mm || 35);

  function registerObject(data, mesh) {
    mesh.userData.objectId = data.id;
    mesh.userData.kind = data.kind;
    objects.set(data.id, { data, mesh });
    pickables.push(mesh);
  }

  const loader = new THREE.TextureLoader();
  loader.setCrossOrigin("anonymous");
  let pending = 0;
  function loadCard(data) {
    if (!data.image) return;
    pending++;
    loader.load(
      texUrl(data, scene, assetBase),
      (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace;
        const geo = new THREE.PlaneGeometry(tex.image.width, tex.image.height);
        const mat = new THREE.MeshBasicMaterial({
          map: tex, transparent: true, opacity: data.opacity ?? 1,
          side: THREE.DoubleSide, depthWrite: false, toneMapped: false, fog: false,
        });
        const mesh = new THREE.Mesh(geo, mat);
        mesh.userData.kind = data.kind || "card";
        // 一层薄描边：机位视角关掉，自由视角下能一眼看出"这是几块立起来的板子"
        const outline = new THREE.LineSegments(
          new THREE.EdgesGeometry(geo),
          new THREE.LineBasicMaterial({
            color: 0x4aa3ff, transparent: true, opacity: 0.4, depthWrite: false, fog: false,
          })
        );
        outline.renderOrder = 3;
        outline.visible = !state.lockView;
        mesh.add(outline);
        outlines.push(outline);
        applyTransform(mesh, data);
        mesh.renderOrder = 1;
        world.add(mesh);
        registerObject(data, mesh);
        pending--;
        paintList();
        if (pending === 0) $("ds-loading").style.display = "none";
      },
      undefined,
      () => {
        pending--;
        if (pending === 0) $("ds-loading").style.display = "none";
      }
    );
  }

  function applyTransform(mesh, data) {
    const p = data.position || [0, 0, 0];
    const r = data.rotation || [0, 0, 0];
    const s = data.scale || [1, 1, 1];
    mesh.position.set(p[0], p[1], p[2]);
    mesh.rotation.order = "YXZ";
    mesh.rotation.set(THREE.MathUtils.degToRad(r[0]), THREE.MathUtils.degToRad(r[1]), THREE.MathUtils.degToRad(r[2]));
    if (mesh.userData.kind === "card") mesh.scale.set(s[0], s[1], s[2] || 1);
    else mesh.scale.setScalar(1);
  }

  let groundGroup = null;
  function buildGround(data) {
    const size = Math.max(24, Number(data.size || 30));
    groundGroup = new THREE.Group();
    const plane = new THREE.Mesh(
      new THREE.PlaneGeometry(size, size, 1, 1),
      new THREE.MeshStandardMaterial({ color: 0x2a2f38, roughness: 0.95, metalness: 0.0 })
    );
    plane.rotation.x = -Math.PI / 2;
    plane.position.set(data.position[0], data.position[1], data.position[2]);
    groundGroup.add(plane);
    const grid = new THREE.GridHelper(size, Math.round(size), 0x3d4550, 0x262b33);
    grid.position.copy(plane.position);
    grid.position.y += 0.002;
    groundGroup.add(grid);
    world.add(groundGroup);
    pickables.push(plane);
    plane.userData.objectId = data.id;
    plane.userData.kind = "plane";
    objects.set(data.id, { data, mesh: plane });
  }

  // 先铺场景里已有的对象
  (scene.objects || []).forEach((obj) => {
    if (obj.visible === false) return;
    if (obj.kind === "card") loadCard(obj);
    else if (obj.kind === "mannequin") {
      const mesh = buildMannequin(obj);
      world.add(mesh);
      registerObject(obj, mesh);
    } else if (obj.kind === "plane") buildGround(obj);
  });
  if (!(scene.objects || []).some((o) => o.kind === "plane")) {
    buildGround({ id: "ground_auto", kind: "plane", size: 40, position: [0, 0, 0], color: [58, 62, 72] });
  }
  if (pending === 0) $("ds-loading").style.display = "none";

  // 初始机位
  applyCamera(shotCam, scene.camera);
  shotTarget.position.set(...(scene.camera.target || [0, 1.4, 0]));
  shotCam.lookAt(shotTarget.position);
  shotRig.update(shotCam.fov, frustumAspect());

  // 自由环绕时的落点：机位斜后方，能看出分层但不至于穿帮得太厉害
  viewCam.position.copy(shotCam.position).add(new THREE.Vector3(5.0, 3.0, 6.0));
  orbit.target.copy(shotTarget.position);
  viewCam.far = 500;
  viewCam.updateProjectionMatrix();

  // ---------------- 交互 ----------------
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let downPos = null;

  renderer.domElement.addEventListener("pointerdown", (e) => { downPos = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (!downPos) return;
    const moved = Math.hypot(e.clientX - downPos[0], e.clientY - downPos[1]);
    downPos = null;
    if (moved > 4 || transform.dragging) return;
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, viewCam);
    const hits = raycaster.intersectObjects(pickables.filter((m) => m.visible !== false), true);
    if (hits.length) {
      let target = hits[0].object;
      while (target && !target.userData.objectId) target = target.parent;
      if (target && target.userData.objectId) select(target.userData.objectId);
    } else if (state.mode === "object") select(null);
  });

  function select(id) {
    state.selected = id;
    paintList();
    paintInspector();
    if (state.mode === "object" && id && objects.get(id)) {
      transform.attach(objects.get(id).mesh);
    } else if (state.mode !== "camera") {
      transform.detach();
    }
  }

  // ---------------- 面板绘制 ----------------
  const trackEl = $("ds-track");
  function paintList() {
    const list = $("ds-objects");
    list.innerHTML = "";
    const items = [];
    objects.forEach(({ data }) => items.push(data));
    items.sort((a, b) => (a.kind === "card" ? 1 : 0) - (b.kind === "card" ? 1 : 0));
    items.forEach((data) => {
      const li = document.createElement("li");
      li.className = data.id === state.selected ? "on" : "";
      const tag = data.kind === "mannequin" ? "人物" : data.kind === "card" ? "图层" : "地面";
      li.innerHTML = `<span class="ds-tag">${tag}</span><span class="ds-name">${data.label || data.id}</span>`;
      const eye = document.createElement("button");
      eye.className = "ds-eye";
      eye.textContent = data.visible === false ? "○" : "●";
      eye.title = "显示/隐藏";
      eye.onclick = (ev) => {
        ev.stopPropagation();
        data.visible = data.visible === false;
        const entry = objects.get(data.id);
        if (entry) entry.mesh.visible = data.visible;
        state.patches[data.id] = { ...(state.patches[data.id] || {}), visible: data.visible };
        state.dirty = true;
        paintList();
      };
      li.appendChild(eye);
      li.onclick = () => select(data.id);
      list.appendChild(li);
    });
    $("ds-count").textContent = `${items.length} 个`;
  }

  function paintInspector() {
    const box = $("ds-inspector");
    const entry = state.selected && objects.get(state.selected);
    if (!entry) {
      box.innerHTML = `<h4>属性</h4><p class="ds-empty">在画面里点选物体，或从左侧列表选择</p>`;
      return;
    }
    const d = entry.data;
    box.innerHTML = `
      <h4>${d.label || d.id}</h4>
      <div class="ds-grid">
        <label>X</label><input type="number" step="0.05" data-t="pos0" value="${d.position[0].toFixed(2)}">
        <label>Y</label><input type="number" step="0.05" data-t="pos1" value="${d.position[1].toFixed(2)}">
        <label>Z</label><input type="number" step="0.05" data-t="pos2" value="${d.position[2].toFixed(2)}">
      </div>
      <div class="ds-grid">
        <label>朝向</label><input type="number" step="5" data-t="yaw" value="${(d.rotation?.[1] || 0).toFixed(0)}">
        <label>不透明</label><input type="number" min="0" max="1" step="0.05" data-t="opacity" value="${(d.opacity ?? 1).toFixed(2)}">
      </div>
      ${d.kind === "mannequin" ? `
      <div class="ds-grid">
        <label>身高</label><input type="number" min="1" max="2.1" step="0.02" data-t="height" value="${(d.height || 1.75).toFixed(2)}">
        <label>姿势</label>
        <select data-t="pose">${POSES.map((p) => `<option ${p === (d.pose || "站立") ? "selected" : ""}>${p}</option>`).join("")}</select>
      </div>` : ""}
      <div class="ds-row">
        <button class="ds-btn" data-act="facecam">面朝机位</button>
        <button class="ds-btn" data-act="dup">复制</button>
      </div>
      <div class="ds-row">
        <button class="ds-btn ds-danger" data-act="del">删除</button>
      </div>
      <p class="ds-tip">${d.kind === "card" ? "图层是照片按深度切开的一层，位置决定它参与视差的方式。" : "替身只用于构图与蒙版，不会被渲染成真人。"}</p>`;

    box.querySelectorAll("input,select").forEach((el) => {
      el.onchange = () => {
        const t = el.dataset.t;
        if (t === "pos0" || t === "pos1" || t === "pos2") {
          const i = +t.slice(-1);
          d.position[i] = +el.value;
          applyTransform(entry.mesh, d);
          if (d.kind === "mannequin") refreshMannequin(entry.mesh, d);
        } else if (t === "yaw") {
          d.rotation = d.rotation || [0, 0, 0];
          d.rotation[1] = +el.value;
          applyTransform(entry.mesh, d);
        } else if (t === "opacity") {
          d.opacity = +el.value;
          if (entry.mesh.material) entry.mesh.material.opacity = d.opacity;
        } else if (t === "height") {
          d.height = +el.value;
          refreshMannequin(entry.mesh, d);
        } else if (t === "pose") {
          d.pose = el.value;
          refreshMannequin(entry.mesh, d);
        }
        markPatched(d);
      };
    });

    box.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.onclick = () => {
        const act = btn.dataset.act;
        if (act === "del") {
          entry.mesh.visible = false;
          world.remove(entry.mesh);
          objects.delete(d.id);
          pickables.splice(pickables.indexOf(entry.mesh), 1);
          state.removed.push(d.id);
          state.selected = null;
          transform.detach();
          paintList();
          paintInspector();
        } else if (act === "dup") {
          const copy = JSON.parse(JSON.stringify(d));
          copy.id = `added_${Date.now().toString(36)}`;
          copy.position = [d.position[0] + 0.8, d.position[1], d.position[2] + 0.4];
          addObject(copy);
        } else if (act === "facecam") {
          const dx = shotCam.position.x - d.position[0];
          const dz = shotCam.position.z - d.position[2];
          d.rotation = d.rotation || [0, 0, 0];
          d.rotation[1] = deg(Math.atan2(dx, dz));
          applyTransform(entry.mesh, d);
          markPatched(d);
          paintInspector();
        }
      };
    });
  }

  function markPatched(d) {
    const p = state.patches[d.id] || {};
    p.position = d.position;
    p.rotation = d.rotation;
    p.opacity = d.opacity;
    p.height = d.height;
    p.pose = d.pose;
    p.visible = d.visible !== false;
    state.patches[d.id] = p;
    state.dirty = true;
  }

  function addObject(data) {
    const mesh = buildMannequin(data);
    world.add(mesh);
    registerObject(data, mesh);
    state.added.push(data);
    state.dirty = true;
    select(data.id);
    paintList();
  }

  $("ds-addman").onclick = () => {
    const dir = new THREE.Vector3();
    viewCam.getWorldDirection(dir);
    const base = viewCam.position.clone().addScaledVector(dir, 4.5);
    base.y = 0;
    addObject({
      id: `added_${Date.now().toString(36)}`,
      kind: "mannequin",
      label: `替身 ${objects.size}`,
      position: base.toArray().map((v) => +v.toFixed(2)),
      rotation: [0, deg(Math.atan2(shotCam.position.x - base.x, shotCam.position.z - base.z)), 0],
      height: 1.75,
      pose: "站立",
      opacity: 1,
    });
  };

  // 镜头面板
  const lensEl = $("ds-lens");
  function fovFromLens(lens) { return deg(2 * Math.atan(24 / (2 * Math.max(lens, 1)))); }
  function paintCameraInputs() {
    lensEl.value = lensMm;
    $("ds-lensv").textContent = `${Math.round(lensMm)}mm`;
    $("ds-fov").textContent = `${shotCam.fov.toFixed(1)}° · ${frustumAspect().toFixed(2)}:1`;
    root.querySelectorAll("[data-cam]").forEach((el) => {
      const [key, idx] = el.dataset.cam.split(".");
      const v = key === "position" ? shotCam.position.getComponent(+idx) : shotTarget.position.getComponent(+idx);
      el.value = v.toFixed(2);
    });
    if (state.lockView) {
      viewCam.position.copy(shotCam.position);
      viewCam.quaternion.copy(shotCam.quaternion);
      viewCam.fov = shotCam.fov;
      viewCam.updateProjectionMatrix();
    }
  }
  lensEl.oninput = () => {
    lensMm = +lensEl.value;
    shotCam.fov = fovFromLens(lensMm);
    shotCam.updateProjectionMatrix();
    syncRigPose();
    paintCameraInputs();
    state.dirty = true;
  };
  root.querySelectorAll("[data-cam]").forEach((el) => {
    el.onchange = () => {
      const [key, idx] = el.dataset.cam.split(".");
      const v = +el.value;
      if (key === "position") {
        const arr = shotCam.position.toArray();
        arr[+idx] = v;
        shotCam.position.set(...arr);
      } else {
        const arr = shotTarget.position.toArray();
        arr[+idx] = v;
        shotTarget.position.set(...arr);
      }
      shotCam.lookAt(shotTarget.position);
      syncRigPose();
      paintCameraInputs();
      state.dirty = true;
    };
  });
  $("ds-pick").onclick = () => {
    const dir = new THREE.Vector3();
    viewCam.getWorldDirection(dir);
    shotCam.position.copy(viewCam.position);
    shotTarget.position.copy(viewCam.position.clone().addScaledVector(dir, 3));
    shotCam.lookAt(shotTarget.position);
    syncRigPose();
    paintCameraInputs();
    state.dirty = true;
  };
  $("ds-lock").onchange = (e) => {
    state.lockView = e.target.checked;
    orbit.enabled = !state.lockView;
    if (state.lockView) {
      paintCameraInputs();
    } else {
      // 解锁时退到机位斜后方，能一眼看出"这是一摞立起来的板子"
      viewCam.position.copy(shotCam.position).add(new THREE.Vector3(5.0, 3.0, 6.0));
      orbit.target.copy(shotTarget.position);
      orbit.update();
    }
    outlines.forEach((o) => { o.visible = !state.lockView; });
    updateHud();
  };
  $("ds-reset").onclick = () => {
    applyCamera(shotCam, scene.camera);
    shotTarget.position.set(...(scene.camera.target || [0, 1.4, 0]));
    shotCam.lookAt(shotTarget.position);
    lensMm = Number(scene.camera.lens_mm || 35);
    lensEl.value = lensMm;
    shotCam.fov = fovFromLens(lensMm);
    shotCam.updateProjectionMatrix();
    syncRigPose();
    viewCam.position.copy(shotCam.position).add(new THREE.Vector3(2.4, 1.6, 2.2));
    orbit.target.copy(shotTarget.position);
    paintCameraInputs();
  };

  // 底部提示：机位视角优先，其次是当前模式的操作说明
  function updateHud() {
    if (state.lockView) {
      $("ds-hud").textContent = "机位视角：所见即预演 · 取消勾选可自由环绕看分层";
      return;
    }
    $("ds-hud").textContent = {
      orbit: "拖动旋转 · 滚轮缩放 · 右键平移",
      camera: "拖橙色球改看向点；镜头位置用左侧数值或「取当前视角」",
      object: "拖轴移动物体 · W 移动 / E 旋转 / R 缩放",
    }[state.mode] || "";
  }

  // 模式切换
  root.querySelectorAll("#ds-modes button").forEach((btn) => {
    btn.onclick = () => {
      state.mode = btn.dataset.mode;
      root.querySelectorAll("#ds-modes button").forEach((b) => b.classList.toggle("on", b === btn));
      transform.detach();
      if (state.mode === "camera") transform.attach(shotTarget);
      else if (state.mode === "object" && state.selected && objects.get(state.selected)) {
        transform.attach(objects.get(state.selected).mesh);
      }
      updateHud();
    };
  });
  updateHud();
  window.addEventListener("keydown", onKey);
  function onKey(e) {
    if (e.key === "Escape") close();
    if (e.key === "w") transform.setMode("translate");
    if (e.key === "e") transform.setMode("rotate");
    if (e.key === "r") transform.setMode("scale");
  }

  // 时间轴
  let path = (scene.camera.path || []).map((p) => ({ ...p }));
  let playing = false;
  function paintTrack() {
    trackEl.innerHTML = "";
    if (!path.length) {
      trackEl.innerHTML = `<span class="ds-empty">还没有轨迹关键帧，点「加关键帧」把当前机位钉到时间轴</span>`;
      return;
    }
    path.forEach((k, i) => {
      const b = document.createElement("button");
      b.className = "ds-kf";
      b.innerHTML = `<b>${i + 1}</b><span>${(k.t || 0).toFixed(1)}s</span>`;
      b.onclick = () => {
        shotCam.position.set(...k.position);
        shotTarget.position.set(...(k.target || [0, 1.4, 0]));
        shotCam.lookAt(shotTarget.position);
        syncRigPose();
        paintCameraInputs();
      };
      trackEl.appendChild(b);
    });
  }
  $("ds-addkf").onclick = () => {
    const t = path.length ? (path[path.length - 1].t || 0) + 0.5 : 0;
    path.push({ t, position: shotCam.position.toArray().map((v) => +v.toFixed(3)), target: shotTarget.position.toArray() });
    paintTrack();
    state.dirty = true;
  };
  $("ds-play").onclick = () => {
    playing = !playing;
    $("ds-play").textContent = playing ? "停止" : "播放";
    if (!playing) return;
    let i = 0;
    const step = () => {
      if (!playing) return;
      const k = path[Math.min(i, path.length - 1)];
      if (k) {
        shotCam.position.set(...k.position);
        shotTarget.position.set(...(k.target || [0, 1.4, 0]));
        shotCam.lookAt(shotTarget.position);
        syncRigPose();
        paintCameraInputs();
      }
      i++;
      if (i < path.length) setTimeout(step, 420);
      else { playing = false; $("ds-play").textContent = "播放"; }
    };
    step();
  };
  paintTrack();
  paintList();
  paintCameraInputs();
  $("ds-lock").checked = state.lockView;
  orbit.enabled = !state.lockView;

  // ---------------- 循环 ----------------
  let raf = 0;
  function resize() {
    const w = view.clientWidth || 800;
    const h = view.clientHeight || 500;
    renderer.setSize(w, h, false);
    viewCam.aspect = w / h;
    viewCam.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(view);
  resize();

  function tick() {
    raf = requestAnimationFrame(tick);
    if (state.lockView) {
      viewCam.position.copy(shotCam.position);
      viewCam.quaternion.copy(shotCam.quaternion);
      viewCam.fov = shotCam.fov;
      viewCam.updateProjectionMatrix();
    } else {
      orbit.update();
    }
    renderer.render(world, viewCam);
  }
  tick();

  // ---------------- 保存 / 关闭 ----------------
  function collect() {
    const cam = readCamera();
    const patches = {};
    objects.forEach(({ data }) => {
      patches[data.id] = {
        position: data.position, rotation: data.rotation || [0, 0, 0],
        opacity: data.opacity ?? 1, visible: data.visible !== false,
        ...(data.kind === "mannequin" ? { height: data.height, pose: data.pose } : {}),
      };
    });
    Object.assign(patches, state.patches);
    return JSON.stringify({
      camera: cam,
      objects: patches,
      added: state.added,
      removed: state.removed,
      path,
      shot_count: Math.max(1, Math.min(9, path.length || 4)),
      aspect: `${scene.frame_size[0]}:${scene.frame_size[1]}`,
      saved_at: new Date().toISOString(),
    });
  }

  $("ds-save").onclick = () => {
    const payload = collect();
    onSave && onSave(payload);
    $("ds-save").textContent = "已保存 ✓";
    setTimeout(() => { $("ds-save").textContent = "保存到节点"; }, 1200);
    state.dirty = false;
  };
  $("ds-close").onclick = () => close();


  function close() {
    cancelAnimationFrame(raf);
    ro.disconnect();
    window.removeEventListener("keydown", onKey);
    orbit.dispose();
    transform.dispose();
    renderer.dispose();
    root.remove();
    onClose && onClose(state.dirty);
  }

  window.__ds = { world, viewCam, shotCam, shotTarget, renderer, objects, state, collect };
  return { close, collect };
}
