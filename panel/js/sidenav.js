/* ============================================================
   sidenav.js —— 设置页左侧导航
   自动从 [data-nav] 的卡片生成目录；点击平滑滚动，滚动时高亮
   ============================================================ */

const SideNav = (function () {
  "use strict";

  let links = [];

  function build() {
    const nav = $id("sideNav");
    if (!nav) return;

    const cards = Array.from(
      document.querySelectorAll("#page-setting [data-nav]")
    );
    nav.innerHTML = "";
    links = [];

    cards.forEach((card) => {
      if (!card.id) return;

      const a = document.createElement("a");
      a.href = "#" + card.id;
      a.textContent = card.dataset.nav || card.id;
      a.addEventListener("click", (e) => {
        e.preventDefault();
        // 用 block:"start" 顶到最上面；scroll-margin-top 抵消粘顶的导航栏
        card.scrollIntoView({ behavior: "smooth", block: "start" });
        markActive(card);
      });

      nav.appendChild(a);
      links.push({ a, card });
    });

    observe(cards);
  }

  function markActive(card) {
    links.forEach((l) => l.a.classList.toggle("active", l.card === card));
  }

  function observe(cards) {
    if (!("IntersectionObserver" in window) || !cards.length) {
      links.forEach((l) => l.a.classList.add("active"));
      return;
    }

    const io = new IntersectionObserver(
      (entries) => {
        // 取当前可见卡片里最靠上的那个
        const visible = entries.filter((e) => e.isIntersecting);
        if (!visible.length) return;
        visible.sort(
          (x, y) => x.boundingClientRect.top - y.boundingClientRect.top
        );
        markActive(visible[0].target);
      },
      {
        // 顶部留出导航栏高度，底部收窄，避免"两个同时高亮"
        rootMargin: "-64px 0px -55% 0px",
        threshold: 0,
      }
    );

    cards.forEach((c) => io.observe(c));
  }

  function init() {
    build();
  }

  return { init, build };
})();
