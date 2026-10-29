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
    if (mode === "tree") {
      tracker.notch.manual = true;
      tracker.notch.inspecting = true;
      tracker.setNotch(true);
    } else if (mode.startsWith("insights")) {
      localStorage.setItem("branchglanceInsightsRange", mode.endsWith("24h") ? "24h" : "30d");
      tracker.openDashboard();
    } else {
      tracker.setNotch(false);
    }
  });
})();
