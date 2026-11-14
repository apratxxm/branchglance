(() => {
  const mode = new URLSearchParams(location.search).get("capture");
  window.pywebview = {api: {
    set_notch: () => Promise.resolve(true),
    get_keep_open: () => Promise.resolve({keepOpen: false, keepOpenRevision: 0}),
    get_widget_lock: () => Promise.resolve({locked: false, expanded: false, revision: 0}),
    get_monitors: () => Promise.resolve([])
  }};
  window.addEventListener("load", async () => {
    const liveCapture = (mode === "tree" || mode === "collapsed") && new URLSearchParams(location.search).get("live") === "1";
    const prefetched = window.__captureLive;
    if (liveCapture && !prefetched) throw new Error("Missing prefetched live snapshot");
    const snapshot = liveCapture ? prefetched.stats : await (await fetch("/api/stats")).json();
    const tracker = window.__tokenTrackerTest;
    tracker.state.providerPolicy = snapshot.providerPolicies;
    tracker.state.providerPolicyReady = true;
    if (liveCapture) {
      const sample = prefetched.live, sampleAt = Date.parse(sample.generatedAt);
      if (!Array.isArray(sample.chats) || !Number.isFinite(sampleAt) || sampleAt <= 0 || sampleAt > Date.now() || sampleAt < Date.parse(snapshot.generatedAt)) throw new Error("Invalid live capture snapshot");
      tracker.state.live = sample;
      tracker.state.liveAcceptedGeneratedAt = sampleAt;
      tracker.state.liveSampleAccepted = true;
      tracker.applyLive(snapshot, sample);
    }
    tracker.render(snapshot);
    tracker.setupNotch();
    // routing=codex|claude|both: a healthy first reading, then a crossing, so the routing alert fires as it does live.
    const routing = new URLSearchParams(location.search).get("routing");
    if (routing) {
      const now = Date.now() / 1000;
      const quota = {codex: {Codex: [61, 252 * 60], Claude: [24, 47 * 60]},
                     claude: {Codex: [37, 161 * 60], Claude: [61, 57 * 60]},
                     both: {Codex: [61, 252 * 60], Claude: [96, 57 * 60]}}[routing];
      const low = JSON.parse(JSON.stringify(snapshot));
      for (const provider of low.providers) {
        const [percent, seconds] = quota[provider.name];
        provider.fiveHour = {percent, resetAt: now + seconds};
        provider.limitsUpdatedAt = new Date().toISOString();
      }
      tracker.render(low);
    }
    if (mode === "tree") {
      tracker.notch.manual = true;
      tracker.notch.inspecting = true;
      tracker.setNotch(true);
      // A headless browser cannot perform the native window resize requested by
      // set_notch. Match the capture surface to its toolbar instead of applying
      // viewport corrections intended for the real WebView host.
      const surface = document.querySelector('.layout');
      surface.style.setProperty('width', '100%', 'important');
      surface.style.setProperty('min-width', '100%', 'important');
    } else if (mode === "settings") {
      tracker.openDashboard();
      window.TokenTrackerDashboard.selectTab("settings");
    } else if (mode?.startsWith("insights")) {
      // insights-<range>[-lines][-hide][-hover]: chart direction variants for review captures.
      const range = mode.split("-")[1];
      localStorage.setItem("branchglanceInsightsRange", ["24h", "7d", "30d", "all"].includes(range) ? range : "30d");
      localStorage.setItem("branchglanceChartStyle", mode.includes("-lines") ? "lines" : "stacked");
      const waitFor = selector => new Promise(resolve => {
        const started = Date.now();
        const poll = () => {
          const found = document.querySelector(selector);
          if (found || Date.now() - started > 8000) resolve(found);
          else setTimeout(poll, 100);
        };
        poll();
      });
      tracker.openDashboard();
      if (mode.includes("-hide")) {
        const toggle = await waitFor('[data-dashboard-provider="Codex"]');
        toggle?.click();
      }
      if (mode.includes("-hover")) {
        await waitFor(".dash-point");
        const points = document.querySelectorAll(".dash-point");
        const point = points[Math.floor(points.length * 0.62)];
        if (point) {
          const box = point.getBoundingClientRect();
          point.dispatchEvent(new PointerEvent("pointerover", {bubbles: true, clientX: box.left + box.width / 2, clientY: box.top + box.height / 2}));
        }
      }
    } else {
      tracker.setNotch(false);
    }
    if (liveCapture) {
      // A synchronous acknowledgement makes a missing or failed application visible to the harness.
      const ready = new XMLHttpRequest();
      ready.open("GET", "/capture-ready", false);
      ready.send();
      if (ready.status !== 200) throw new Error("Live capture readiness acknowledgement failed");
    }
  });
})();
