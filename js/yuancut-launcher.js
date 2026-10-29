/**
 * ComfyUI-YuanCut —— YuanCut 启动器
 *
 * 行为与 ComfyUI-OpenCut 一致：
 *  1. 不自动打开；点节点里的「打开 YuanCut」、双击节点、右键菜单或顶栏按钮才打开。
 *  2. 关闭后 iframe 实例保留在内存里，再次打开秒开，编辑状态与项目都不丢。
 *  3. 第一次打开时才真正请求 /yuancut/...，之前不产生任何加载。
 *
 * 纯 ES module，不需要打包；ComfyUI 会把 js/ 目录挂到
 * /extensions/ComfyUI-YuanCut/ 下并自动加载其中的 .js。
 */
import { app } from "../../../scripts/app.js";

const NODE_CLASS = "YuanCut";

/** 插件界面固定中文，内置编辑器也固定打开 zh 语言 */
const BASE_URL = "/yuancut/zh";
const HOME_URL = `${BASE_URL}/projects/`;

const TEXT = {
  open: "打开 YuanCut",
  nodeHint: "点击打开 YuanCut 视频编辑器，进入后是所有项目",
  reload: "重新加载",
  reloadTitle: "重新加载当前页面（未保存的编辑会丢失）",
  projects: "项目",
  projectsTitle: "返回项目列表",
  tab: "新标签页",
  tabTitle: "在新标签页打开 YuanCut",
  close: "关闭",
  closeTitle: "关闭（之后可再次打开，编辑状态保留）",
  menu: "打开 YuanCut 编辑器",
  dialogLabel: "YuanCut 视频编辑器",
  tag: "视频编辑器",
};

const STYLE_ID = "yuancut-launcher-style";
const OVERLAY_ID = "yuancut-launcher-overlay";

let overlay = null;
let frame = null;
let frameLoaded = false;
let restoreFocus = null;

