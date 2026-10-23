/* ============================================================
   util.js —— 极小的 DOM 工具
   ============================================================ */

(function (global) {
  "use strict";

  global.$id = function (id) {
    return document.getElementById(id);
  };

  global.makeEl = function (tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
  };
})(window);
