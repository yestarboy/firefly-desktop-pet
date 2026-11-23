/* ============================================================
   workspace.js —— 工作区：文件树 / 编辑器 / 跑命令 / 交给萤宝
   对外暴露 bind / onEvent / setVisible
   ============================================================ */

const WorkspacePanel = (function () {
  "use strict";

  let bridge = null;
  let rootPath = "";
  let curDir = "";
  let curFile = "";

  function $(id) {
    return document.getElementById(id);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function logLine(text, cls) {
    const box = $("wsLog");
    if (!box) return;
    const div = document.createElement("div");
    div.className = "line" + (cls ? " " + cls : "");
    div.textContent = text;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function humanSize(n) {
    if (!n) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  // ---------------------------------------------------------------- 文件树

  function renderList(data) {
    const tree = $("wsTree");
    if (!tree) return;
    tree.innerHTML = "";

    if (!data || !data.ok) {
      logLine("[错误] " + ((data && data.error) || "读取目录失败"), "error");
      return;
    }

    curDir = data.path || "";

    if (curDir) {
      const up = document.createElement("div");
      up.className = "ws-item";
      up.innerHTML = '<span class="ico">↩</span><span>..</span>';
      up.onclick = function () {
        const parent = curDir.split("/").slice(0, -1).join("/");
        list(parent);
      };
      tree.appendChild(up);
    }

    (data.items || []).forEach(function (it) {
      const row = document.createElement("div");
      row.className = "ws-item";
      const icon = it.dir ? "📁" : "📄";
      row.innerHTML =
        '<span class="ico">' + icon + "</span><span>" + esc(it.name) + "</span>";
      row.title = it.path + (it.dir ? "" : "  " + humanSize(it.size));
      row.onclick = function () {
        if (it.dir) list(it.path);
        else openFile(it.path, row);
      };
      tree.appendChild(row);
    });
  }

  function list(rel) {
    if (!bridge) {
      console.log("[ws] bridge 未就绪");
      return;
    }
    if (!bridge.wsList) {
      console.log("[ws] 后端没有 wsList 接口");
      return;
    }
    bridge.wsList(rel || "", function (raw) {
      let data = {};
      try {
        data = JSON.parse(raw);
      } catch (e) {
        console.log("[ws] wsList 返回无法解析:", String(raw).slice(0, 120));
        return;
      }
      console.log("[ws] 目录", data.path || "/", "条目", (data.items || []).length, data.error || "");
      renderList(data);
    });
  }

  function openFile(path, row) {
    if (!bridge || !bridge.wsRead) return;
    bridge.wsRead(path, function (raw) {
      let data = {};
      try {
        data = JSON.parse(raw);
      } catch (e) {
        return;
      }
      if (!data.ok) {
        logLine("[错误] " + (data.error || "读取失败"), "error");
        return;
      }
      curFile = data.path;
      $("wsPath").value = data.path;
      $("wsEditor").value = data.content || "";
      if (data.truncated) logLine("[提示] 文件较大，只载入了前面一部分", "dim");

      document.querySelectorAll("#wsTree .ws-item").forEach(function (el) {
        el.classList.remove("active");
      });
      if (row) row.classList.add("active");
    });
  }

  // ---------------------------------------------------------------- 动作

  function save() {
    if (!curFile) {
      logLine("[提示] 先选一个文件", "dim");
      return;
    }
    if (!bridge || !bridge.wsWrite) return;
    bridge.wsWrite(curFile, $("wsEditor").value, function (raw) {
      let data = {};
      try {
        data = JSON.parse(raw);
      } catch (e) {
        return;
      }
      if (data.ok) logLine("已保存 " + data.path + "（" + humanSize(data.size) + "）", "reply");
      else logLine("[错误] " + (data.error || "保存失败"), "error");
    });
  }

  function runCommand() {
    const cmd = ($("wsCmd").value || "").trim();
    if (!cmd) return;
    if (!bridge || !bridge.wsRun) return;

    logLine("$ " + cmd, "cmd");
    $("wsRunBtn").disabled = true;
    bridge.wsRun(cmd, function (raw) {
      $("wsRunBtn").disabled = false;
      let data = {};
      try {
        data = JSON.parse(raw);
      } catch (e) {
        return;
      }
      const out = $("wsOut");
      out.hidden = false;
      out.textContent =
        (data.ok ? "" : "[退出码 " + data.code + "]\n") + (data.output || data.error || "");
      if (data.seconds != null) logLine("完成（" + data.seconds + "s）", data.ok ? "dim" : "error");
    });
  }

  function evolve() {
    const goal = ($("wsGoal").value || "").trim();
    if (!bridge || !bridge.evolveStart) return;

    const state = $("wsEvolveState");
    state.hidden = false;
    state.classList.remove("fail");
    state.textContent = goal ? "目标：" + goal + "\n准备中…" : "没指定目标，让她自己找改进点…";

    $("wsEvolveBtn").disabled = true;
    logLine(goal ? "开始自动迭代：" + goal : "开始自动迭代（自主选题）", "cmd");
    bridge.evolveStart(goal);
  }

  function onEvolveEvent(payload) {
    const kind = payload && payload.kind;
    const text = (payload && payload.text) || "";
    const state = $("wsEvolveState");

    if (kind === "evolve_log") {
      logLine(text, "dim");
    } else if (kind === "evolve_notice") {
      state.hidden = false;
      state.textContent = "🗣 " + text;
      logLine("萤宝：" + text, "reply");
    } else if (kind === "evolve_propose") {
      state.hidden = false;
      state.textContent = text;
      logLine("[选题] " + text.split("\n")[0], "cmd");
    } else if (kind === "done") {
      $("wsEvolveBtn").disabled = false;
      state.hidden = false;
      state.classList.remove("fail");
      state.textContent =
        "✅ 完成，改动在分支 " +
        (payload.branch || "") +
        "\n" +
        text +
        "\n" +
        (payload.diff || "") +
        "\n报告：" +
        (payload.report || "");
      logLine("迭代完成：" + (payload.branch || ""), "reply");
      list(curDir);
    } else if (kind === "fail") {
      $("wsEvolveBtn").disabled = false;
      state.hidden = false;
      state.classList.add("fail");
      state.textContent = "❌ 失败（已自动回滚）：" + text;
      logLine("迭代失败：" + text, "error");
    }
  }

  function ask() {
    const text = ($("wsAsk").value || "").trim();
    if (!text) return;
    if (!bridge || !bridge.wsAsk) return;
    $("wsAsk").value = "";
    $("wsAskBtn").disabled = true;
    logLine("你：" + text, "dim");
    bridge.wsAsk(text);
  }

  // ---------------------------------------------------------------- 事件

  function onEvent(event, payload) {
    if (event === "evolve_event") {
      onEvolveEvent(payload || {});
      return;
    }
    if (event !== "ws_event") return;
    const kind = payload && payload.kind;
    const text = (payload && payload.text) || "";
    console.log("[ws]", kind, text.slice(0, 120));

    if (kind === "log") {
      logLine(text, "cmd");
    } else if (kind === "reply") {
      logLine("萤宝：" + text, "reply");
      // 做完事刷新一下，别让界面停在旧内容
      list(curDir);
      if (curFile) {
        bridge.wsRead(curFile, function (raw) {
          try {
            const d = JSON.parse(raw);
            if (d.ok) $("wsEditor").value = d.content || "";
          } catch (e) {}
        });
      }
    } else if (kind === "error") {
      logLine("[错误] " + text, "error");
    } else if (kind === "done") {
      $("wsAskBtn").disabled = false;
    } else if (kind === "start") {
      logLine("开始处理…", "dim");
    }
  }

  // ---------------------------------------------------------------- 初始化

  function bind(b) {
    bridge = b;

    document.querySelectorAll(".chat-subnav .sub-tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setVisible(btn.dataset.sub === "work");
      });
    });

    $("wsSave").addEventListener("click", save);
    $("wsRunBtn").addEventListener("click", runCommand);
    $("wsAskBtn").addEventListener("click", ask);

    const evoBtn = $("wsEvolveBtn");
    if (evoBtn) evoBtn.addEventListener("click", evolve);
    const goalEl = $("wsGoal");
    if (goalEl) {
      goalEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter") evolve();
      });
    }

    $("wsCmd").addEventListener("keydown", function (e) {
      if (e.key === "Enter") runCommand();
    });
    $("wsAsk").addEventListener("keydown", function (e) {
      if (e.key === "Enter") ask();
    });
    $("wsUp").addEventListener("click", function () {
      const parent = curDir.split("/").slice(0, -1).join("/");
      list(parent);
    });

    if (bridge.wsRoot) {
      bridge.wsRoot(function (raw) {
        try {
          const d = JSON.parse(raw);
          if (d.ok) {
            rootPath = d.root;
            const label = $("wsRootLabel");
            label.textContent = rootPath;
            label.title = rootPath;
          }
        } catch (e) {}
      });
    }
    list("");
  }

  function setVisible(show) {
    const ws = $("workspace");
    const listEl = $("chatList");
    const inputEl = document.querySelector("#page-chat .chat-input");
    if (!ws) return;

    ws.hidden = !show;
    if (listEl) listEl.style.display = show ? "none" : "";
    if (inputEl) inputEl.style.display = show ? "none" : "";

    document.querySelectorAll(".chat-subnav .sub-tab").forEach(function (b) {
      b.classList.toggle("active", (b.dataset.sub === "work") === show);
    });
  }

  return { bind: bind, onEvent: onEvent, setVisible: setVisible };
})();