/* YuanCut 的暗色设计令牌：background hsl(0 0% 7%)、border hsl(0 0% 16%)、primary #009dff */
const CSS = `
#${OVERLAY_ID} {
  position: fixed;
  inset: 0;
  z-index: 100000;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.72);
  backdrop-filter: blur(4px);
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
#${OVERLAY_ID}.is-open { display: flex; }

#${OVERLAY_ID} .yuancut-panel {
  display: flex;
  flex-direction: column;
  width: min(1680px, 100%);
  height: 100%;
  overflow: hidden;
  border: 1px solid hsl(0 0% 16%);
  border-radius: 14px;
  background: hsl(0 0% 7%);
  box-shadow: 0 32px 90px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(0, 157, 255, 0.06);
}

#${OVERLAY_ID} .yuancut-bar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 46px;
  padding: 0 12px 0 14px;
  border-bottom: 1px solid hsl(0 0% 16%);
  background: hsl(0 0% 9%);
  color: hsl(0 0% 92%);
  font-size: 13px;
  font-weight: 500;
  user-select: none;
}

#${OVERLAY_ID} .yuancut-brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
#${OVERLAY_ID} .yuancut-mark {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #009dff;
  box-shadow: 0 0 10px rgba(0, 157, 255, 0.75);
}
#${OVERLAY_ID} .yuancut-name { font-size: 13px; font-weight: 600; letter-spacing: 0.2px; }
#${OVERLAY_ID} .yuancut-tag {
  padding: 2px 7px;
  border: 1px solid hsl(204 100% 18%);
  border-radius: 999px;
  background: hsl(204 100% 10%);
  color: hsl(200 98% 66%);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

#${OVERLAY_ID} .yuancut-tools { display: flex; align-items: center; gap: 4px; }
#${OVERLAY_ID} .yuancut-btn {
  padding: 6px 11px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: hsl(0 0% 72%);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
}
#${OVERLAY_ID} .yuancut-btn:hover { background: hsl(0 0% 16%); color: hsl(0 0% 96%); }
#${OVERLAY_ID} .yuancut-btn:active { background: hsl(0 0% 20%); }
#${OVERLAY_ID} .yuancut-btn:focus-visible { outline: 2px solid rgba(0, 157, 255, 0.55); outline-offset: 1px; }
#${OVERLAY_ID} .yuancut-btn.is-danger:hover {
  border-color: hsl(0 72% 40%);
  background: hsl(0 72% 32%);
  color: #fff;
}

#${OVERLAY_ID} .yuancut-frame-wrap {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
}
#${OVERLAY_ID} .yuancut-frame {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
  background: hsl(0 0% 7%);
}
#${OVERLAY_ID} .yuancut-loading {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  background: hsl(0 0% 7%);
  color: hsl(0 0% 72%);
  font-size: 13px;
}
#${OVERLAY_ID} .yuancut-loading[hidden] { display: none; }
#${OVERLAY_ID} .yuancut-spinner {
  width: 22px;
  height: 22px;
  border: 2px solid hsl(0 0% 22%);
  border-top-color: #009dff;
  border-radius: 50%;
  animation: yuancut-spin 0.8s linear infinite;
}
@keyframes yuancut-spin { to { transform: rotate(360deg); } }

.yuancut-node-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 2px;
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: hsl(0 0% 62%);
  user-select: none;
}
.yuancut-node-box .yuancut-node-hint {
  font-size: 11px;
  line-height: 1.4;
  opacity: 0.8;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.yuancut-node-box .yuancut-node-open {
  padding: 8px 12px;
  border: 1px solid rgba(0, 157, 255, 0.55);
  border-radius: 8px;
  background: #009dff;
  color: #fff;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: filter 0.12s ease, transform 0.06s ease;
}
.yuancut-node-box .yuancut-node-open:hover { filter: brightness(1.1); }
.yuancut-node-box .yuancut-node-open:active { filter: brightness(0.92); transform: translateY(1px); }
.yuancut-node-box .yuancut-node-open:focus-visible { outline: 2px solid rgba(0, 157, 255, 0.5); outline-offset: 2px; }
`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}

let loadingEl = null;
let loadingTimer = null;
let navAckTimer = null;

/** 加载提示：延迟 150ms 再显示，避免快速加载时闪一下 */
function showLoading() {
  if (!loadingEl || !loadingEl.hidden) return;
  clearTimeout(loadingTimer);
  loadingTimer = setTimeout(() => {
    if (loadingEl) loadingEl.hidden = false;
  }, 150);
}

function hideLoading() {
  clearTimeout(loadingTimer);
  loadingTimer = null;
  if (loadingEl) loadingEl.hidden = true;
}

/** 整页加载某个地址 */
function loadFrame(url) {
  if (!frame) return;
  showLoading();
  frame.src = url;
  frameLoaded = true;
}

/**
 * 站内跳转：交给前端路由，避免 iframe 整页重载（重载要重新解析 ~3MB 脚本）
 * 前端侧由 host-bridge 监听并 push。
 */
function navigateInFrame(route) {
  if (!frameLoaded || !frame?.contentWindow) {
    loadFrame(`${BASE_URL}${route.replace(/^\/zh/, "")}`);
    return;
  }
  try {
    frame.contentWindow.postMessage(
      { type: "yuancut:navigate", path: route },
      window.location.origin,
    );
    // 前端路由没接住（旧版本/异常）就退回整页加载，避免按钮点了没反应
    clearTimeout(navAckTimer);
    navAckTimer = setTimeout(() => {
      try {
        if (!frame.contentWindow.location.pathname.endsWith(route)) {
          loadFrame(`${BASE_URL}/projects/`);
        }
      } catch {
        loadFrame(`${BASE_URL}/projects/`);
      }
    }, 800);
  } catch {
    loadFrame(`${BASE_URL}/projects/`);
  }
}

