/* ============================================================
   main.js —— 启动入口
   按顺序做四件事：定义全局回调 → 建通道 → 初始化各模块 → 首次载入
   ============================================================ */

(function () {
  "use strict";

  // 后端异步结果回调（必须在通道建立前就存在）
  window.__onTestResult = function (o) {
    SettingsPanel.onTestResult(o);
  };

  // ------------------------------------------------ 3D 角色启动
  //
  // 优先用 babylon-mmd（panel3d/ 构建产物 panel/js/mmd.bundle.js）；
  // 没有构建产物时自动退回 three.js 版本（js/model3d.js）。两个都失败也不影响聊天。

  let pendingModelUrl = null;

  function startThreeFallback() {
    const s = document.createElement("script");
    s.type = "module";
    // 带时间戳，避开 QtWebEngine 的 HTTP 缓存
    s.src = "js/model3d.js?v=" + Date.now();
    s.onerror = () => console.warn("3D：three 版本加载失败");
    document.body.appendChild(s);
  }

  function apply3dModel(url) {
    if (window.BabylonMMDViewer) {
      window.BabylonMMDViewer.load(url);
    } else if (window.Model3D) {
      window.Model3D.load(url);
    }
  }

  function boot3d() {
    const s = document.createElement("script");
    // 带时间戳：QtWebEngine 会缓存这个 1.7MB 的包，不加就会加载到旧版本
    s.src = "js/mmd.bundle.js?v=" + Date.now();
    s.onload = () => {
      if (window.BabylonMMDViewer) {
        console.log("3D：使用 babylon-mmd");
        window.BabylonMMDViewer.init("stage3d");
        if (pendingModelUrl) apply3dModel(pendingModelUrl);
      } else {
        startThreeFallback();
      }
    };
    s.onerror = () => {
      console.log("3D：未找到 mmd.bundle.js，退回 three.js 版本");
      startThreeFallback();
    };
    document.head.appendChild(s);
  }

  // 后端广播事件（桌宠窗口也有同名函数，两边共用一套事件名）
  window.__pet_on_py_event = function (event, payload) {
    ChatPanel.onEvent(event, payload);
    try {
      if (event === "power_event" && typeof SettingsPanel !== "undefined" && SettingsPanel.onPowerEvent) {
        SettingsPanel.onPowerEvent(payload || {});
      }
    } catch (e) {}
    try {
      if (typeof WorkspacePanel !== "undefined") WorkspacePanel.onEvent(event, payload);
    } catch (e) {}

    // 3D 角色也订阅同一批事件（tts_start / tts_end 用来驱动口型）
    if (window.BabylonMMDViewer) {
      try {
        window.BabylonMMDViewer.onEvent(event, payload);
      } catch (e) {}
    }
    if (window.Model3D) {
      try {
        window.Model3D.onEvent(event, payload);
      } catch (e) {}
    }
  };

  // tab 切换：日志页只在可见时轮询，切走就停，省 CPU
  window.__onTabChange = function (name) {
    if (name === "log") LogsPanel.onShow();
    else LogsPanel.onHide();

    // 回到对话页时画布才真正有尺寸，需要重新算一次取景
    if (name === "chat") {
      if (window.BabylonMMDViewer) {
        try { window.BabylonMMDViewer.resize(); } catch (e) {}
      }
      if (window.Model3D) {
        try { window.Model3D.resize(); } catch (e) {}
      }
    }
  };

  new QWebChannel(qt.webChannelTransport, function (ch) {
    const bridge = ch.objects.cfg;
    if (!bridge) {
      console.error("QWebChannel: 没有拿到 cfg 对象");
      return;
    }

    // 先切 tab 再绑定，避免绑定阶段用到隐藏元素
    Tabs.init("chat");
    SideNav.init();
    SettingsPanel.bind(bridge);
    ChatPanel.bind(bridge);
    try {
      if (typeof WorkspacePanel !== "undefined") WorkspacePanel.bind(bridge);
    } catch (e) {
      console.error("[ws] 工作区初始化失败:", e);
    }
    LogsPanel.bind(bridge);
    SettingsPanel.reload();

    // 3D 模型路径由后端扫描目录给出，前端不再猜中文文件名。
    // 面板页在 /panel/index.html，相对路径会变成 /panel/assets/...，所以用绝对路径。
    if (bridge.model3dPath) {
      bridge.model3dPath(function (name) {
        if (!name) return;
        pendingModelUrl = "/assets/models3d/" + encodeURIComponent(name);
        window.MODEL3D_URL = pendingModelUrl;
        apply3dModel(pendingModelUrl);
      });
    }

    // 动作（VMD）已移除，不再向后端询问动作文件

    boot3d();
  });
})();
