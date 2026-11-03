// Alber-3D 导演台 · ComfyUI 前端扩展：给「Alber-导演台 · 场景编辑」节点挂上「打开 Alber-3D 导演台」按钮。
//
// 注意：ComfyUI 会递归扫描 WEB_DIRECTORY 下所有 *.js 并逐个 import()，
// 本文件被挂在 /extensions/ComfyUI-Alber-DirectorStage/js/director_stage.js，
// 因此引用核心模块一律用绝对路径 /scripts/app.js，避免层级算错。
import { app } from "/scripts/app.js";

const EXT_NAME = "director3d.stage";
const BTN_OPEN = "打开 Alber-3D 导演台";
const BTN_RUN = "保存并运行";

// ---------------------------------------------------------------- 工具

function toast(severity, summary, detail) {
  try {
    app.extensionManager?.toast?.add({ severity, summary, detail, life: 5000 });
  } catch (e) {
    console.log(`[director3d] ${summary} ${detail || ""}`);
  }
}

function findWidget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

/** 顺着 scene 输入链路找到上游节点的 id，用于向后端要场景。 */
function upstreamNodeId(node) {
  const input = node.inputs?.find((i) => i.name === "scene");
  if (!input || input.link == null) return null;
  const links = app.graph?.links;
  const link = links?.[input.link] ?? links?.get?.(input.link);
  return link?.origin_id ?? null;
}

// ---------------------------------------------------------------- 取场景

async function fetchScene(node) {
  const originId = upstreamNodeId(node);
  const qs = originId != null ? `?node_id=${encodeURIComponent(originId)}` : "";
  const res = await fetch(`/director3d/scene${qs}`);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.error) msg = data.error;
    } catch (e) { /* 非 JSON 响应 */ }
    throw new Error(msg);
  }
  const data = await res.json();
  if (!data?.scene) throw new Error("上游还没有产出场景，请先运行一次「Alber-导演台 · 视频转3D」。");
  return data.scene;
}

// ---------------------------------------------------------------- 主流程

async function openConsole(node) {
  const boot = document.createElement("div");
  boot.className = "ds-boot";
  boot.innerHTML = `<div class="ds-boot-box">正在读取场景…</div>`;
  document.body.appendChild(boot);

  try {
    const scene = await fetchScene(node);
    boot.remove();

    // 控制台按需加载：首屏不拉 three.js
    const mod = await import("./director/console.js");
    mod.openDirectorConsole({
      scene,
      onSave: (payload) => writeState(node, payload),
      onClose: () => app.graph?.setDirtyCanvas?.(true, true),
    });
  } catch (err) {
    boot.remove();
    toast("error", "打不开 Alber-3D 导演台", String(err?.message || err));
  }
}

/** 把 Alber-3D 导演台吐出的机位 JSON 写回节点 state 控件，并触发重绘。 */
function writeState(node, payload) {
  const widget = findWidget(node, "state");
  if (widget) {
    widget.value = payload;
    widget.callback?.(payload);
  }
  node.properties = node.properties || {};
  node.properties.director_state = payload;
  app.graph?.setDirtyCanvas?.(true, true);
  toast("success", "机位已保存", "接着排队运行，就会按新机位重新渲染预演图。");
}

// ---------------------------------------------------------------- 注册

// 给自检脚本留个标记：能读到它，说明扩展确实被加载并执行了
if (typeof window !== "undefined") window.__director3dStage = { name: EXT_NAME };

app.registerExtension({
  name: EXT_NAME,

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "DirectorStage") return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onCreated?.apply(this, arguments);
      const node = this;

      // state 是隐藏的长 JSON 字符串控件：高度压到 0，但保持序列化
      const stateWidget = findWidget(node, "state");
      if (stateWidget) {
        stateWidget.computeSize = () => [0, -4];
        stateWidget.serialize = true;
      }

      const openBtn = node.addWidget("button", BTN_OPEN, null, () => openConsole(node));
      openBtn.color = "#2f6f5f";
      openBtn.serialize = false;

      const runBtn = node.addWidget("button", BTN_RUN, null, () => {
        app.graph?.setDirtyCanvas?.(true, true);
        try {
          app.queuePrompt(0, 1);
        } catch (e) {
          toast("warn", "请手动点 Queue", "当前前端不支持脚本排队。");
        }
      });
      runBtn.serialize = false;

      node.size = [Math.max(node.size[0], 320), Math.max(node.size[1], 150)];
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      onConfigure?.apply(this, arguments);
      // 工作流重新打开时，把保存在节点属性里的机位找回来
      const w = findWidget(this, "state");
      const saved = this.properties?.director_state;
      if (w && saved && !w.value) w.value = saved;
    };

    // 节点底部画一行提示，代替占位的 text 控件
    const onDrawForeground = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      onDrawForeground?.apply(this, arguments);
      if (this.flags?.collapsed) return;
      if (findWidget(this, "state")?.value) return;
      ctx.save();
      ctx.fillStyle = "rgba(150,150,160,0.75)";
      ctx.font = "12px sans-serif";
      ctx.fillText("先跑一次上游「视频转3D」，再打开 Alber-3D 导演台", 12, this.size[1] - 10);
      ctx.restore();
    };
  },
});
