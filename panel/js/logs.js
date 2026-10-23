/* ============================================================
   logs.js —— 日志页
   读 logs/*.log 的尾部，支持自动刷新
   ============================================================ */

const LogsPanel = (function () {
  "use strict";

  let bridge = null;
  let timer = null;

  function setChip(id, text, ok) {
    const el = $id(id);
    if (!el) return;
    el.textContent = text;
    el.className = "chip " + (ok ? "ok" : "err");
  }

  function refreshServices() {
    if (!bridge || !bridge.services) return;
    bridge.services(function (raw) {
      let o = {};
      try {
        o = JSON.parse(raw);
      } catch (e) {
        return;
      }
      setChip("svcTts", o.tts ? "TTS 在线" : "TTS 离线", !!o.tts);
      setChip("svcStt", o.stt ? "STT 在线" : "STT 离线", !!o.stt);

      const box = $id("logPids");
      if (box) {
        const pids = o.pids || [];
        box.textContent = pids.length
          ? pids.map(function (p) { return p.name + " → PID " + p.pid; }).join("    ")
          : "暂无 PID 记录（用 start_all.vbs 启动时才会写入）";
      }
    });
  }

  function refreshLog() {
    if (!bridge || !bridge.readLog) return;

    const name = $id("logName").value;
    const lines = parseInt($id("logLines").value, 10) || 200;
    const pre = $id("logView");

    // 如果用户已经滚到最底部，刷新后继续贴底；否则保持位置
    const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;

    bridge.readLog(name, lines, function (text) {
      pre.textContent = text || "（空）";
      if (stick) pre.scrollTop = pre.scrollHeight;
    });
  }

  function refreshAll() {
    refreshServices();
    refreshLog();
  }

  function startAuto() {
    stopAuto();
    const box = $id("logAuto");
    if (box && box.checked) timer = setInterval(refreshAll, 2000);
  }

  function stopAuto() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function bind(b) {
    bridge = b;

    $id("logRefresh").addEventListener("click", refreshAll);
    $id("logName").addEventListener("change", refreshLog);
    $id("logAuto").addEventListener("change", startAuto);

    refreshAll();
  }

  /** 切到这个 tab 时调用 */
  function onShow() {
    refreshAll();
    startAuto();
  }

  /** 切走时停止轮询，省 CPU */
  function onHide() {
    stopAuto();
  }

  return { bind, refreshAll, onShow, onHide };
})();
