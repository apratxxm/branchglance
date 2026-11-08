// Branchglance site. Motion follows tasteskill: a hero entrance (CSS, once), one loop per window
// with a pause disc, click-only tabs, and one scroll moment (word-highlight) in its own section.
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const still = document.documentElement.dataset.motion === "off";

  const year = $("#year");
  if (year) year.textContent = new Date().getFullYear();

  /* ---------- Loops: 3.2 s cycles, one change every 1.6 s, pause disc, restart on pointer ---------- */
  const loop = (host, step) => {
    if (!host || still) return;
    let on = true, t = 0, timer = null;
    const pause = $(".pause", host);
    const run = () => { clearInterval(timer); t = 0; step(t); timer = setInterval(() => step(++t), 1600); };
    const stop = () => { clearInterval(timer); timer = null; };
    pause && pause.addEventListener("click", () => {
      on = !on;
      pause.setAttribute("aria-pressed", String(!on));
      pause.setAttribute("aria-label", on ? "Pause the animation" : "Play the animation");
      on ? run() : stop();
    });
    host.addEventListener("pointerenter", () => on && run());
    host.addEventListener("focusin", () => on && run());
    run();
  };

  // Hero: the test worker asks a question, the toolbar count follows, then it carries on.
  const test = $("#row-test"), parent = test && test.previousElementSibling, wait = $("#w-wait");
  const setTest = (waiting) => {
    if (!test) return;
    const st = $(".state", test), act = $(".act", test);
    test.classList.toggle("waiting", waiting);
    parent.classList.toggle("kid-wait", waiting);
    st.className = `state ${waiting ? "s-wait" : "s-run"}`;
    st.textContent = waiting ? "Waiting" : "Running";
    act.textContent = waiting ? "Needs approval" : "Running tests";
    wait.textContent = waiting ? "! 2" : "! 1";
  };
  loop($(".widget"), (t) => setTest(t % 2 === 1));

  // Insights: the window flips between cost and tokens.
  const app = $(".app"), n = $(".o-n", app), lbl = $(".o-l", app), segs = $$(".o-seg span", app);
  loop(app, (t) => {
    const tok = t % 2 === 1;
    app.classList.toggle("show-tok", tok);
    n.textContent = tok ? "3.24B" : "$742.40";
    lbl.textContent = tok ? "Tokens · 30 days" : "API-equivalent cost · 30 days";
    segs.forEach((s) => s.classList.toggle("on", (s.dataset.v === "tok") === tok));
  });

  /* ---------- Tabs: click only ---------- */
  const tabs = $$('[role="tab"]');
  const list = $(".tabs");
  const place = (tab) => {
    list.style.setProperty("--ti-y", `${tab.offsetTop}px`);
    list.style.setProperty("--ti-h", `${tab.offsetHeight}px`);
  };
  const select = (tab) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      t.getAttribute("aria-controls").split(" ").forEach((id) => { $("#" + id).hidden = !on; });
    });
    place(tab);
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      const k = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
      if (!k) return;
      e.preventDefault();
      const next = tabs[(i + k + tabs.length) % tabs.length];
      select(next); next.focus();
    });
  });
  if (tabs.length) { place(tabs[0]); addEventListener("resize", () => place($('[role="tab"][aria-selected="true"]'))); }

  /* ---------- Menu sheet ---------- */
  const toggle = $(".nav__toggle"), sheet = $("#sheet");
  const setSheet = (open) => {
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    sheet.hidden = !open;
    document.body.style.overflow = open ? "hidden" : "";
  };
  toggle.addEventListener("click", () => setSheet(toggle.getAttribute("aria-expanded") !== "true"));
  sheet.addEventListener("click", (e) => { if (e.target.closest("a")) setSheet(false); });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && !sheet.hidden) { setSheet(false); toggle.focus(); } });

  /* ---------- Moment: word-highlight (scrubbed both ways, its own section only) ---------- */
  const text = $(".mo-wh__text");
  if (text && !still) {
    const wrapWords = (node) => {
      [...node.childNodes].forEach((c) => {
        if (c.nodeType === 3) {
          const f = document.createDocumentFragment();
          c.textContent.split(/(\s+)/).forEach((p) => {
            if (!p) return;
            if (/^\s+$/.test(p)) f.append(p);
            else { const s = document.createElement("span"); s.className = "mo-w"; s.textContent = p; f.append(s); }
          });
          c.replaceWith(f);
        } else wrapWords(c);
      });
    };
    wrapWords(text);
    text.closest("section").classList.add("mo-wh");
    const words = $$(".mo-w", text);
    let ticking = false;
    const paint = () => {
      ticking = false;
      const mid = innerHeight * 0.55;
      words.forEach((w) => w.classList.toggle("is-on", w.getBoundingClientRect().top < mid));
    };
    addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(paint); } }, { passive: true });
    addEventListener("resize", paint);
    paint();
  }
})();
