/* ============================================================
   清影 QingYing — client
   ============================================================ */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  system: null,
  probe: null,
  preview: null,
  jobs: [],
  jobStatuses: new Map(),
  settings: {
    sr_engine: "fast",
    ai_model: "general",
    scale: 2,
    target_height: null,
    interp: "off",
    fps_mode: "multiple",
    fps_value: 2,
    fps_custom: false,
    order: "interp_first",
    denoise: "off",
    sharpen: 0.35,
    encoder: "libx264",
    codec: "h264",
    quality: "high",
    container: "mp4",
    audio: "copy",
    start: 0,
    end: null,
    output_dir: "",
    output_name: "",
  },
  previewTime: 1,
  previewSeq: 0,
  previewDirty: false,
  browser: {
    mode: "video",
    path: "",
    data: null,
    selected: null,
  },
  uploading: false,
  activeView: "workspace",
  pollTimer: null,
};

const STATUS_LABELS = {
  queued: "排队中",
  running: "处理中",
  done: "已完成",
  error: "失败",
  canceled: "已取消",
};

/* ---------------- utilities ---------------- */

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function formatBytes(bytes) {
  if (!bytes || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
}

function formatDuration(seconds, compact = false) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "—";
  const total = Math.max(0, Math.round(Number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return compact ? `${hours}h ${minutes}m` : `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return compact ? `${minutes}m ${secs}s` : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function formatFps(fps) {
  if (!fps) return "—";
  const rounded = Math.round(fps);
  return Math.abs(fps - rounded) < 0.03 ? String(rounded) : fps.toFixed(2);
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 1) return "即将完成";
  if (seconds < 60) return `约 ${Math.ceil(seconds)} 秒`;
  if (seconds < 3600) return `约 ${Math.ceil(seconds / 60)} 分钟`;
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.round((seconds % 3600) / 60);
    return `约 ${hours} 小时${minutes ? ` ${minutes} 分` : ""}`;
  }
  return `约 ${(seconds / 86400).toFixed(1)} 天`;
}

function resolutionLabel(width, height) {
  if (!width || !height) return "—";
  if (height >= 4300) return "8K";
  if (height >= 2100) return "4K";
  if (height >= 1400) return "2K";
  if (height >= 1000) return "1080P";
  if (height >= 700) return "720P";
  return `${height}P`;
}

async function api(path, options = {}) {
  const config = { headers: {}, ...options };
  if (config.body && !(config.body instanceof FormData)) {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(path, config);
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    let detail = payload.detail || payload.message || `请求失败（${response.status}）`;
    if (Array.isArray(detail)) {
      detail = detail.map((item) => item.msg || JSON.stringify(item)).join("；");
    }
    throw new Error(detail);
  }
  return payload;
}

function toast(message, type = "info", title = "") {
  const stack = $("#toast-stack");
  const icons = { success: "i-check", error: "i-alert", info: "i-info" };
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.innerHTML = `
    <span class="toast-icon"><svg><use href="#${icons[type] || "i-info"}"></use></svg></span>
    <div>${title ? `<b>${esc(title)}</b>` : ""}${esc(message)}</div>`;
  stack.appendChild(node);
  window.setTimeout(() => {
    node.classList.add("leaving");
    window.setTimeout(() => node.remove(), 260);
  }, type === "error" ? 6000 : 4200);
}

function debounce(fn, delay = 500) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

/* ---------------- initialization ---------------- */

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  const savedOutput = localStorage.getItem("qingying_output");
  if (savedOutput) state.settings.output_dir = savedOutput;
  await loadSystem();
  syncUI();
  await pollJobs(true);
  state.pollTimer = window.setInterval(() => pollJobs(false), 1200);
}

async function loadSystem(refreshAi = false) {
  try {
    if (refreshAi) {
      await api("/api/system/refresh-ai", { method: "POST" });
    }
    const system = await api("/api/system");
    state.system = system;
    renderSystem(system);
  } catch (error) {
    $("#ffmpeg-pill").innerHTML = '<i class="status-dot bad"></i><span>引擎检测失败</span>';
    toast(error.message, "error", "无法读取系统状态");
  }
}

function renderSystem(system) {
  const pill = $("#ffmpeg-pill");
  if (system.binaries.ffmpeg_ok) {
    pill.innerHTML = '<i class="status-dot ok"></i><span>本地引擎就绪</span>';
  } else {
    pill.innerHTML = '<i class="status-dot bad"></i><span>未找到 FFmpeg</span>';
  }

  $("#sys-cpu").textContent = `${system.cpu_count} 线程 · ${system.platform.machine || system.platform.system}`;
  $("#sys-ffmpeg").textContent = system.binaries.ffmpeg_version || "未找到";
  const ai = system.ai || {};
  const aiNode = $("#sys-ai");
  if (ai.available) {
    aiNode.textContent = ai.device ? `可用 · ${ai.device}` : "可用";
    aiNode.className = "ok";
  } else {
    aiNode.textContent = "不可用";
    aiNode.className = "bad";
    aiNode.title = ai.reason || "未检测到可用的 Vulkan 显卡";
  }
  const output = $("#sys-output");
  output.textContent = system.output_dir;
  output.title = system.output_dir;
  $("#sys-output").dataset.path = system.output_dir;

  const free = system.disk?.free ? `剩余 ${formatBytes(system.disk.free)}` : "";
  const note = $("#sys-note");
  if (!system.binaries.ffmpeg_ok) {
    note.hidden = false;
    note.textContent = "未找到 FFmpeg。请安装 FFmpeg 后重启应用，或将 ffmpeg 放入程序目录的 bin 文件夹。";
  } else if (!ai.available) {
    note.hidden = false;
    note.textContent = `AI 超分不可用：${ai.reason || "未检测到兼容的 Vulkan 显卡"}。快速高清与补帧仍可正常使用。`;
  } else {
    note.hidden = true;
    note.textContent = "";
  }
  if (free) $("#footer-runtime").textContent = `本地工作台 · ${free}`;

  const aiButton = $("#ai-engine-btn");
  aiButton.disabled = !ai.available;
  aiButton.title = ai.available ? "Real-ESRGAN · Vulkan 加速" : (ai.reason || "当前显卡不支持 AI 超分");
  if (!ai.available) $("#sr-engine-note").textContent = "AI 超分需要兼容 Vulkan 的显卡；当前使用高质量重采样。";

  renderEncoderOptions(system.encoders || []);
}

function renderEncoderOptions(encoders) {
  const select = $("#encoder");
  if (!encoders.length) {
    select.innerHTML = '<option value="libx264">H.264 · CPU (x264)</option>';
    return;
  }
  const current = state.settings.encoder;
  const groups = { software: [], hardware: [] };
  for (const item of encoders) {
    groups[item.hardware ? "hardware" : "software"].push(item);
  }
  let html = "";
  if (groups.software.length) {
    html += '<optgroup label="CPU 编码">';
    for (const item of groups.software) {
      html += `<option value="${esc(item.value)}" data-codec="${esc(item.codec)}">${esc(item.label)}</option>`;
    }
    html += "</optgroup>";
  }
  if (groups.hardware.length) {
    html += '<optgroup label="硬件加速">';
    for (const item of groups.hardware) {
      html += `<option value="${esc(item.value)}" data-codec="${esc(item.codec)}">${esc(item.label)}</option>`;
    }
    html += "</optgroup>";
  }
  select.innerHTML = html;
  const exists = encoders.some((item) => item.value === current);
  const preferred = exists ? current : (encoders.find((item) => item.value === "libx264") || encoders[0]).value;
  select.value = preferred;
  const option = select.selectedOptions[0];
  state.settings.encoder = preferred;
  state.settings.codec = option?.dataset.codec || "h264";
}

/* ---------------- events ---------------- */

function bindEvents() {
  $$("[data-view]").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  $$("[data-nav]").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.nav));
  });

  $("#browse-source").addEventListener("click", () => openBrowser("video"));
  $("#upload-source").addEventListener("click", () => $("#file-input").click());
  $("#change-source").addEventListener("click", () => openBrowser("video"));
  $("#file-input").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) uploadFile(file);
    event.target.value = "";
  });

  const dropZone = $("#drop-zone");
  ["dragenter", "dragover"].forEach((type) => {
    document.addEventListener(type, (event) => {
      if (!event.dataTransfer?.types?.includes("Files")) return;
      event.preventDefault();
      dropZone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    document.addEventListener(type, (event) => {
      if (type === "dragleave" && event.relatedTarget) return;
      dropZone.classList.remove("dragging");
    });
  });
  document.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files?.length) return;
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (!file.type.startsWith("video/") && !/\.(mp4|mkv|mov|avi|ts|m2ts|webm|flv|wmv|rmvb)$/i.test(file.name)) {
      toast("这里只支持视频文件", "error");
      return;
    }
    if (file.size > 2 * 1024 ** 3) {
      toast("超过 2GB 的文件建议用「浏览本机文件」直接读取，避免复制。", "info", "大文件提示");
      openBrowser("video");
      return;
    }
    uploadFile(file);
  });

  $("#sr-toggle").addEventListener("change", (event) => {
    state.settings.sr_engine = event.target.checked ? (state.settings.sr_engine === "ai" && aiAvailable() ? "ai" : "fast") : "off";
    syncUI();
    markPreviewDirty();
  });
  $$("#sr-engine button").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      state.settings.sr_engine = button.dataset.value;
      syncUI();
      markPreviewDirty();
    });
  });
  $$("#scale-row button").forEach((button) => {
    button.addEventListener("click", () => {
      const scale = Number(button.dataset.scale);
      if (scale === 0) {
        state.settings.target_height = Number($("#custom-height").value) || 2160;
      } else {
        state.settings.scale = scale;
        state.settings.target_height = null;
      }
      syncUI();
      markPreviewDirty();
    });
  });
  $("#custom-height").addEventListener("input", (event) => {
    state.settings.target_height = clamp(Number(event.target.value) || 2160, 120, 4320);
    syncUI();
    markPreviewDirty();
  });
  $("#ai-model").addEventListener("change", (event) => {
    state.settings.ai_model = event.target.value;
    markPreviewDirty();
  });

  $("#interp-toggle").addEventListener("change", (event) => {
    state.settings.interp = event.target.checked ? "balanced" : "off";
    syncUI();
    markPreviewDirty();
  });
  $$("#interp-quality button").forEach((button) => {
    button.addEventListener("click", () => {
      state.settings.interp = button.dataset.value;
      syncUI();
      markPreviewDirty();
    });
  });
  $$("#fps-row button").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.mode;
      if (mode === "custom") {
        state.settings.fps_custom = true;
        state.settings.fps_mode = "fixed";
        state.settings.fps_value = Number($("#custom-fps").value) || 60;
      } else {
        state.settings.fps_custom = false;
        state.settings.fps_mode = mode;
        state.settings.fps_value = Number(button.dataset.value);
      }
      syncUI();
    });
  });
  $("#custom-fps").addEventListener("input", (event) => {
    state.settings.fps_mode = "fixed";
    state.settings.fps_value = Number(event.target.value) || 60;
    syncUI();
  });
  $$("#order-row button").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      state.settings.order = button.dataset.value;
      syncUI();
      markPreviewDirty();
    });
  });

  $("#advanced-toggle").addEventListener("click", () => {
    const body = $("#advanced-body");
    body.hidden = !body.hidden;
    $("#advanced-toggle").classList.toggle("open", !body.hidden);
  });

  $("#denoise").addEventListener("change", (event) => {
    state.settings.denoise = event.target.value;
    markPreviewDirty();
  });
  $("#sharpen").addEventListener("input", (event) => {
    state.settings.sharpen = Number(event.target.value);
    $("#sharpen-value").textContent = state.settings.sharpen.toFixed(2);
    event.target.style.setProperty("--range", `${(state.settings.sharpen / 1.2) * 100}%`);
    markPreviewDirty();
  });
  $("#encoder").addEventListener("change", (event) => {
    state.settings.encoder = event.target.value;
    state.settings.codec = event.target.selectedOptions[0]?.dataset.codec || "h264";
    updateAdvancedSummary();
  });
  $("#quality").addEventListener("change", (event) => {
    state.settings.quality = event.target.value;
    updateAdvancedSummary();
  });
  $("#audio").addEventListener("change", (event) => {
    state.settings.audio = event.target.value;
    updateAdvancedSummary();
  });
  $$("#container-row button").forEach((button) => {
    button.addEventListener("click", () => {
      state.settings.container = button.dataset.value;
      syncUI();
      updateAdvancedSummary();
    });
  });
  $("#trim-start").addEventListener("input", (event) => {
    state.settings.start = Math.max(0, Number(event.target.value) || 0);
    updateRunSummary();
  });
  $("#trim-end").addEventListener("input", (event) => {
    state.settings.end = event.target.value === "" ? null : Math.max(0, Number(event.target.value) || 0);
    updateRunSummary();
  });
  $("#use-preview-time").addEventListener("click", () => {
    const time = Math.max(0, Number($("#preview-time").value) || 0);
    $("#trim-start").value = time.toFixed(1);
    state.settings.start = time;
    updateRunSummary();
    toast(`已从 ${formatDuration(time)} 开始处理片段`, "info");
  });
  $("#browse-output").addEventListener("click", () => openBrowser("dir"));
  $("#output-dir").addEventListener("input", (event) => {
    state.settings.output_dir = event.target.value.trim();
    localStorage.setItem("qingying_output", state.settings.output_dir);
  });
  $("#output-name").addEventListener("input", (event) => {
    state.settings.output_name = event.target.value.trim();
  });

  $("#preview-btn").addEventListener("click", () => requestPreview(true));
  $("#refresh-preview").addEventListener("click", () => requestPreview(true));
  $("#preview-time").addEventListener("change", () => {
    state.previewTime = Math.max(0, Number($("#preview-time").value) || 0);
    requestPreview(true);
  });
  $("#preview-time").addEventListener("input", () => {
    state.previewTime = Math.max(0, Number($("#preview-time").value) || 0);
  });
  $("#compare-range").addEventListener("input", (event) => {
    const compare = $("#compare-stage");
    if (compare) compare.style.setProperty("--pos", `${event.target.value}%`);
  });

  $("#start-btn").addEventListener("click", () => startJob(false));
  $("#trial-btn").addEventListener("click", () => startJob(true));
  $("#open-output").addEventListener("click", openOutputDir);
  $("#open-output-2").addEventListener("click", openOutputDir);
  $("#refresh-ai").addEventListener("click", async () => {
    const button = $("#refresh-ai");
    button.disabled = true;
    button.textContent = "检测中…";
    try {
      await loadSystem(true);
      toast("AI 引擎状态已刷新", "success");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = "重新检测";
    }
  });
  $("#help-btn").addEventListener("click", () => openModal("help"));
  $("#quit-btn").addEventListener("click", quitApp);

  $$("[data-close]").forEach((element) => {
    element.addEventListener("click", () => closeModal(element.dataset.close));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeModal("browser");
      closeModal("help");
      closeModal("video");
    }
  });

  $("#browser-up").addEventListener("click", () => {
    if (state.browser.data?.parent) loadBrowser(state.browser.data.parent);
  });
  $("#browser-go").addEventListener("click", () => loadBrowser($("#browser-path").value.trim()));
  $("#browser-path").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadBrowser(event.target.value.trim());
  });
  $("#browser-confirm").addEventListener("click", confirmBrowser);
  $("#browser-list").addEventListener("click", onBrowserListClick);
  $("#browser-list").addEventListener("dblclick", onBrowserListDoubleClick);

  $("#queue-list").addEventListener("click", onQueueClick);
  $("#queue-full").addEventListener("click", onQueueClick);
}

function showView(view) {
  state.activeView = view;
  $$(".view").forEach((element) => element.classList.remove("active"));
  $(`#view-${view}`)?.classList.add("active");
  $$(".nav-btn").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  if (view === "queue") pollJobs(false);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function aiAvailable() {
  return Boolean(state.system?.ai?.available);
}

function openModal(name) {
  const modal = $(`#${name}-modal`);
  if (!modal) return;
  modal.hidden = false;
  document.body.style.overflow = "hidden";
}

function closeModal(name) {
  const modal = $(`#${name}-modal`);
  if (!modal) return;
  modal.hidden = true;
  if (name === "video") {
    const player = $("#video-player");
    player.pause();
    player.removeAttribute("src");
    player.load();
  }
  if (!$$(".modal").some((element) => !element.hidden)) {
    document.body.style.overflow = "";
  }
}

/* ---------------- file browser ---------------- */

async function openBrowser(mode, initialPath = null) {
  state.browser.mode = mode;
  state.browser.selected = null;
  $("#browser-eyebrow").textContent = mode === "dir" ? "CHOOSE OUTPUT FOLDER" : "PICK A VIDEO";
  $("#browser-title").textContent = mode === "dir" ? "选择输出文件夹" : "选择视频文件";
  $("#browser-confirm").textContent = mode === "dir" ? "使用此文件夹" : "选择此文件";
  let path = initialPath;
  if (!path && mode === "dir" && state.settings.output_dir) path = state.settings.output_dir;
  if (!path && state.probe) {
    const normalized = state.probe.path.replaceAll("/", "\\");
    const index = normalized.lastIndexOf("\\");
    if (index > 0) path = normalized.substring(0, index);
  }
  if (!path) path = localStorage.getItem("qingying_browser") || null;
  openModal("browser");
  await loadBrowser(path);
}

async function loadBrowser(path) {
  const list = $("#browser-list");
  list.innerHTML = '<div class="browser-empty">正在读取…</div>';
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const data = await api(`/api/fs${query}`);
    state.browser.data = data;
    state.browser.path = data.path;
    state.browser.selected = null;
    localStorage.setItem("qingying_browser", data.path);
    $("#browser-path").value = data.path;
    $("#browser-up").disabled = !data.parent;
    renderBrowserRoots(data.roots || []);
    renderBrowserList(data);
    $("#browser-confirm").disabled = state.browser.mode === "video";
    $("#browser-selection").textContent = state.browser.mode === "dir"
      ? `输出到：${data.path}`
      : "请选择一个视频文件";
  } catch (error) {
    list.innerHTML = `<div class="browser-empty">${esc(error.message)}</div>`;
    toast(error.message, "error", "无法打开目录");
  }
}

function renderBrowserRoots(roots) {
  const container = $("#browser-roots");
  container.innerHTML = roots.map((root) => `<button data-root="${esc(root.path)}">${esc(root.label)}</button>`).join("");
  $$("button[data-root]", container).forEach((button) => {
    button.addEventListener("click", () => loadBrowser(button.dataset.root));
  });
}

function renderBrowserList(data) {
  const list = $("#browser-list");
  const directories = data.directories || [];
  const files = state.browser.mode === "video" ? (data.files || []) : [];
  let html = "";
  for (const dir of directories) {
    html += `
      <div class="browser-item dir" data-type="dir" data-path="${esc(dir.path)}">
        <svg><use href="#i-folder"></use></svg>
        <span class="bi-name">${esc(dir.name)}</span>
        <span class="bi-meta">文件夹</span>
      </div>`;
  }
  for (const file of files) {
    html += `
      <div class="browser-item video" data-type="file" data-path="${esc(file.path)}">
        <svg><use href="#i-film"></use></svg>
        <span class="bi-name">${esc(file.name)}</span>
        <span class="bi-meta">${formatBytes(file.size)}</span>
      </div>`;
  }
  if (!html) {
    html = `<div class="browser-empty">${state.browser.mode === "dir" ? "这里没有子文件夹，可直接使用当前文件夹。" : "此文件夹中没有找到支持的视频文件。"}</div>`;
  }
  list.innerHTML = html;
}

function onBrowserListClick(event) {
  const item = event.target.closest(".browser-item");
  if (!item) return;
  if (item.dataset.type === "dir") {
    loadBrowser(item.dataset.path);
    return;
  }
  $$(".browser-item.selected", $("#browser-list")).forEach((element) => element.classList.remove("selected"));
  item.classList.add("selected");
  state.browser.selected = item.dataset.path;
  $("#browser-confirm").disabled = false;
  $("#browser-selection").textContent = item.dataset.path;
}

function onBrowserListDoubleClick(event) {
  const item = event.target.closest(".browser-item");
  if (!item) return;
  if (item.dataset.type === "file") {
    state.browser.selected = item.dataset.path;
    confirmBrowser();
  }
}

function confirmBrowser() {
  if (state.browser.mode === "dir") {
    const path = state.browser.data?.path;
    if (!path) return;
    state.settings.output_dir = path;
    $("#output-dir").value = path;
    localStorage.setItem("qingying_output", path);
    closeModal("browser");
    toast("输出目录已设置", "success");
    return;
  }
  if (!state.browser.selected) return;
  const path = state.browser.selected;
  closeModal("browser");
  selectSource(path);
}

/* ---------------- source ---------------- */

async function selectSource(path) {
  $("#source-empty").classList.add("loading");
  $("#browse-source").disabled = true;
  $("#upload-source").disabled = true;
  toast("正在读取视频信息…", "info");
  try {
    const probe = await api("/api/probe", { method: "POST", body: { path } });
    state.probe = probe;
    state.previewTime = clamp(probe.duration * 0.08 || 1, 0.1, Math.max(0.1, probe.duration - 0.1));
    if (state.previewTime > 30) state.previewTime = 30;
    state.settings.start = 0;
    state.settings.end = null;
    state.settings.output_name = "";
    state.settings.denoise = probe.height <= 720 ? "light" : "off";
    $("#preview-time").value = state.previewTime.toFixed(1);
    $("#trim-start").value = "0";
    $("#trim-end").value = "";
    $("#output-name").value = "";
    $("#denoise").value = state.settings.denoise;
    renderSource();
    syncUI();
    await requestPreview(false);
    toast(`已载入 ${probe.name}`, "success");
    if (probe.hdr) {
      toast("检测到 HDR 素材。当前版本会转为 8-bit SDR 输出，色彩可能与原始播放器不同。", "info", "HDR 提示");
    }
  } catch (error) {
    toast(error.message, "error", "无法读取视频");
  } finally {
    $("#source-empty").classList.remove("loading");
    $("#browse-source").disabled = false;
    $("#upload-source").disabled = false;
  }
}

function renderSource() {
  const probe = state.probe;
  const loaded = Boolean(probe);
  $("#source-empty").hidden = loaded;
  $("#source-loaded").hidden = !loaded;
  $("#change-source").hidden = !loaded;
  $("#preview-btn").disabled = !loaded;
  if (!loaded) {
    $("#start-btn").disabled = true;
    $("#trial-btn").disabled = true;
    return;
  }
  $("#source-name").textContent = probe.name;
  $("#source-path").textContent = probe.path;
  $("#source-path").title = probe.path;
  $("#meta-resolution").textContent = `${probe.width}×${probe.height}`;
  $("#meta-fps").textContent = `${formatFps(probe.fps)} fps`;
  $("#meta-duration").textContent = formatDuration(probe.duration);
  $("#meta-size").textContent = formatBytes(probe.size);
  $("#source-codec-badge").textContent = `${(probe.video_codec || "video").toUpperCase()}${probe.hdr ? " · HDR" : ""}`;
  const thumb = $("#source-thumb-img");
  thumb.src = `/api/thumbnail?path=${encodeURIComponent(probe.path)}&duration=${encodeURIComponent(probe.duration)}`;
  thumb.onerror = () => {
    thumb.style.display = "none";
  };
  thumb.style.display = "block";
  $("#start-btn").disabled = false;
  $("#trial-btn").disabled = false;
  updateRunSummary();
}

async function uploadFile(file) {
  if (state.uploading) return;
  if (file.size > 20 * 1024 ** 3) {
    toast("文件超过 20GB 上传上限，请使用「浏览本机文件」。", "error", "无法上传");
    return;
  }
  state.uploading = true;
  const panel = $("#upload-progress");
  panel.hidden = false;
  $("#upload-percent").textContent = "0%";
  $("#upload-bar").style.width = "0%";
  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.round((event.loaded / event.total) * 100);
    $("#upload-percent").textContent = `${percent}%`;
    $("#upload-bar").style.width = `${percent}%`;
  };
  xhr.onload = async () => {
    state.uploading = false;
    panel.hidden = true;
    try {
      const payload = JSON.parse(xhr.responseText || "{}");
      if (xhr.status >= 200 && xhr.status < 300 && payload.path) {
        await selectSource(payload.path);
      } else {
        throw new Error(payload.detail || "上传失败");
      }
    } catch (error) {
      toast(error.message, "error", "上传失败");
    }
  };
  xhr.onerror = () => {
    state.uploading = false;
    panel.hidden = true;
    toast("网络中断，上传失败", "error");
  };
  xhr.send(form);
}

/* ---------------- settings UI ---------------- */

function syncUI() {
  const settings = state.settings;
  const srOn = settings.sr_engine !== "off";
  const interpOn = settings.interp !== "off";

  $("#sr-toggle").checked = srOn;
  $("#sr-panel").classList.toggle("off", !srOn);
  $("#sr-body").classList.toggle("locked", !srOn);
  setActive("#sr-engine", "value", settings.sr_engine === "off" ? "fast" : settings.sr_engine);
  const aiButton = $("#ai-engine-btn");
  if (!aiAvailable()) aiButton.disabled = true;

  const scaleValue = settings.target_height ? "0" : String(settings.scale);
  setActive("#scale-row", "scale", scaleValue);
  $("#custom-height-row").hidden = !settings.target_height;
  if (settings.target_height) $("#custom-height").value = settings.target_height;
  $("#ai-model-field").hidden = settings.sr_engine !== "ai";
  $("#ai-model").value = settings.ai_model;
  $("#sr-engine-note").textContent = settings.sr_engine === "ai"
    ? "Real-ESRGAN 逐帧重建，画质最好，需要 Vulkan 显卡。"
    : (aiAvailable()
      ? "高质量重采样 + 锐化，速度快，适合长视频。"
      : "高质量重采样，速度快；AI 超分需要兼容 Vulkan 的显卡。");

  $("#interp-toggle").checked = interpOn;
  $("#interp-panel").classList.toggle("off", !interpOn);
  $("#interp-body").classList.toggle("locked", !interpOn);
  setActive("#interp-quality", "value", interpOn ? settings.interp : "balanced");
  $("#interp-quality-note").textContent = settings.interp === "smooth"
    ? "双向光流 + 遮挡补偿，画质优先，速度较慢。"
    : settings.interp === "fast"
      ? "帧混合模式，速度最快，快速运动可能轻微重影。"
      : "双向光流 + 遮挡补偿，速度与画质平衡。";

  $$("#fps-row button").forEach((button) => {
    const mode = button.dataset.mode;
    let active = false;
    if (mode === "custom") active = settings.fps_custom;
    else if (mode === "multiple") active = !settings.fps_custom && settings.fps_mode === "multiple" && Number(button.dataset.value) === settings.fps_value;
    else active = !settings.fps_custom && settings.fps_mode === "fixed" && Number(button.dataset.value) === settings.fps_value;
    button.classList.toggle("active", active);
  });
  $("#custom-fps-row").hidden = !settings.fps_custom;
  if (settings.fps_custom) $("#custom-fps").value = settings.fps_value;

  const aiMode = settings.sr_engine === "ai";
  $$("#order-row button").forEach((button) => {
    button.disabled = aiMode;
    button.classList.toggle("active", aiMode ? button.dataset.value === "sr_first" : button.dataset.value === settings.order);
  });
  $("#order-row").title = aiMode ? "AI 超分模式下会先完成超分，再补帧" : "";

  $$("#container-row button").forEach((button) => {
    button.classList.toggle("active", button.dataset.value === settings.container);
  });

  $("#sharpen-value").textContent = settings.sharpen.toFixed(2);
  $("#sharpen").value = settings.sharpen;
  $("#sharpen").style.setProperty("--range", `${(settings.sharpen / 1.2) * 100}%`);
  $("#denoise").value = settings.denoise;
  $("#quality").value = settings.quality;
  $("#audio").value = settings.audio;
  $("#output-dir").value = settings.output_dir;
  $("#output-name").value = settings.output_name;

  updateAdvancedSummary();
  updateTargetReadouts();
  updateRunSummary();
}

function setActive(containerSelector, dataKey, value) {
  $$(`${containerSelector} button`).forEach((button) => {
    button.classList.toggle("active", button.dataset[dataKey] === String(value));
  });
}

function updateAdvancedSummary() {
  const encoderLabel = $("#encoder").selectedOptions[0]?.textContent?.split(" · ")[0] || "H.264";
  const qualityLabel = { high: "高质量", balanced: "均衡", small: "小体积" }[state.settings.quality] || "";
  $("#advanced-summary").textContent = `${encoderLabel} · ${qualityLabel} · ${state.settings.container.toUpperCase()}`;
}

function calcTarget() {
  if (!state.probe) return null;
  const probe = state.probe;
  let width;
  let height;
  if (state.settings.sr_engine === "off") {
    width = probe.width;
    height = probe.height;
  } else if (state.settings.target_height) {
    height = state.settings.target_height;
    width = Math.round((height * probe.width) / probe.height);
  } else {
    width = Math.round(probe.width * state.settings.scale);
    height = Math.round(probe.height * state.settings.scale);
  }
  width = Math.max(2, Math.round(width / 2) * 2);
  height = Math.max(2, Math.round(height / 2) * 2);
  let fps = probe.fps;
  if (state.settings.interp !== "off") {
    fps = state.settings.fps_mode === "multiple" ? probe.fps * state.settings.fps_value : state.settings.fps_value;
  }
  const start = Math.max(0, Number(state.settings.start) || 0);
  const end = state.settings.end !== null ? Number(state.settings.end) : probe.duration;
  const duration = Math.max(0, Math.min(probe.duration, end) - start);
  return { width, height, fps, start, end, duration };
}

function updateTargetReadouts() {
  const target = calcTarget();
  if (!target || !state.probe) {
    $("#sr-readout b").textContent = "—";
    $("#interp-readout b").textContent = "—";
    return;
  }
  const probe = state.probe;
  $("#sr-readout b").textContent = `${probe.width}×${probe.height} → ${target.width}×${target.height}`;
  $("#interp-readout b").textContent = `${formatFps(probe.fps)} → ${formatFps(target.fps)} fps`;
}

function updateRunSummary() {
  const target = calcTarget();
  const probe = state.probe;
  const startButton = $("#start-btn");
  const trialButton = $("#trial-btn");
  if (!probe || !target) {
    $("#run-target").textContent = "选择视频后开始";
    $("#run-detail").textContent = "所有处理均在本机完成。";
    startButton.disabled = true;
    trialButton.disabled = true;
    return;
  }
  const srOn = state.settings.sr_engine !== "off";
  const interpOn = state.settings.interp !== "off";
  if (!srOn && !interpOn) {
    $("#run-target").textContent = "请至少启用一项增强";
    $("#run-detail").textContent = "打开超分辨率或运动补帧后即可开始。";
    startButton.disabled = true;
    trialButton.disabled = true;
    return;
  }
  const targetLabel = `${resolutionLabel(target.width, target.height)} · ${target.width}×${target.height}`;
  const parts = [];
  if (srOn) {
    const effectiveScale = probe.width ? target.width / probe.width : state.settings.scale;
    parts.push(state.settings.sr_engine === "ai" ? "AI 超分" : `超分 ${Number(effectiveScale.toFixed(2))}×`);
  }
  if (interpOn) parts.push(`补帧至 ${formatFps(target.fps)} fps`);
  $("#run-target").innerHTML = `${parts.join(" <em>·</em> ")} <em>→</em> ${esc(targetLabel)}`;
  const duration = target.duration;
  const frames = duration * target.fps;
  const order = interpOn && srOn ? (state.settings.order === "interp_first" && state.settings.sr_engine !== "ai" ? "补帧优先" : "超分优先") : "";
  const bits = [
    `处理 ${formatDuration(duration)}`,
    `约 ${Math.round(frames).toLocaleString("zh-CN")} 帧`,
    `输出 ${(state.settings.container || "mp4").toUpperCase()}`,
  ];
  if (order) bits.push(order);
  $("#run-detail").textContent = bits.join(" · ");
  const interpBad = interpOn && target.fps <= probe.fps + 0.01;
  const fpsTooHigh = interpOn && target.fps > 240.5;
  const resolutionTooHigh = target.width > 7680 || target.height > 4320;
  const invalid = interpBad || fpsTooHigh || resolutionTooHigh;
  startButton.disabled = invalid || state.uploading || !state.system?.binaries?.ffmpeg_ok;
  trialButton.disabled = invalid || state.uploading || !state.system?.binaries?.ffmpeg_ok;
  if (interpBad) $("#run-detail").textContent = `目标帧率需高于源帧率 ${formatFps(probe.fps)} fps`;
  else if (fpsTooHigh) $("#run-detail").textContent = "目标帧率不能超过 240 fps";
  else if (resolutionTooHigh) $("#run-detail").textContent = "目标分辨率超过 8K 上限，请降低倍率";
}

function markPreviewDirty() {
  updateTargetReadouts();
  updateRunSummary();
  if (state.probe && state.preview) {
    state.previewDirty = true;
    $("#preview-note").textContent = "设置已更改 · 点击刷新预览查看新效果。";
  }
}

/* ---------------- preview ---------------- */

async function requestPreview(manual = false) {
  if (!state.probe) {
    if (manual) toast("请先选择视频", "info");
    return;
  }
  const sequence = ++state.previewSeq;
  $("#preview-loading").hidden = false;
  $("#preview-btn").disabled = true;
  try {
    const payload = {
      path: state.probe.path,
      time: clamp(Number(state.previewTime) || 0, 0, Math.max(0, state.probe.duration - 0.05)),
      options: buildOptions(),
    };
    const preview = await api("/api/preview", { method: "POST", body: payload });
    if (sequence !== state.previewSeq) return;
    state.preview = preview;
    state.previewDirty = false;
    if (!preview.cached) {
      $("#preview-time").value = Number(preview.time).toFixed(1);
    }
    $("#compare-before").src = preview.before;
    $("#compare-after").src = preview.after;
    $("#compare-empty").hidden = true;
    $("#compare-stage").hidden = false;
    $("#compare-range").value = 50;
    $("#compare-stage").style.setProperty("--pos", "50%");
    $("#preview-note").textContent = preview.note || "拖动分割线查看增强前后差异。";
    const target = preview.target;
    $("#preview-target").hidden = false;
    $("#preview-target").textContent = `${resolutionLabel(target.width, target.height)} · ${target.width}×${target.height} · ${formatFps(target.fps)} fps${preview.ai_used ? " · AI 预览" : ""}`;
    if (manual) toast("预览帧已更新", "success");
  } catch (error) {
    if (sequence === state.previewSeq) {
      toast(error.message, "error", "预览生成失败");
      $("#preview-note").textContent = error.message;
    }
  } finally {
    if (sequence === state.previewSeq) {
      $("#preview-loading").hidden = true;
      $("#preview-btn").disabled = false;
    }
  }
}

/* ---------------- jobs ---------------- */

function buildOptions() {
  const settings = state.settings;
  const selected = $("#encoder").selectedOptions[0];
  return {
    sr_engine: settings.sr_engine,
    ai_model: settings.ai_model,
    scale: settings.target_height ? 1 : settings.scale,
    target_height: settings.target_height || null,
    interp: settings.interp,
    fps_mode: settings.fps_mode,
    fps_value: settings.fps_value,
    order: settings.order,
    denoise: settings.denoise,
    sharpen: settings.sharpen,
    codec: selected?.dataset.codec || settings.codec || "h264",
    encoder: $("#encoder").value || null,
    quality: settings.quality,
    crf: null,
    preset: "medium",
    container: settings.container,
    audio: state.probe?.has_audio ? settings.audio : "none",
    start: Math.max(0, Number(settings.start) || 0),
    end: settings.end === null || settings.end === "" ? null : Number(settings.end),
    output_dir: settings.output_dir || null,
    output_name: settings.output_name || null,
  };
}

async function startJob(preview = false) {
  if (!state.probe) {
    toast("请先选择一个视频", "info");
    openBrowser("video");
    return;
  }
  if (!state.system?.binaries?.ffmpeg_ok) {
    toast("未找到 FFmpeg，无法开始处理", "error");
    return;
  }
  const target = calcTarget();
  if (!target) return;
  if (state.settings.sr_engine === "off" && state.settings.interp === "off") {
    toast("请至少启用超分或补帧", "info");
    return;
  }
  if (state.settings.sr_engine === "ai" && !aiAvailable()) {
    toast("当前设备不支持 AI 超分，请改用快速高清", "error");
    return;
  }
  if (state.settings.interp !== "off" && target.fps <= state.probe.fps + 0.01) {
    toast(`目标帧率必须高于源帧率 ${formatFps(state.probe.fps)} fps`, "error");
    return;
  }
  if (state.settings.end !== null && Number(state.settings.end) <= Number(state.settings.start)) {
    toast("结束时间必须大于开始时间", "error");
    return;
  }

  const options = buildOptions();
  if (preview) {
    const duration = state.probe.duration;
    const start = Math.max(0, Number(options.start) || 0);
    const existingEnd = options.end !== null ? Number(options.end) : null;
    const candidate = existingEnd && existingEnd > start ? Math.min(existingEnd, start + 10) : start + 10;
    options.end = Math.min(duration, candidate);
    if (options.end <= start + 0.05) {
      toast("距离结尾不足 0.1 秒，请调整开始时间", "error");
      return;
    }
    options.start = start;
  }

  const buttons = ["#start-btn", "#trial-btn"];
  buttons.forEach((selector) => { $(selector).disabled = true; });
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: { source: state.probe.path, options, preview },
    });
    toast(preview ? "10 秒试跑已加入队列" : "任务已加入队列，开始处理", "success", preview ? "试跑" : "已开始");
    state.jobStatuses.set(job.id, job.status);
    await pollJobs(false);
    if (!preview) showView("queue");
  } catch (error) {
    toast(error.message, "error", "无法创建任务");
  } finally {
    updateRunSummary();
  }
}

