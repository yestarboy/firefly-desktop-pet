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

  // 后端广播事件（桌宠窗口也有同名函数，两边共用一套事件名）
  window.__pet_on_py_event = function (event, payload) {
    ChatPanel.onEvent(event, payload);
  };

  // tab 切换：日志页只在可见时轮询，切走就停，省 CPU
  window.__onTabChange = function (name) {
    if (name === "log") LogsPanel.onShow();
    else LogsPanel.onHide();
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
    LogsPanel.bind(bridge);
    SettingsPanel.reload();
  });
})();
