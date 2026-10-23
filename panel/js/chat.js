/* ============================================================
   chat.js —— 对话页的全部逻辑
   对外暴露 bind / addMsg / onEvent / send
   ============================================================ */

const ChatPanel = (function () {
  "use strict";

  let bridge = null;
  let list = null;
  let curBubble = null; // 正在流式写入的那个气泡

  function scrollToBottom() {
    list.scrollTop = list.scrollHeight;
  }

  /**
   * 追加一条消息。
   * @param {"me"|"bot"} who
   * @param {string} text
   * @returns {HTMLElement} 气泡元素，流式时直接往里追加文字
   */
  function addMsg(who, text) {
    const row = makeEl("div", "chat-row " + (who === "me" ? "me" : "bot"));
    const bubble = makeEl("div", "bubble", text || "");
    row.appendChild(bubble);
    list.appendChild(row);
    scrollToBottom();
    return bubble;
  }

  function send() {
    const input = $id("chatInput");
    const text = (input.value || "").trim();
    if (!text || !bridge) return;

    input.value = "";
    curBubble = null;

    // 不再本地插入"我"的气泡：后端会广播 user_text，
    // 这样从桌宠窗口或语音输入的句子也能在面板里显示
    if (typeof bridge.askLLM === "function") {
      bridge.askLLM(text);
    } else {
      addMsg("bot", "[未接通] 后端还没有 askLLM 接口");
    }
  }

  /** 后端广播过来的事件（与桌宠窗口共用同一套事件名） */
  function onEvent(event, payload) {
    payload = payload || {};

    if (event === "user_text") {
      addMsg("me", payload.text || "");
    } else if (event === "llm_start") {
      curBubble = addMsg("bot", "");
    } else if (event === "llm_delta") {
      if (curBubble) {
        curBubble.textContent += payload.text || "";
        scrollToBottom();
      }
    } else if (event === "llm_reply") {
      if (curBubble) {
        curBubble.textContent = payload.answer || "(空回复)";
        curBubble = null;
        scrollToBottom();
      }
    } else if (event === "llm_error") {
      addMsg("bot", "[错误] " + (payload.error || "未知错误"));
      curBubble = null;
    }
    // llm_status / pet_motion / tts_start / tts_end 在面板里不处理
  }

  function bind(b) {
    bridge = b;
    list = $id("chatList");

    $id("chatSend").addEventListener("click", send);
    $id("chatInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
  }

  return { bind, addMsg, onEvent, send };
})();