async function pollJobs(initial = false) {
  try {
    const data = await api("/api/jobs");
    const jobs = data.jobs || [];
    const previous = state.jobStatuses;
    for (const job of jobs) {
      const old = previous.get(job.id);
      if (!initial && old && old !== job.status) {
        if (job.status === "done") {
          toast(job.preview ? "试跑片段已完成，可以播放预览。" : "视频处理完成，可以下载了。", "success", job.preview ? "试跑完成" : "处理完成");
          if (job.preview) openVideo(job.download_url, `${job.source_name} · 试跑片段`);
        } else if (job.status === "error") {
          toast(job.error || "处理失败", "error", "任务失败");
        } else if (job.status === "canceled") {
          toast("任务已取消", "info");
        }
      }
      previous.set(job.id, job.status);
    }
    state.jobs = jobs;
    renderJobs();
  } catch (error) {
    if (initial) toast(error.message, "error", "无法读取任务队列");
  }
}

function renderJobs() {
  const jobs = state.jobs;
  const active = jobs.filter((job) => job.status === "running" || job.status === "queued");
  const badge = $("#queue-count");
  if (active.length) {
    badge.hidden = false;
    badge.textContent = String(active.length);
  } else {
    badge.hidden = true;
  }
  const done = jobs.filter((job) => job.status === "done").length;
  $("#queue-subtitle").textContent = active.length
    ? `${active.length} 个任务进行中`
    : done
      ? `已完成 ${done} 个任务`
      : "还没有任务";

  const mini = $("#queue-list");
  if (!jobs.length) {
    mini.innerHTML = '<div class="queue-empty"><span>队列是空的</span><p>开始处理或试跑一段 10 秒预览。</p></div>';
  } else {
    mini.innerHTML = jobs.slice(0, 5).map((job) => jobMarkup(job, false)).join("");
  }

  const full = $("#queue-full");
  if (!jobs.length) {
    full.innerHTML = '<div class="big-empty"><svg><use href="#i-list"></use></svg><h3>暂无任务</h3><p>回到工作台，选择视频并开始处理。</p><button class="btn primary" data-nav="workspace">去工作台</button></div>';
    $$("[data-nav]", full).forEach((button) => button.addEventListener("click", () => showView(button.dataset.nav)));
  } else {
    full.innerHTML = jobs.map((job) => jobMarkup(job, true)).join("");
  }
}

