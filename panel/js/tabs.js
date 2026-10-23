/* ============================================================
   tabs.js —— 顶部导航栏切换
   只做一件事：点哪个 tab 就显示对应的 .tab-page
   ============================================================ */

const Tabs = (function () {
  "use strict";

  let tabs = [];
  let pages = [];

  function show(name) {
    if (!name) return;
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    pages.forEach((p) => {
      p.hidden = p.id !== "page-" + name;
    });
    try {
      localStorage.setItem("firefly.tab", name);
    } catch (e) {}

    // 通知各页面模块：切走了 / 切到了，用来启停轮询之类的行为
    if (typeof window.__onTabChange === "function") {
      try {
        window.__onTabChange(name);
      } catch (e) {}
    }
  }

  function init(defaultName) {
    tabs = Array.from(document.querySelectorAll(".tab"));
    pages = Array.from(document.querySelectorAll(".tab-page"));

    tabs.forEach((t) => {
      t.addEventListener("click", () => show(t.dataset.tab));
    });

    let saved = null;
    try {
      saved = localStorage.getItem("firefly.tab");
    } catch (e) {}

    show(saved || defaultName || (tabs[0] && tabs[0].dataset.tab));
  }

  return { init, show };
})();