/** 重新加载 iframe 里当前页面（项目列表或编辑器） */
function reloadFrame() {
  if (!frameLoaded || !frame) return;
  showLoading();
  try {
    frame.contentWindow.location.reload();
  } catch {
    loadFrame(HOME_URL);
  }
}

function onOverlayClick(event) {
  const action = event.target.closest?.("[data-act]")?.dataset.act;
  if (!action) {
    if (event.target === overlay) closeEditor();
    return;
  }
  if (action === "close") {
    closeEditor();
  } else if (action === "reload") {
    reloadFrame();
  } else if (action === "projects") {
    let current = "";
    try {
      current = frame?.contentWindow?.location?.pathname || "";
    } catch {
      current = "";
    }
    if (current.endsWith("/projects/")) return;
    navigateInFrame("/zh/projects/");
  } else if (action === "tab") {
    window.open(HOME_URL, "_blank", "noopener");
  }
}

function onKeyDown(event) {
  if (event.key === "Escape" && overlay?.classList.contains("is-open")) {
    closeEditor();
  }
}

function ensureOverlay() {
  if (overlay) return overlay;

  injectStyle();

  overlay = document.createElement("div");
  overlay.id = OVERLAY_ID;
  overlay.innerHTML = `
    <div class="yuancut-panel" role="dialog" aria-modal="true" aria-label="${TEXT.dialogLabel}">
      <div class="yuancut-bar">
        <div class="yuancut-brand">
          <span class="yuancut-mark"></span>
          <span class="yuancut-name">YuanCut</span>
          <span class="yuancut-tag">${TEXT.tag}</span>
        </div>
        <div class="yuancut-tools">
          <button type="button" class="yuancut-btn" data-act="projects" title="${TEXT.projectsTitle}">${TEXT.projects}</button>
          <button type="button" class="yuancut-btn" data-act="reload" title="${TEXT.reloadTitle}">${TEXT.reload}</button>
          <button type="button" class="yuancut-btn" data-act="tab" title="${TEXT.tabTitle}">${TEXT.tab}</button>
          <button type="button" class="yuancut-btn is-danger" data-act="close" title="${TEXT.closeTitle}">${TEXT.close}</button>
        </div>
      </div>
      <div class="yuancut-frame-wrap">
        <iframe class="yuancut-frame" title="YuanCut" src="about:blank" allow="clipboard-read; clipboard-write; fullscreen"></iframe>
        <div class="yuancut-loading" hidden>
          <span class="yuancut-spinner"></span>
          <span>正在加载 YuanCut…</span>
        </div>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  frame = overlay.querySelector(".yuancut-frame");
  loadingEl = overlay.querySelector(".yuancut-loading");
  frame.addEventListener("load", hideLoading);
  window.addEventListener("message", (event) => {
    if (event.data?.type === "yuancut:navigated") {
      clearTimeout(navAckTimer);
      navAckTimer = null;
      hideLoading();
    }
  });
  overlay.addEventListener("click", onOverlayClick);
  document.addEventListener("keydown", onKeyDown, true);

  return overlay;
}

/** 打开编辑器（第一次调用时才真正加载 /yuancut/...） */
export function openEditor() {
  ensureOverlay();
  if (!frameLoaded) {
    loadFrame(HOME_URL);
  }
  restoreFocus = document.activeElement;
  overlay.classList.add("is-open");
  overlay.querySelector('[data-act="close"]')?.focus();
}

/** 关闭编辑器；iframe 保留，下次打开立即恢复上次的编辑状态 */
export function closeEditor() {
  if (!overlay) return;
  overlay.classList.remove("is-open");
  if (restoreFocus?.focus) {
    try {
      restoreFocus.focus();
    } catch {
      /* 元素可能已被移除 */
    }
  }
  restoreFocus = null;
}

/** Python 侧的 NODE_DISPLAY_NAME_MAPPINGS 需重启 ComfyUI 才生效，这里兜一层即时生效 */
function applyTitle(node) {
  if (!node.title || node.title === NODE_CLASS) node.title = "YuanCut 视频编辑器";
}

function buildNodeUI(node) {
  if (node.__yuancutLauncher) return;
  node.__yuancutLauncher = true;

  applyTitle(node);

  const box = document.createElement("div");
  box.className = "yuancut-node-box";
  box.innerHTML = `
    <div class="yuancut-node-hint">${TEXT.nodeHint}</div>
    <button type="button" class="yuancut-node-open">${TEXT.open}</button>`;

  const button = box.querySelector(".yuancut-node-open");
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openEditor();
  });
  // 阻止在按钮上拖拽时被画布当成节点拖动
  box.addEventListener("pointerdown", (event) => event.stopPropagation());
  box.addEventListener("wheel", (event) => event.stopPropagation(), { passive: true });

  if (typeof node.addDOMWidget === "function") {
    node.addDOMWidget("yuancut_launch", "yuancut-launch", box, {
      getValue: () => "",
      setValue: () => {},
      computeSize: (width) => [width || 280, 78],
      hideOnZoom: false,
    });
  }

  // 双击节点 = 打开
  const previousDblClick = node.onDblClick;
  node.onDblClick = function (...args) {
    if (typeof previousDblClick === "function") previousDblClick.apply(this, args);
    openEditor();
  };

  // 右键菜单入口
  const previousMenu = node.getExtraMenuOptions;
  node.getExtraMenuOptions = function (canvas, options) {
    if (typeof previousMenu === "function") previousMenu.apply(this, arguments);
    options.push({ content: TEXT.menu, callback: () => openEditor() });
  };

  // 节点最小尺寸：保证提示文字与按钮完整落在节点内
  const MIN_WIDTH = 300;
  const MIN_HEIGHT = 150;
  const ensureSize = () => {
    if (!node.size || node.size.length < 2 || typeof node.size[0] !== "number") return;
    const width = node.size[0] || 0;
    const height = node.size[1] || 0;
    if (width >= MIN_WIDTH && height >= MIN_HEIGHT) return;
    node.setSize([Math.max(width, MIN_WIDTH), Math.max(height, MIN_HEIGHT)]);
    app.graph?.setDirtyCanvas?.(true, true);
  };

  ensureSize();
  setTimeout(ensureSize, 0);
  setTimeout(ensureSize, 150);

  const previousOnAdded = node.onAdded;
  node.onAdded = function (...args) {
    if (typeof previousOnAdded === "function") previousOnAdded.apply(this, args);
    ensureSize();
  };

  const previousOnConfigure = node.onConfigure;
  node.onConfigure = function (...args) {
    if (typeof previousOnConfigure === "function") previousOnConfigure.apply(this, args);
    ensureSize();
    applyTitle(this);
  };
}

app.registerExtension({
  name: "ComfyUI.YuanCut.NodeLauncher",
  async nodeCreated(node) {
    if (node.comfyClass !== NODE_CLASS) return;
    buildNodeUI(node);
  },
});

app.registerExtension({
  name: "ComfyUI.YuanCut.TopMenu",
  async setup() {
    const ComfyButton = window.comfyAPI?.button?.ComfyButton;
    if (!ComfyButton) return;
    const menu = app.menu?.settingsGroup;
    if (!menu) return;
    try {
      menu.append(
        new ComfyButton({
          icon: "video",
          tooltip: "打开 YuanCut 视频编辑器",
          content: "YuanCut",
          action: openEditor,
        }),
      );
    } catch (error) {
      console.warn("[YuanCut] failed to add top menu button", error);
    }
  },
});

// 方便在控制台里调试：ComfyUI.YuanCut.open()
window.ComfyUI = window.ComfyUI || {};
window.ComfyUI.YuanCut = { open: openEditor, close: closeEditor };