function jobMarkup(job, full) {
  const status = job.status;
  const icon = {
    queued: "i-clock",
    running: "i-refresh",
    done: "i-check",
    error: "i-alert",
    canceled: "i-close",
  }[status] || "i-clock";
  const percent = status === "done" ? 100 : clamp(job.progress || 0, 0, 100);
  let meta = "";
  if (status === "running") {
    const bits = [];
    if (job.speed) bits.push(`${job.speed.toFixed(2)}× 速度`);
    if (job.fps) bits.push(`${job.fps.toFixed(1)} fps`);
    if (job.eta_seconds) bits.push(formatEta(job.eta_seconds));
    meta = bits.length ? bits.join(" · ") : "正在准备…";
  } else if (status === "queued") {
    meta = "等待处理";
  } else if (status === "done") {
    meta = `${resolutionLabel(job.target.width, job.target.height)} · ${formatFps(job.target.fps)} fps`;
    if (job.output_size) meta += ` · ${formatBytes(job.output_size)}`;
  } else if (status === "error") {
    meta = "点击查看错误信息";
  } else {
    meta = "已停止";
  }
  const detail = status === "running"
    ? `${job.stage || STATUS_LABELS[status]}${job.message && job.message !== job.stage ? ` · ${job.message}` : ""}`
    : status === "error"
      ? (job.error || "处理失败")
      : `${STATUS_LABELS[status] || status}${job.preview ? " · 试跑片段" : ""}`;

  const actions = [];
  if (status === "running" || status === "queued") {
    actions.push(`<button class="mini-btn" data-action="cancel" data-id="${job.id}">取消</button>`);
  } else if (status === "done") {
    if (job.preview) actions.push(`<button class="mini-btn" data-action="play" data-id="${job.id}">播放</button>`);
    actions.push(`<a class="mini-btn" href="${job.download_url}" download>下载</a>`);
    actions.push(`<button class="mini-btn" data-action="folder" data-id="${job.id}">目录</button>`);
    actions.push(`<button class="mini-btn" data-action="remove" data-id="${job.id}"><svg><use href="#i-trash"></use></svg></button>`);
  } else {
    actions.push(`<button class="mini-btn" data-action="remove" data-id="${job.id}"><svg><use href="#i-trash"></use></svg>移除</button>`);
  }

  const errorBlock = status === "error" && job.error
    ? `<div class="job-error">${esc(job.error)}</div>`
    : "";
  return `
    <article class="job ${esc(status)}" data-id="${job.id}">
      <div class="job-top">
        <div class="job-status-icon"><svg><use href="#${icon}"></use></svg></div>
        <div class="job-title"><b title="${esc(job.source_name)}">${esc(job.source_name)}</b><span>${esc(detail)}</span></div>
        <span class="job-meta">${percent.toFixed(0)}%</span>
      </div>
      <div class="job-progress"><i style="width:${percent}%"></i></div>
      <div class="job-foot">
        <span class="job-meta">${esc(meta)}</span>
        <div class="job-actions">${actions.join("")}</div>
      </div>
      ${errorBlock}
    </article>`;
}

