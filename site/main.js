// Branchglance site behaviour. No dependencies.
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.documentElement.classList.remove("no-js");
  requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add("loaded")));

  /* ---------- Headline, word by word ---------- */
  $$(".split .line").forEach((line) => {
    const walk = (node) => {
      [...node.childNodes].forEach((n) => {
        if (n.nodeType === 3) {
          const frag = document.createDocumentFragment();
          n.textContent.split(/(\s+)/).forEach((part) => {
            if (!part) return;
            if (/^\s+$/.test(part)) frag.append(part);
            else { const w = document.createElement("span"); w.className = "w"; w.textContent = part; frag.append(w); }
          });
          n.replaceWith(frag);
        } else walk(n);
      });
    };
    walk(line);
  });
  $$(".split").forEach((h) => {
    h.setAttribute("aria-hidden", "false");
    $$(".w", h).forEach((w, i) => (w.style.transitionDelay = `${120 + i * 75}ms`));
    $$(".w", h).forEach((w) => w.setAttribute("aria-hidden", "true"));
    requestAnimationFrame(() => h.classList.add("in"));
  });

  /* ---------- Reveal on scroll ---------- */
  $$("[data-r]").forEach((el) => el.style.setProperty("--d", el.dataset.d || 0));
  if (reduced || !("IntersectionObserver" in window)) $$("[data-r]").forEach((el) => el.classList.add("in"));
  else {
    const io = new IntersectionObserver((es) => es.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }), { rootMargin: "0px 0px -10% 0px" });
    $$("[data-r]").forEach((el) => io.observe(el));
  }

  /* ---------- Clock, bands, countdowns ---------- */
  const clock = $("#clock");
  const tickClock = () => { if (clock) clock.textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); };
  tickClock(); setInterval(tickClock, 15000);

  // Same thresholds as the widget: amber at 50%, red at 70%.
  $$(".pct").forEach((el) => {
    const p = +el.dataset.pct;
    el.classList.toggle("hot", p >= 70);
    el.classList.toggle("warn", p >= 50 && p < 70);
  });

  const fmtCd = (s) => {
    s = Math.max(0, s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(sec).padStart(2, "0")}s`;
  };
  const fmtAge = (s) => (s < 600 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
  const cds = $$("[data-cd]").map((el) => ({ el, s: +el.dataset.cd }));
  const ages = $$("[data-age]").map((el) => ({ el, s: +el.dataset.age }));
  setInterval(() => {
    cds.forEach((c) => { c.s -= 1; c.el.textContent = fmtCd(c.s); });
    ages.forEach((a) => { a.s += 1; a.el.textContent = fmtAge(a.s); });
  }, 1000);

  /* ---------- Hero tree: a scripted afternoon ---------- */
  const row = (k) => $(`[data-row="${k}"]`);
  const nWait = $("#n-wait"), waitChip = $(".c-wait");
  const setState = (k, state, act) => {
    const r = row(k); if (!r) return;
    const st = $(".state", r);
    st.className = `state s-${state}`;
    st.textContent = state === "run" ? "Running" : state === "wait" ? "Waiting" : "Done";
    r.classList.toggle("waiting", state === "wait");
    r.classList.toggle("done", state === "done");
    if (act != null) $(".act", r).textContent = act;
    if (state === "wait" && !reduced) { r.classList.remove("flash"); void r.offsetWidth; r.classList.add("flash"); }
    // A parent's title turns amber while it, or any of its subagents, waits.
    let p = r; while (p && !p.classList.contains("parent")) p = p.previousElementSibling;
    if (p) {
      let any = p.classList.contains("waiting"), n = p.nextElementSibling;
      while (n && !n.classList.contains("parent")) { any ||= n.classList.contains("waiting"); n = n.nextElementSibling; }
      $(".t b", p).style.color = any ? "var(--yellow)" : "";
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
  setState("docs", "wait");
  let si = 0, treeTimer = null;
  const tok = $$("[data-tok]").map((el) => ({ el, v: +el.dataset.tok }));
  const tick = () => {
    script[si++ % script.length]();
    tok.forEach((t) => {
      if (t.el.closest(".row.waiting, .row.done")) return;
      t.v = Math.min(t.el.classList.contains("hot") ? 199 : 260, t.v + Math.round(Math.random() * 2));
      t.el.textContent = `${t.v}K`;
    });
  };
  const tree = $("#tree");
  if (tree && "IntersectionObserver" in window) {
    new IntersectionObserver(([e]) => {
      if (e.isIntersecting && !treeTimer) treeTimer = setInterval(tick, 2600);
      else if (!e.isIntersecting && treeTimer) { clearInterval(treeTimer); treeTimer = null; }
    }).observe(tree);
  }

  /* ---------- Sticky tour, driven by scroll ---------- */
  const tour = $("#tour");
  const tabs = $$(".ttab");
  const panels = $$(".panel");
  const stage = $(".tour-stage");
  const N = panels.length;
  const stickyMQ = matchMedia("(min-width: 901px) and (min-height: 701px)");
  let current = -1;

  const countUp = (el) => {
    const to = parseFloat(el.dataset.count), t0 = performance.now(), dur = 1100;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = `$${(to * e).toFixed(2)}`;
      if (k < 1 && el.closest(".panel.on")) requestAnimationFrame(step);
      else if (k >= 1) el.textContent = `$${to.toFixed(2)}`;
    };
    if (reduced) el.textContent = `$${to.toFixed(2)}`; else requestAnimationFrame(step);
  };
  const activate = (i) => {
    if (i === current) return;
    current = i;
    tabs.forEach((t, n) => { t.setAttribute("aria-selected", n === i); t.tabIndex = n === i ? 0 : -1; });
    panels.forEach((p, n) => p.classList.toggle("on", n === i));
    const glow = getComputedStyle(panels[i]).getPropertyValue("--glow");
    stage.style.setProperty("--stage-glow", glow);
    const money = $(".money", panels[i]); if (money) countUp(money);
  };
  const range = () => {
    const r = tour.getBoundingClientRect();
    const start = r.top + scrollY - 76, len = tour.offsetHeight - (innerHeight - 10);
    return { start, len };
  };
  const onScroll = () => {
    if (!stickyMQ.matches) return;
    const { start, len } = range();
    const p = Math.min(0.9999, Math.max(0, (scrollY - start) / len));
    const i = Math.floor(p * N), local = p * N - i;
    activate(i);
    tabs[i].style.setProperty("--p", local.toFixed(3));
  };
  tabs.forEach((t, n) => t.addEventListener("click", () => {
    if (!stickyMQ.matches) return;
    const { start, len } = range();
    scrollTo({ top: start + ((n + 0.02) / N) * len, behavior: reduced ? "auto" : "smooth" });
  }));
  // Stacked layout: each panel plays when it scrolls into view.
  const stackIO = "IntersectionObserver" in window ? new IntersectionObserver((es) => es.forEach((e) => {
    if (stickyMQ.matches) return;
    e.target.classList.toggle("on", e.isIntersecting);
    const money = $(".money", e.target); if (money && e.isIntersecting) countUp(money);
  }), { threshold: 0.35 }) : null;
  // Panels are stacked absolutely; size the box to the tallest one so nothing overlaps the CTA.
  const sizePanels = () => {
    const box = $(".panels");
    if (!stickyMQ.matches) { box.style.removeProperty("--ph"); return; }
    let h = 0;
    panels.forEach((p) => { h = Math.max(h, p.scrollHeight); });
    box.style.setProperty("--ph", `${h}px`);
  };
  const setMode = () => {
    current = -1;
    sizePanels();
    if (stickyMQ.matches) onScroll();
    else panels.forEach((p) => { p.classList.remove("on"); stackIO && stackIO.observe(p); });
  };
  stickyMQ.addEventListener("change", setMode);
  addEventListener("scroll", onScroll, { passive: true });
  addEventListener("resize", () => { sizePanels(); onScroll(); });
  document.fonts && document.fonts.ready.then(sizePanels);
  setMode();

  /* ---------- FAQ ---------- */
  const qs = $$(".qa button");
  qs.forEach((b) => b.addEventListener("click", () => {
    const open = b.getAttribute("aria-expanded") === "true";
    qs.forEach((o) => { o.setAttribute("aria-expanded", "false"); $("#" + o.getAttribute("aria-controls")).hidden = true; });
    if (!open) { b.setAttribute("aria-expanded", "true"); $("#" + b.getAttribute("aria-controls")).hidden = false; }
  }));

  /* ---------- Install tabs ---------- */
  const ptabs = $$('.tabs [role="tab"]');
  const select = (t) => ptabs.forEach((x) => {
    const on = x === t;
    x.setAttribute("aria-selected", on); x.tabIndex = on ? 0 : -1;
    $("#" + x.getAttribute("aria-controls")).hidden = !on;
  });
  ptabs.forEach((t, n) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = ptabs[(n + (e.key === "ArrowRight" ? 1 : -1) + ptabs.length) % ptabs.length];
      select(next); next.focus();
    });
  });
  if (/Mac/i.test(navigator.platform || navigator.userAgent)) select($("#tab-mac"));

  /* ---------- Copy buttons ---------- */
  $$(".copy").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#" + b.dataset.copy).textContent); b.textContent = "Copied"; b.classList.add("ok"); }
    catch { b.textContent = "Select and copy"; }
    setTimeout(() => { b.textContent = "Copy"; b.classList.remove("ok"); }, 1600);
  }));
})();
