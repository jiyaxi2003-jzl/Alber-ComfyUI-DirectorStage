// 人物替身与机位模型的 three.js 实现。
// 顶点表与 Python 端 director3d/pose.py 严格一致，保证"控制台所见"与"后端预演"一致。
import * as THREE from "../vendor/three.module.js";

export const JOINT_BASE = [
  [0.0, 1.64, 0.02], // 0 鼻
  [0.0, 1.44, 0.0], // 1 颈
  [-0.18, 1.4, 0.0], // 2 右肩
  [-0.25, 1.13, 0.02], // 3 右肘
  [-0.27, 0.88, 0.03], // 4 右腕
  [0.18, 1.4, 0.0], // 5 左肩
  [0.25, 1.13, 0.02], // 6 左肘
  [0.27, 0.88, 0.03], // 7 左腕
  [-0.11, 0.93, 0.0], // 8 右髋
  [-0.12, 0.51, 0.01], // 9 右膝
  [-0.12, 0.08, 0.0], // 10 右踝
  [0.11, 0.93, 0.0], // 11 左髋
  [0.12, 0.51, 0.01], // 12 左膝
  [0.12, 0.08, 0.0], // 13 左踝
  [-0.04, 1.68, 0.07], // 14 右眼
  [0.04, 1.68, 0.07], // 15 左眼
  [-0.085, 1.66, 0.0], // 16 右耳
  [0.085, 1.66, 0.0], // 17 左耳
];

export const LIMBS = [
  [1, 2], [1, 5], [2, 3], [3, 4], [5, 6], [6, 7], [1, 8], [8, 9], [9, 10],
  [1, 11], [11, 12], [12, 13], [1, 0], [0, 14], [14, 16], [0, 15], [15, 17],
  [2, 16], [5, 17],
];

export const POSES = ["站立", "行走", "奔跑", "下蹲", "坐姿", "举手", "指向"];

export function jointsLocal(mannequin) {
  const height = Number(mannequin.height || 1.75);
  const pose = mannequin.pose || "站立";
  const j = JOINT_BASE.map((p) => p.slice());
  const h1 = height / 1.75;

  if (pose === "行走" || pose === "奔跑") {
    const amp = pose === "行走" ? 0.28 : 0.52;
    j[9][2] += amp * 0.6; j[10][2] += amp * 0.9;
    j[12][2] -= amp * 0.6; j[13][2] -= amp * 0.9;
    j[9][1] += amp * 0.1; j[3][2] -= amp * 0.5; j[6][2] += amp * 0.5;
    j[3][1] += 0.02; j[6][1] += 0.02;
    if (pose === "奔跑") { j[1][2] += 0.06; j[0][2] += 0.06; }
  } else if (pose === "下蹲") {
    j[1][1] -= 0.36; j[0][1] -= 0.36;
    for (let i = 8; i < 14; i++) j[i][1] -= 0.34;
    j[9][2] += 0.3; j[12][2] += 0.3; j[3][1] -= 0.1; j[6][1] -= 0.1;
  } else if (pose === "坐姿") {
    j[1][1] -= 0.32; j[0][1] -= 0.32;
    for (let i = 8; i < 14; i++) { j[i][1] -= 0.32; j[i][2] += 0.12; }
    j[9][2] += 0.38; j[12][2] += 0.38; j[10][1] -= 0.3; j[13][1] -= 0.3;
  } else if (pose === "举手") {
    j[3] = [-0.24, 1.52, 0.01]; j[4] = [-0.26, 1.76, 0.02];
    j[6] = [0.24, 1.52, 0.01]; j[7] = [0.26, 1.76, 0.02];
  } else if (pose === "指向") {
    j[3] = [-0.23, 1.42, 0.06]; j[4] = [-0.21, 1.38, 0.42];
    j[6] = [0.27, 1.18, 0.02]; j[7] = [0.28, 0.93, 0.03];
  }
  return j.map((p) => [p[0] * h1, p[1] * h1, p[2] * h1]);
}

const _v = new THREE.Vector3();

function capsuleBetween(a, b, radius, material) {
  const dir = new THREE.Vector3().subVectors(b, a);
  const len = dir.length();
  const geo = new THREE.CapsuleGeometry(radius, Math.max(len, 0.01), 4, 8);
  const mesh = new THREE.Mesh(geo, material);
  mesh.position.copy(a).addScaledVector(dir, 0.5);
  mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.normalize());
  return mesh;
}

