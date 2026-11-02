(() => {
  const mode = new URLSearchParams(location.search).get("capture");
  window.pywebview = {api: {
    set_notch: () => Promise.resolve(true),
    get_keep_open: () => Promise.resolve({keepOpen: false, keepOpenRevision: 0}),
    get_widget_lock: () => Promise.resolve({locked: false, expanded: false, revision: 0}),
    get_monitors: () => Promise.resolve([])
  }};
  window.addEventListener("load", async () => {
    const snapshot = await (await fetch("/api/stats")).json();
    const tracker = window.__tokenTrackerTest;
    tracker.state.providerPolicy = snapshot.providerPolicies;
    tracker.state.providerPolicyReady = true;
    tracker.render(snapshot);
    tracker.setupNotch();
    // Live mode: poll /api/live exactly as the app does (GET only), so running chats arrive with the evidence the tree needs.
    if ((mode === "tree" || mode === "collapsed") && new URLSearchParams(location.search).get("live") === "1") {
      tracker.state.snapshot = tracker.state.snapshot || snapshot;
      // The app's own start-up load can hold pollLive back; retry until a live sample has been accepted.
      for (let attempt = 0; attempt < 12 && !tracker.state.live && tracker.pollLive; attempt++) {
        await tracker.pollLive();
        if (!tracker.state.live) await new Promise(resolve => setTimeout(resolve, 150));
      }
    }
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
      if (new URLSearchParams(location.search).get("menu") === "1") tracker.toggleToolbarMenu();
      // A headless browser cannot perform the native window resize requested by
      // set_notch. Match the capture surface to its toolbar instead of applying
      // viewport corrections intended for the real WebView host.
      const surface = document.querySelector('.layout');
      surface.style.setProperty('width', '100%', 'important');
      surface.style.setProperty('min-width', '100%', 'important');
    } else if (mode === "settings") {
      tracker.openDashboard();
      window.TokenTrackerDashboard.selectTab("settings");
    } else if (mode.startsWith("insights")) {
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
  });
})();
