// Small, dependency-free behaviour for the Branchglance site.
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Toolbar clock, like the widget's.
  const clock = $("#clock");
  const fmtClock = () => new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const tickClock = () => { if (clock) clock.textContent = fmtClock(); };
  tickClock();
  setInterval(tickClock, 15000);

  // Usage colour thresholds, as in the widget bands.
  $$(".pct").forEach((el) => {
    const p = +el.dataset.pct;
    if (p >= 80) el.classList.add("hot");
    else if (p >= 50) el.classList.add("warn");
  });

  // 5-hour countdowns.
  const fmtCd = (s, withSec) => {
    s = Math.max(0, s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    return withSec ? `${m}m ${String(sec).padStart(2, "0")}s` : `${m}m`;
  };
  const cds = $$("[data-cd]").map((el) => ({ el, s: +el.dataset.cd, sec: el.hasAttribute("data-sec") || +el.dataset.cd < 3600 }));
  const fmtAge = (s) => (s < 600 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
  const ages = $$("[data-age]").map((el) => ({ el, s: +el.dataset.age }));
  setInterval(() => {
    cds.forEach((c) => { c.s -= 1; c.el.textContent = fmtCd(c.s, c.sec || c.s < 3600); });
    ages.forEach((a) => { a.s += 1; a.el.textContent = fmtAge(a.s); });
  }, 1000);

  // A scripted afternoon in the tree: activities change, one worker asks a question,
  // the docs chat gets its answer. Counts in the toolbar follow along.
  const row = (k) => $(`[data-row="${k}"]`);
  const nWait = $("#n-wait"), waitChip = $(".c-wait");
  const setState = (k, state, act) => {
    const r = row(k); if (!r) return;
    const st = $(".state", r), a = $(".act", r);
    st.className = `state s-${state}`;
    st.textContent = state === "run" ? "Running" : state === "wait" ? "Waiting" : "Done";
    r.classList.toggle("waiting", state === "wait");
    r.classList.toggle("done", state === "done");
    if (act != null) a.textContent = act;
    if (state === "wait" && !reduced) { r.classList.remove("flash"); void r.offsetWidth; r.classList.add("flash"); }
    // A parent's title turns amber while any of its subagents waits.
    let p = r; while (p && !p.classList.contains("parent")) p = p.previousElementSibling;
    if (p) {
      let anyWait = false, n = p.nextElementSibling;
      while (n && !n.classList.contains("parent")) { anyWait ||= n.classList.contains("waiting"); n = n.nextElementSibling; }
      p.classList.toggle("kid-wait", anyWait);
      $(".t b", p).style.color = anyWait || p.classList.contains("waiting") ? "var(--yellow)" : "";
    }
    const count = $$(".row.waiting").length;
    if (nWait && nWait.textContent !== String(count)) {
      nWait.textContent = count;
      waitChip.style.visibility = count ? "" : "hidden";
      if (!reduced) { waitChip.classList.remove("bump"); void waitChip.offsetWidth; waitChip.classList.add("bump"); }
    }
  };
  const setAct = (k, act) => { const a = $(".act", row(k)); if (a) a.textContent = act; };

  const script = [
    () => setAct("api", "Running tests"),
    () => setAct("auth", "Reading files"),
    () => setState("test", "wait", "Needs approval"),
    () => setAct("review", "Reviewing diff"),
    () => setState("docs", "run", "Editing file"),
    () => setAct("docrev", "Checking links"),
    () => setAct("auth", "Editing file"),
    () => setState("review", "done", "Finished"),
    () => setState("test", "run", "Running tests"),
    () => setAct("api", "Editing file"),
    () => setState("review", "run", "Running checks"),
    () => setState("docs", "wait", "Asked a question"),
    () => setAct("auth", "Checking data"),
  ];
  // Start from the markup's initial state: docs is waiting.
  setState("docs", "wait");
  let i = 0;
  const tok = $$("[data-tok]").map((el) => ({ el, v: +el.dataset.tok }));
  const tick = () => {
    script[i % script.length]();
    i += 1;
    tok.forEach((t) => {
      if (t.el.closest(".row.waiting, .row.done")) return;
      t.v = Math.min(t.el.classList.contains("hot") ? 199 : 260, t.v + Math.round(Math.random() * 2));
      t.el.textContent = `${t.v}K`;
    });
  };
  let timer = null;
  const tree = $("#tree");
  if (tree && "IntersectionObserver" in window) {
    new IntersectionObserver(([e]) => {
      if (e.isIntersecting && !timer) timer = setInterval(tick, 2600);
      else if (!e.isIntersecting && timer) { clearInterval(timer); timer = null; }
    }).observe(tree);
  }

  // Reveal on scroll.
  if (!reduced && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { rootMargin: "0px 0px -8% 0px" });
    $$(".prose-band, .sec-head, .spec, .ledger, .pane, .steps").forEach((el, n) => {
      el.classList.add("reveal");
      if (el.classList.contains("spec")) el.style.transitionDelay = `${(n % 3) * 70}ms`;
      io.observe(el);
    });
  }

  // Install tabs.
  const tabs = $$('[role="tab"]');
  const select = (t) => {
    tabs.forEach((x) => {
      const on = x === t;
      x.setAttribute("aria-selected", on);
      x.tabIndex = on ? 0 : -1;
      $("#" + x.getAttribute("aria-controls")).hidden = !on;
    });
  };
  tabs.forEach((t, n) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = tabs[(n + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      select(next); next.focus();
    });
  });
  if (/Mac/i.test(navigator.platform || navigator.userAgent)) select($("#tab-mac"));

  // Copy buttons.
  $$(".copy").forEach((b) => b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#" + b.dataset.copy).textContent);
      b.textContent = "Copied"; b.classList.add("ok");
    } catch { b.textContent = "Select and copy"; }
    setTimeout(() => { b.textContent = "Copy"; b.classList.remove("ok"); }, 1600);
  }));
})();