async function onQueueClick(event) {
  const actionButton = event.target.closest("[data-action]");
  if (!actionButton) return;
  const action = actionButton.dataset.action;
  const jobId = actionButton.dataset.id;
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job) return;
  try {
    if (action === "cancel") {
      await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      toast("正在取消任务…", "info");
      pollJobs(false);
    } else if (action === "remove") {
      await api(`/api/jobs/${jobId}`, { method: "DELETE" });
      await pollJobs(false);
    } else if (action === "folder") {
      await api("/api/open-folder", { method: "POST", body: { path: job.output } });
    } else if (action === "play") {
      openVideo(job.download_url, job.source_name);
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

function openVideo(url, title) {
  if (!url) return;
  $("#video-player").src = url;
  $("#video-caption").textContent = title || "";
  openModal("video");
  $("#video-player").play().catch(() => {});
}

/* ---------------- misc actions ---------------- */

async function openOutputDir() {
  const path = state.system?.output_dir;
  if (!path) return;
  try {
    await api("/api/open-folder", { method: "POST", body: { path } });
  } catch (error) {
    toast(error.message, "error");
  }
}

async function quitApp() {
  const active = state.jobs.filter((job) => job.status === "running" || job.status === "queued").length;
  const message = active
    ? `还有 ${active} 个任务正在处理或排队，退出会中断它们。确定退出清影吗？`
    : "确定退出清影吗？退出后本地服务将停止。";
  if (!window.confirm(message)) return;
  try {
    await api("/api/shutdown", { method: "POST" });
    document.body.innerHTML = '<div style="min-height:100vh;display:grid;place-items:center;background:#070a0f;color:#edf3fb;font-family:system-ui;text-align:center"><div><div style="font-size:38px;margin-bottom:10px">✦</div><h1 style="font-size:22px;font-weight:600">清影已退出</h1><p style="color:#8999ad;font-size:13px">可以关闭此页面了。</p></div></div>';
    window.setTimeout(() => window.close(), 600);
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ---------------- presets ---------------- */

$$(".preset-menu button").forEach((button) => {
  button.addEventListener("click", () => applyPreset(button.dataset.preset));
});

function applyPreset(preset) {
  if (!state.probe) {
    toast("请先选择视频，再应用快捷方案", "info");
    return;
  }
  const probe = state.probe;
  const s = state.settings;
  if (preset === "uhd60") {
    s.sr_engine = "fast";
    s.scale = 2;
    s.target_height = probe.height < 2160 ? 2160 : Math.min(4320, probe.height);
    s.interp = "balanced";
    s.fps_custom = false;
    s.fps_mode = "fixed";
    s.fps_value = probe.fps >= 59 ? 120 : 60;
    s.quality = "high";
    s.order = "interp_first";
    toast(`已设为 ${s.target_height}P / ${s.fps_value}fps 方案`, "success", "快捷方案");
  } else if (preset === "upscale") {
    s.sr_engine = "fast";
    s.scale = 2;
    s.target_height = null;
    s.interp = "off";
    toast("已设为 2× 超分方案", "success", "快捷方案");
  } else if (preset === "smooth") {
    s.sr_engine = "off";
    s.target_height = null;
    s.interp = "balanced";
    s.fps_custom = false;
    s.fps_mode = "fixed";
    s.fps_value = probe.fps >= 59 ? 120 : 60;
    toast(`已设为 ${s.fps_value}fps 补帧方案（不放大）`, "success", "快捷方案");
  } else if (preset === "anime") {
    s.sr_engine = "fast";
    s.scale = 2;
    s.target_height = null;
    s.interp = "balanced";
    s.fps_custom = false;
    s.fps_mode = "fixed";
    s.fps_value = probe.fps >= 59 ? 120 : 60;
    s.sharpen = 0.25;
    s.denoise = "light";
    s.order = "interp_first";
    toast("已设为动画修复方案：2× 超分 + 锐化", "success", "快捷方案");
  }
  syncUI();
  markPreviewDirty();
}
