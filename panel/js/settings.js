/* ============================================================
   settings.js —— 设置页的全部逻辑
   对外只暴露 bind / reload / collect / onTestResult 四个方法
   ============================================================ */

const SettingsPanel = (function () {
  "use strict";

  let bridge = null;
  let currentMic = "";      // 配置里的麦克风索引（设备列表是异步来的，先记住）

  // 与后端 EDITABLE_KEYS 一一对应：这些字段会被读写
  const FIELDS = [
    "provider",
    "model",
    "base_url",
    "api_key",
    "temperature",
    "max_tokens",
    "system_prompt",
    "tts_ref_wav",
    "tts_speed",
    "stt_mic",
    "voice_mode",
    "voice_hotkey",
  ];

  // ---- 热键捕获 ----
  let capturingKey = false;

  function normalizeKey(e) {
    if (/^F([1-9]|1[0-2])$/.test(e.key)) return e.key;
    const simple = { " ": "Space", Tab: "Tab", CapsLock: "CapsLock", "`": "Backquote" };
    if (simple[e.key]) return simple[e.key];
    if (e.key === "Control")
      return e.code === "ControlRight" ? "RightCtrl" : "LeftCtrl";
    if (e.key === "Shift")
      return e.code === "ShiftRight" ? "RightShift" : "LeftShift";
    if (e.key === "Alt")
      return e.code === "AltRight" ? "RightAlt" : "LeftAlt";
    if (e.key === "Escape") return "__cancel__";
    return null;
  }

  // ---------- 小工具 ----------

  function setStatus(text, cls) {
    const el = $id("status");
    el.textContent = text;
    el.className = "status" + (cls ? " " + cls : "");
  }

  function setNote(id, text, cls) {
    const el = $id(id);
    if (!el) return;
    el.textContent = text;
    el.className = "note" + (cls ? " " + cls : "");
  }

  // ---------- 数据 -> 界面 ----------

  function fillVoices(voices, current) {
    const sel = $id("tts_ref_wav");
    sel.innerHTML = "";

    if (!voices.length) {
      const o = makeEl("option", null, "（firefly 目录下没有 wav）");
      o.value = "";
      sel.appendChild(o);
      return;
    }

    voices.forEach((v) => {
      const o = makeEl(
        "option",
        null,
        v.file + "  ·  " + v.seconds + "s  ·  " + v.text
      );
      o.value = v.file;
      if (v.file === current) o.selected = true;
      sel.appendChild(o);
    });
  }

  function fill(state) {
    const c = state.config || {};

    currentMic =
      c.stt_mic === undefined || c.stt_mic === null ? "" : String(c.stt_mic);

    FIELDS.forEach((k) => {
      const el = $id(k);
      if (!el) return;
      // stt_mic 是 select，选项要等设备列表回来才填，这里跳过
      if (k === "stt_mic") return;
      if (c[k] !== undefined && c[k] !== null) el.value = c[k];
    });

    fillVoices(state.voices || [], c.tts_ref_wav || "");
    loadDevices();
    $id("stateBox").textContent = JSON.stringify(state.status || {}, null, 2);
  }

  // ---------- 麦克风设备 ----------

  function fillMicDevices(list) {
    const sel = $id("stt_mic");
    if (!sel) return;

    sel.innerHTML = "";

    const def = makeEl("option", null, "系统默认");
    def.value = "";
    sel.appendChild(def);

    (list || []).forEach((d) => {
      const o = makeEl(
        "option",
        null,
        "[" + d.index + "] " + d.name + (d.default ? "（默认）" : "")
      );
      o.value = String(d.index);
      sel.appendChild(o);
    });

    sel.value = currentMic;
    if (sel.value !== currentMic) sel.value = ""; // 配置里的设备已不在
  }

  function loadDevices() {
    if (!bridge || !bridge.getSttDevices) return;
    setNote("micMsg", "读取设备中…", "");

    bridge.getSttDevices(function (raw) {
      let o = {};
      try {
        o = JSON.parse(raw);
      } catch (e) {
        setNote("micMsg", "设备列表解析失败", "err");
        return;
      }
      if (o.error) {
        fillMicDevices([]);
        setNote("micMsg", o.error, "err");
        return;
      }
      fillMicDevices(o.devices);
      setNote("micMsg", "已找到 " + (o.devices || []).length + " 个输入设备", "ok");
    });
  }

  // ---------- 界面 -> 数据 ----------

  function collect() {
    const out = {};

    FIELDS.forEach((k) => {
      const el = $id(k);
      if (!el) return;

      let v = el.value;

      if (k === "temperature") v = parseFloat(v) || 0.9;
      else if (k === "tts_speed") v = parseFloat(v) || 1.0;
      else if (k === "max_tokens") v = parseInt(v, 10) || 512;
      else if (typeof v === "string") v = v.trim();

      out[k] = v;
    });

    return out;
  }

  // ---------- 与后端交互 ----------

  function reload() {
    if (!bridge) return;
    setStatus("读取中…");
    bridge.loadConfig(function (raw) {
      try {
        const state = JSON.parse(raw);
        if (state.error) throw new Error(state.error);
        fill(state);
        setStatus("已载入", "ok");
      } catch (e) {
        setStatus("读取失败：" + e.message, "err");
      }
    });
  }

  function save() {
    if (!bridge) return;
    const btn = $id("save");
    btn.disabled = true;

    bridge.saveConfig(JSON.stringify(collect()), function (res) {
      btn.disabled = false;

      let r = { ok: false, error: "返回格式异常" };
      try {
        r = JSON.parse(res);
      } catch (e) {}

      if (r.ok) {
        setNote("saveMsg", "✓ 已保存并立即生效", "ok");
        setStatus("已保存", "ok");
      } else {
        setNote("saveMsg", "✗ " + (r.error || "未知错误"), "err");
        setStatus("保存失败", "err");
      }
    });
  }

  function onTestResult(o) {
    if (!o) return;
    if (o.kind === "llm") {
      setNote("llmMsg", (o.ok ? "✓ " + o.ms + "ms  " : "✗ ") + o.text,
              o.ok ? "ok" : "err");
    } else if (o.kind === "tts") {
      setNote("ttsMsg", o.ok ? "✓ 已播放，" + o.ms + "ms" : "✗ " + o.text,
              o.ok ? "ok" : "err");
    } else if (o.kind === "mic") {
      setNote("micMsg", o.ok ? "✓ 听到：" + (o.text || "（空）") : "✗ " + o.text,
              o.ok ? "ok" : "err");
    }
  }

  // ---------- 绑定事件 ----------

  function bind(b) {
    bridge = b;

    $id("save").addEventListener("click", save);
    $id("reload").addEventListener("click", reload);

    $id("toggleKey").addEventListener("click", function () {
      const inp = $id("api_key");
      const show = inp.type === "password";
      inp.type = show ? "text" : "password";
      this.textContent = show ? "隐藏" : "显示";
    });

    $id("testLLM").addEventListener("click", function () {
      setNote("llmMsg", "测试中…（会真实调用一次 API）", "");
      bridge.testLLM("你好，用一句话介绍你自己");
    });

    $id("testTTS").addEventListener("click", function () {
      setNote("ttsMsg", "合成中…", "");
      bridge.testTTS("你好呀，我是流萤，很高兴认识你。");
    });

    $id("refreshMic").addEventListener("click", loadDevices);

    $id("hotkeyBtn").addEventListener("click", function () {
      capturingKey = true;
      this.textContent = "请按键…（Esc 取消）";
      $id("voice_hotkey").focus();
    });

    document.addEventListener("keydown", function (e) {
      if (!capturingKey) return;
      e.preventDefault();

      const name = normalizeKey(e);
      if (!name) return;

      capturingKey = false;
      $id("hotkeyBtn").textContent = "按下要用的键";
      if (name !== "__cancel__") $id("voice_hotkey").value = name;
    });

    $id("testMic").addEventListener("click", function () {
      if (!bridge || !bridge.testMic) return;
      setNote("micMsg", "请说话…说完停顿一下", "");
      bridge.testMic();
    });
  }

  return { bind, reload, collect, onTestResult };
})();