/** 构建人物替身：每根骨头一个胶囊，头一个球 */
export function buildMannequin(m, material) {
  const group = new THREE.Group();
  group.userData.kind = "mannequin";
  const mat = material || new THREE.MeshStandardMaterial({ color: 0xe8e4dc, roughness: 0.72, metalness: 0.05 });
  const joints = jointsLocal(m).map((p) => new THREE.Vector3(...p));
  const radius = Math.max(0.045, Number(m.height || 1.75) * 0.036);

  LIMBS.forEach(([a, b], i) => {
    if (a >= 8 && b >= 8) return; // 腿另算
    const mesh = capsuleBetween(joints[a].clone(), joints[b].clone(), radius * (i < 7 ? 0.9 : 0.8), mat);
    mesh.userData.jointPair = [a, b];
    group.add(mesh);
  });
  // 双腿 + 髋部
  [[8, 9], [9, 10], [11, 12], [12, 13], [8, 11]].forEach(([a, b]) => {
    const mesh = capsuleBetween(joints[a].clone(), joints[b].clone(), radius * 1.05, mat);
    mesh.userData.jointPair = [a, b];
    group.add(mesh);
  });
  // 躯干
  const torso = capsuleBetween(joints[1].clone(), new THREE.Vector3(0, (joints[8].y + joints[11].y) / 2, 0), radius * 1.7, mat);
  torso.userData.jointPair = [1, 8];
  group.add(torso);
  // 头
  const headR = Math.max(0.085, Number(m.height || 1.75) * 0.062);
  const head = new THREE.Mesh(new THREE.SphereGeometry(headR, 16, 12), mat);
  head.position.set(joints[0].x, joints[1].y + headR * 1.45, joints[0].z);
  group.add(head);

  group.position.set(...(m.position || [0, 0, 0]));
  group.rotation.order = "YXZ";
  const rot = m.rotation || [0, 0, 0];
  group.rotation.set(THREE.MathUtils.degToRad(rot[0]), THREE.MathUtils.degToRad(rot[1]), THREE.MathUtils.degToRad(rot[2]));
  group.userData.objectId = m.id;
  return group;
}

/** 重建替身网格（改姿势/身高时用，直接替换子节点比逐根改更快） */
export function refreshMannequin(group, m) {
  const mat = group.children[0]?.material;
  const fresh = buildMannequin(m, mat);
  group.clear();
  while (fresh.children.length) group.add(fresh.children[0]);
  group.position.copy(fresh.position);
  group.rotation.copy(fresh.rotation);
}

/** 机位线框：锥体 + 视线 */
export function buildCameraRig(color = 0x57c7ff) {
  const group = new THREE.Group();
  const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9 });
  const body = new THREE.LineSegments(new THREE.BufferGeometry(), mat);
  group.add(body);

  const body2 = new THREE.Mesh(
    new THREE.BoxGeometry(0.16, 0.1, 0.2),
    new THREE.MeshStandardMaterial({ color: 0x1b2430, emissive: color, emissiveIntensity: 0.35 })
  );
  group.add(body2);

  function update(vfovDeg, aspect, near = 0.05, far = 2.4) {
    const t = Math.tan(THREE.MathUtils.degToRad(vfovDeg) / 2);
    const hh = t * far;
    const hw = hh * aspect;
    const pts = [
      [0, 0, 0], [-hw, hh, -far], [0, 0, 0], [hw, hh, -far],
      [0, 0, 0], [hw, -hh, -far], [0, 0, 0], [-hw, -hh, -far],
      [-hw, hh, -far], [hw, hh, -far], [hw, hh, -far], [hw, -hh, -far],
      [hw, -hh, -far], [-hw, -hh, -far], [-hw, -hh, -far], [-hw, hh, -far],
      [0, 0, 0], [0, 0, -far * 1.6],
    ].flat();
    body.geometry.dispose();
    body.geometry = new THREE.BufferGeometry();
    body.geometry.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  }

  return { group, update };
}

/** 目标点手柄 */
export function buildTargetHandle(color = 0xffb45e) {
  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(0.09, 14, 10),
    new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.25, roughness: 0.4 })
  );
  mesh.userData.isTarget = true;
  return mesh;
}

/** 场景相机 -> three 相机 */
export function applyCamera(camera, cam) {
  const pos = cam.position || [0, 1.5, 5];
  const tgt = cam.target || [0, 1.4, 0];
  camera.position.set(pos[0], pos[1], pos[2]);
  camera.up.set(0, 1, 0);
  camera.lookAt(new THREE.Vector3(tgt[0], tgt[1], tgt[2]));
  if (cam.vfov) {
    camera.fov = cam.vfov;
    camera.updateProjectionMatrix();
  }
  return camera;
}

export { _v };
