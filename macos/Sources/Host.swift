import Carbon.HIToolbox
import Cocoa
import ServiceManagement
import SwiftUI
import WebKit

final class Host: NSObject, NSApplicationDelegate, WKScriptMessageHandlerWithReply, WKNavigationDelegate, WKUIDelegate {
    let panel = NotchPanel()
    var webView: WKWebView!
    var server: Process?
    var statusItem: NSStatusItem?
    var keepOpen = false, keepOpenRevision = 0
    var expanded = false, lockRevision = 0
    /// The notch never moves, so Lock only records the choice (and keeps Settings and the toolbar in step).
    var widgetLocked = UserDefaults.standard.bool(forKey: "widgetLocked")
    var reducedMotion = false
    /// Size the web UI last asked for; shown only while the notch is open.
    var requestedSize = compactSizes["inspect"]!
    /// The web UI is showing something worth keeping open (inspection, dashboard, new work).
    var webWantsOpen = false
    var hovering = false
    var isOpen = false
    let settingsToken = UUID().uuidString + UUID().uuidString
    var settingsWindow: NSWindow?

    let media = NowPlaying()
    let monitor = SystemMonitor()
    let levels = AudioLevels()
    lazy var lockScreen = LockScreenController(media: media)
    let tabs = TabState()
    var container: NotchContainerView!
    var mediaHost: NSHostingView<MediaView>!
    var hudKind: HUDKind?
    var hudWork: DispatchWorkItem?
    var placedFrame = NSRect.zero
    /// Opened with the inspection shortcut; stays open until the shortcut is pressed again.
    var shortcutOpened = false
    /// Hidden with the widget's Close button or Show/Hide; monitoring carries on.
    var hiddenByUser = false
    var hotKey: EventHotKeyRef?
    /// Open the notch by itself when the tracker reports new work. Off: only hover opens it.
    var autoOpen = UserDefaults.standard.object(forKey: "autoOpen") as? Bool ?? true {
        didSet { UserDefaults.standard.set(autoOpen, forKey: "autoOpen") }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)
        startServerIfNeeded()
        buildStatusItem()

        let config = WKWebViewConfiguration()
        config.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "bg")
        config.userContentController.addUserScript(WKUserScript(
            source: Self.bridgeScript, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: config)
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = self
        webView.uiDelegate = self
        container = NotchContainerView(frame: .zero)
        mediaHost = NSHostingView(rootView: MediaView(media: media))
        let header = NSHostingView(rootView: TabHeader(tabs: tabs, notchWidth: notchWidthForHeader))
        container.header = header
        container.content = webView
        tabs.onSelect = { [weak self] _ in self?.tabChanged() }
        container.onHover = { [weak self] inside in self?.hoverChanged(inside) }
        panel.contentView = container
        panel.acceptsMouseMovedEvents = true

        container.header?.alphaValue = 0
        webView.alphaValue = 0
        collapse(animate: false)
        media.onChange = { [weak self] in self?.mediaChanged() }
        monitor.onEvent = { [weak self] kind in self?.showHUD(kind) }
        monitor.start()
        lockScreen.onLockChange = { [weak self] locked in if locked { self?.hovering = false; self?.hoverOpened = false; self?.updateOpenState() } }
        panel.orderFrontRegardless()
        loadWhenReady(attempt: 0)
        registerShortcut()

        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.isOpen ? { self.syncContentSize(); self.place(size: self.openSize, animate: false) }() : self.collapse(animate: false)
        }
    }

    func applicationWillTerminate(_ note: Notification) { server?.terminate() }

    // MARK: Python backend

    func serverIsUp() -> Bool {
        var request = URLRequest(url: pageURL, timeoutInterval: 0.5)
        request.httpMethod = "HEAD"
        let done = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: request) { _, response, _ in
            ok = (response as? HTTPURLResponse)?.statusCode == 200
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 1)
        return ok
    }

    func startServerIfNeeded() {
        if serverIsUp() { return }
        // Repo root: BRANCHGLANCE_ROOT, else the checkout build.sh recorded in Info.plist.
        guard let rootPath = ProcessInfo.processInfo.environment["BRANCHGLANCE_ROOT"]
                ?? Bundle.main.object(forInfoDictionaryKey: "BranchglanceRoot") as? String else {
            NSLog("Branchglance: no repo root; set BRANCHGLANCE_ROOT")
            return
        }
        let root = URL(fileURLWithPath: rootPath)
        let venvPython = root.appendingPathComponent(".venv/bin/python")
        let process = Process()
        process.executableURL = FileManager.default.isExecutableFile(atPath: venvPython.path)
            ? venvPython : URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-m", "token_tracker.macos_server", "--port", String(serverPort)]
        process.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        // Opened from Finder or at login, apps get launchd's bare PATH; use the login shell's,
        // so the server finds the codex and claude CLIs the way a terminal does.
        if let path = loginShellPath() { env["PATH"] = path }
        env["BRANCHGLANCE_SETTINGS_TOKEN"] = settingsToken
        env["BRANCHGLANCE_POLICY_FILE"] = env["BRANCHGLANCE_POLICY_FILE"] ?? NSHomeDirectory()
            + "/Library/Application Support/Branchglance/policy.json"
        process.environment = env
        do { try process.run(); server = process } catch { NSLog("Branchglance: server start failed: \(error)") }
    }

    func loginShellPath() -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        // Interactive too, since version managers such as nvm set PATH in .zshrc. A marker
        // separates the value from anything the startup files print.
        process.arguments = ["-i", "-l", "-c", "printf '\\n__BG_PATH__%s' \"$PATH\""]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return nil }
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline { usleep(20_000) }
        if process.isRunning { process.terminate(); return nil }
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard let marker = output.range(of: "__BG_PATH__", options: .backwards) else { return nil }
        let path = String(output[marker.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        return path.contains("/usr/bin") ? path : nil
    }

    func loadWhenReady(attempt: Int) {
        DispatchQueue.global().async {
            let up = self.serverIsUp()
            DispatchQueue.main.async {
                if up || attempt >= 40 { self.webView.load(URLRequest(url: pageURL)) }
                else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.loadWhenReady(attempt: attempt + 1) } }
            }
        }
    }

    // MARK: Hover

    var hoverWork: DispatchWorkItem?
    var hoverPoll: Timer?
    /// Set by Minimize: stay closed (no peek, no dwell) until the pointer has left the notch.
    var hoverSuppressed = false

    /// Open the inspection view after a short dwell, close it shortly after the pointer leaves,
    /// so passing the cursor across the menu bar doesn't flicker the notch.
    /// Two stages, like Atoll: hovering makes the closed notch swell slightly (a peek);
    /// staying on it for `dwell` seconds opens it fully. Leaving closes it after a short grace.
    func hoverChanged(_ inside: Bool) {
        // Tracking areas are rebuilt as the panel resizes, which re-sends "entered".
        guard inside != hovering else { return }
        // A resize or animation can report "exited" while the pointer is still on the panel;
        // the poll below notices when it really leaves, since AppKit won't send a second exit.
        if !inside, pointerOnPanel() { return }
        hoverWork?.cancel()
        hovering = inside
        hoverPoll?.invalidate(); hoverPoll = nil
        if inside {
            let poll = Timer(timeInterval: 0.15, repeats: true) { [weak self] _ in
                guard let self, self.hovering, !self.pointerOnPanel() else { return }
                self.hoverChanged(false)
            }
            RunLoop.main.add(poll, forMode: .common)
            hoverPoll = poll
            if hoverSuppressed { return }
            if !isOpen { peek() }
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.hovering, !self.hoverSuppressed else { return }
                self.hoverOpened = true
                self.webView.evaluateJavaScript("window.tokenTrackerInspect && window.tokenTrackerInspect()", completionHandler: nil)
                self.updateOpenState()
            }
            hoverWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.dwell, execute: work)
        } else {
            let work = DispatchWorkItem { [weak self] in
                guard let self, !self.hovering else { return }
                if self.pointerOnPanel() { self.hoverChanged(true); return }
                self.hoverSuppressed = false
                self.hoverOpened = false
                if !self.keepOpen {
                    self.webView.evaluateJavaScript("window.tokenTrackerCloseInspection && window.tokenTrackerCloseInspection()",
                                                    completionHandler: nil)
                }
                self.updateOpenState()
            }
            hoverWork = work
            // Peeking only: shrink back at once. Open: a grace period for a brief slip off the edge.
            DispatchQueue.main.asyncAfter(deadline: .now() + (isOpen ? 0.35 : 0), execute: work)
        }
    }

    /// The panel's frame grown by a couple of points, since the pointer at the very top of the
    /// screen sits on the frame's top edge, which NSRect.contains leaves out.
    func pointerOnPanel() -> Bool {
        panel.isVisible && panel.frame.insetBy(dx: -2, dy: -2).contains(NSEvent.mouseLocation)
    }

    static let dwell: TimeInterval = 0.45
    var hoverOpened = false

    /// The closed notch (or its current activity) grown a little, content still hidden.
    func peek() {
        guard let screen = notchScreen() else { return }
        let notch = hardwareNotchSize(screen)
        let base = hudKind.map { wingSize(notch, $0) } ?? (media.isPlaying ? wingSize(notch, .media) : notch)
        place(size: CGSize(width: base.width + 24, height: base.height + 8), animate: true, spring: true)
    }

    // MARK: Open / collapsed

    func updateOpenState() {
        guard !hiddenByUser else { return }
        (hoverOpened || shortcutOpened || keepOpen || (webWantsOpen && autoOpen)) ? open() : collapse(animate: true)
    }

    var headerHeight: CGFloat { notchScreen().map { hardwareNotchSize($0).height } ?? 32 }
    var notchWidthForHeader: CGFloat { notchScreen().map { hardwareNotchSize($0).width } ?? 180 }

    /// Size of the open notch for the selected tab, header row included.
    var openSize: CGSize {
        switch tabs.selected {
        case .tracker: return CGSize(width: requestedSize.width, height: requestedSize.height + headerHeight)
        case .media: return CGSize(width: max(500, notchWidthForHeader + 240), height: 150 + headerHeight)
        }
    }

    func open() {
        isOpen = true
        // WebKit only tracks hover in the key window; the panel is non-activating, so this
        // doesn't pull focus from the frontmost app but lets chart tooltips work before a click.
        panel.makeKey()
        hideHUD()
        updateVisualizer(showing: false)
        container.headerHeight = headerHeight
        syncContentSize()
        place(size: openSize, animate: true, spring: true)
        media.setVisible(tabs.selected == .media)
        // Fade the content in once the shape has mostly grown, so it doesn't stretch into view.
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = reducedMotion ? 0 : 0.28
            ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.4, 0, 0.2, 1)
            container.content?.animator().alphaValue = 1
            container.header?.animator().alphaValue = 1
        }
    }

    /// Lay the content out at the open size up front (see NotchContainerView.contentSize).
    func syncContentSize() {
        container.headerHeight = headerHeight
        let size = openSize
        container.contentSize = CGSize(width: size.width - 16, height: max(0, size.height - headerHeight))
    }

    /// Shrink to the camera housing; the black panel then disappears into the notch.
    /// While music plays the closed notch keeps a small activity in its wings.
    func collapse(animate: Bool) {
        guard let screen = notchScreen() else { return }
        isOpen = false
        container.content?.alphaValue = 0
        container.header?.alphaValue = 0
        media.setVisible(false)
        // Media updates and page calls land here mid-hover; shrinking the peek out from under the
        // pointer would send "exited" and cancel the pending open, so keep peeking instead.
        if hovering, !hoverSuppressed, hudKind == nil { peek(); return }
        if hudKind == nil, media.isPlaying, !lockScreen.isLocked {
            showIndicator(.media, animate: animate)
            return
        }
        if hudKind == nil { container.indicator = nil; updateVisualizer(showing: false) }
        let notch = hardwareNotchSize(screen)
        place(size: hudKind.map { wingSize(notch, $0) } ?? notch, animate: animate)
    }

    // MARK: Tabs, activities

    func tabChanged() {
        container.content = tabs.selected == .tracker ? webView : mediaHost
        container.content?.alphaValue = isOpen ? 1 : 0
        if isOpen { open() }
    }

    /// Closed notch plus a wing either side, just wide enough for the activity (as in Atoll:
    /// an artwork-sized square for music and battery, room for a level bar for volume and brightness).
    func wingSize(_ notch: CGSize, _ kind: HUDKind) -> CGSize {
        let wing: CGFloat
        switch kind {
        case .volume, .brightness: wing = 84
        case .output: wing = 120
        case .capsLock: wing = notch.height + 18
        case .media, .battery, .microphone, .camera: wing = notch.height + 10
        }
        // +16 for the container's 8pt side insets.
        return CGSize(width: notch.width + 2 * wing + 16, height: notch.height)
    }

    func showIndicator(_ kind: HUDKind, animate: Bool) {
        guard let screen = notchScreen() else { return }
        let notch = hardwareNotchSize(screen)
        container.indicator = NSHostingView(rootView: HUDView(kind: kind, notchWidth: notch.width, media: media, levels: levels))
        updateVisualizer(showing: kind == .media)
        place(size: wingSize(notch, kind), animate: animate)
    }

    /// Volume, brightness and battery: show for a moment beside the closed notch.
    func showHUD(_ kind: HUDKind) {
        guard !isOpen, !lockScreen.isLocked else { return }
        hudKind = kind
        showIndicator(kind, animate: true)
        hudWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.hideHUD(); if self?.isOpen == false { self?.collapse(animate: true) } }
        hudWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6, execute: work)
    }

    /// Run the audio tap only while the music bars are visible and something plays.
    func updateVisualizer(showing: Bool) {
        if showing, media.isPlaying, let player = media.player { levels.start(bundleID: player.bundleID) }
        else { levels.stop() }
    }

    func hideHUD() {
        hudWork?.cancel()
        hudKind = nil
        container.indicator = nil
    }

    func mediaChanged() {
        if !isOpen && hudKind == nil { collapse(animate: true) }
    }

    // MARK: Placement

    /// Pin the panel's top edge to the top of the notched screen, centred on the notch.
    /// `spring`: a soft overshoot for growing (peek, open); otherwise a quick ease for shrinking.
    func place(size: CGSize, animate: Bool, spring: Bool = false) {
        guard let screen = notchScreen() else { return }
        let frame = screen.frame
        let width = min(size.width, frame.width), height = min(size.height, frame.height)
        let rect = NSRect(x: frame.midX - width / 2, y: frame.maxY - height, width: width, height: height)
        // Re-animating to the same frame keeps WebKit resizing and it never paints.
        guard rect != placedFrame else { return }
        placedFrame = rect
        if animate && !reducedMotion {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = spring ? 0.42 : 0.26
                ctx.timingFunction = spring ? CAMediaTimingFunction(controlPoints: 0.2, 1.25, 0.35, 1)
                                            : CAMediaTimingFunction(controlPoints: 0.4, 0, 0.2, 1)
                panel.animator().setFrame(rect, display: true)
            }
        } else {
            panel.setFrame(rect, display: true)
        }
    }

    // MARK: window.pywebview.api bridge

    static let bridgeScript = """
    (() => {
      const call = (name) => (...args) => window.webkit.messageHandlers.bg.postMessage({name, args});
      const names = ['set_notch','get_widget_lock','set_widget_lock','get_keep_open','set_keep_open',
        'get_monitors','select_monitor','set_monitor_name','recenter','begin_drag','end_drag',
        'open_reset_actions','resize','resize_start','fit','minimize','toggle_maximize','close','size'];
      const api = {}; for (const n of names) api[n] = call(n);
      window.pywebview = {api};
      const ready = () => window.dispatchEvent(new Event('pywebviewready'));
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();
    })();
    """

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard let body = message.body as? [String: Any], let name = body["name"] as? String else {
            return replyHandler(nil, nil)
        }
        let args = body["args"] as? [Any] ?? []
        replyHandler(handle(name, args), nil)
    }

    func lockState() -> [String: Any] {
        ["locked": widgetLocked, "expanded": expanded, "revision": lockRevision,
         "keepOpen": keepOpen, "keepOpenRevision": keepOpenRevision]
    }

    func handle(_ name: String, _ args: [Any]) -> Any? {
        switch name {
        case "set_notch":
            guard let isExpanded = args.first as? Bool else { return nil }
            let height = args.count > 1 ? args[1] as? Double : nil
            reducedMotion = (args.count > 2 ? args[2] as? Bool : nil) ?? false
            let mode = args.count > 3 ? args[3] as? String : nil
            expanded = isExpanded
            requestedSize = notchSize(expanded: isExpanded, height: height, mode: mode)
            // Compact idle/rest/controls are resting states; keep those hidden in the notch.
            webWantsOpen = mode.map { $0 == "inspect" || $0 == "dashboard" } ?? isExpanded
            updateOpenState()
            return nil
        case "get_widget_lock":
            return lockState()
        case "set_widget_lock":
            // Same contract as WindowApi: reject bad input or a stale revision by returning current state.
            guard let locked = args.first as? Bool else { return lockState() }
            if args.count > 3, let expected = args[3] as? Int, expected != lockRevision { return lockState() }
            if locked != widgetLocked {
                widgetLocked = locked; lockRevision += 1
                UserDefaults.standard.set(locked, forKey: "widgetLocked")
            }
            return lockState()
        case "get_keep_open":
            return ["keepOpen": keepOpen, "keepOpenRevision": keepOpenRevision]
        case "set_keep_open":
            if args.count > 1, let expected = args[1] as? Int, expected != keepOpenRevision {
                return ["keepOpen": keepOpen, "keepOpenRevision": keepOpenRevision]
            }
            if let value = args.first as? Bool, value != keepOpen {
                keepOpen = value; keepOpenRevision += 1; updateOpenState()
            }
            return ["keepOpen": keepOpen, "keepOpenRevision": keepOpenRevision]
        case "get_monitors", "set_monitor_name":
            return monitors()
        case "select_monitor":
            if let id = args.first as? String, let number = UInt32(id),
               NSScreen.screens.contains(where: { displayID($0) == number }) {
                UserDefaults.standard.set(Int(number), forKey: "displayID")
                placedFrame = .zero
                isOpen ? { syncContentSize(); place(size: openSize, animate: false) }() : collapse(animate: false)
            }
            return monitors()
        case "recenter":
            placedFrame = .zero
            isOpen ? { syncContentSize(); place(size: openSize, animate: false) }() : collapse(animate: false)
            return monitors()
        case "minimize":
            shortcutOpened = false; hoverOpened = false; keepOpen = false; keepOpenRevision += 1
            hoverWork?.cancel(); hoverSuppressed = hovering
            webView.evaluateJavaScript("window.tokenTrackerCloseInspection && window.tokenTrackerCloseInspection()", completionHandler: nil)
            collapse(animate: true)
            return nil
        case "size", "resize_start":
            return ["width": panel.frame.width, "height": panel.frame.height]
        case "fit", "toggle_maximize":
            // The notch has one open size; Fit and Maximize open the full tree as the shortcut does.
            if !shortcutOpened { toggleInspection() } else if name == "toggle_maximize" { toggleInspection() }
            return nil
        case "resize":
            return nil  // The notch panel is sized by its content, not by edge drags.
        case "end_drag":
            return false
        case "close":
            // As on Windows, Close hides the widget and monitoring carries on (Show/Hide brings it back).
            setHidden(true); return nil
        case "open_reset_actions":
            openSettings(); return true
        case "begin_drag":
            return ["accepted": false, "generation": NSNull()]
        default:
            return false
        }
    }

    func notchSize(expanded: Bool, height: Double?, mode: String?) -> CGSize {
        if let mode, let base = compactSizes[mode] {
            guard mode == "inspect" else { return base }
            let h = min(compactInspectMaxHeight, max(base.height, CGFloat(height ?? Double(base.height))))
            return CGSize(width: base.width, height: h)
        }
        guard expanded else { return CGSize(width: notchWidth, height: notchCollapsedHeight) }
        return CGSize(width: notchWidth, height: max(notchCollapsedHeight, CGFloat(height ?? 560)))
    }

    // MARK: Status item

    /// Displays in the shape the widget's Monitor button expects.
    func monitors() -> [[String: Any]] {
        let selected = notchScreen().map(displayID)
        return NSScreen.screens.enumerated().map { index, screen in
            let id = displayID(screen)
            let builtIn = CGDisplayIsBuiltin(id) != 0
            let area = screen.visibleFrame
            return ["id": String(id), "stableId": String(id), "kind": builtIn ? "laptop" : "monitor",
                    "label": screen.localizedName, "index": index, "primary": screen == NSScreen.screens.first,
                    "selected": id == selected, "scale": screen.backingScaleFactor,
                    "workArea": ["x": area.minX, "y": area.minY, "width": area.width, "height": area.height]]
        }
    }

    func setHidden(_ hidden: Bool) {
        hiddenByUser = hidden
        if hidden {
            shortcutOpened = false; hoverOpened = false
            panel.orderOut(nil)
            levels.stop()
        } else {
            panel.orderFrontRegardless()
            collapse(animate: false)
        }
        showHideItem?.title = hidden ? "Show Branchglance" : "Hide Branchglance"
    }

    // MARK: Inspection shortcut

    enum Shortcut: String, CaseIterable {
        case controlShiftT = "⌃⇧T", controlOptionT = "⌃⌥T", off = "Off"
        var modifiers: UInt32? {
            switch self {
            case .controlShiftT: return UInt32(controlKey | shiftKey)
            case .controlOptionT: return UInt32(controlKey | optionKey)
            case .off: return nil
            }
        }
    }

    var shortcut: Shortcut {
        get { Shortcut(rawValue: UserDefaults.standard.string(forKey: "shortcut") ?? "") ?? .controlShiftT }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: "shortcut"); registerShortcut() }
    }

    /// A Carbon hot key: works while other apps are in front and needs no Accessibility permission.
    func registerShortcut() {
        if let hotKey { UnregisterEventHotKey(hotKey); self.hotKey = nil }
        guard let modifiers = shortcut.modifiers else { return }
        if !shortcutHandlerInstalled {
            shortcutHandlerInstalled = true
            var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            InstallEventHandler(GetApplicationEventTarget(), { _, _, context in
                guard let context else { return noErr }
                let host = Unmanaged<Host>.fromOpaque(context).takeUnretainedValue()
                DispatchQueue.main.async { host.toggleInspection() }
                return noErr
            }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
        }
        RegisterEventHotKey(UInt32(kVK_ANSI_T), modifiers, EventHotKeyID(signature: 0x4247_4C4E, id: 1),
                            GetApplicationEventTarget(), 0, &hotKey)
    }
    var shortcutHandlerInstalled = false

    @objc func toggleInspection() {
        if hiddenByUser { setHidden(false) }
        shortcutOpened.toggle()
        let script = shortcutOpened ? "window.tokenTrackerInspect && window.tokenTrackerInspect()"
                                    : "window.tokenTrackerCloseInspection && window.tokenTrackerCloseInspection()"
        if shortcutOpened, tabs.selected != .tracker { tabs.selected = .tracker }
        webView.evaluateJavaScript(script, completionHandler: nil)
        updateOpenState()
    }

    // MARK: Web view policy

    /// Only the local tracker page loads in the notch; any other link opens in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if isLocalTracker(url) || url.absoluteString == "about:blank" { return decisionHandler(.allow) }
        if action.navigationType == .linkActivated || action.targetFrame == nil,
           ["http", "https", "codex", "mailto"].contains(url.scheme?.lowercased() ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    /// target=_blank and window.open: hand http(s) links to the browser, never open a web view.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
           !isLocalTracker(url) {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func isLocalTracker(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1" && url.port == serverPort
    }

    func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted",
                                     accessibilityDescription: "Branchglance")
        let menu = NSMenu()
        let showHide = NSMenuItem(title: "Hide Branchglance", action: #selector(toggleHidden), keyEquivalent: "")
        showHide.target = self
        menu.addItem(showHide)
        showHideItem = showHide
        let shortcutItem = NSMenuItem(title: "Inspection Shortcut", action: nil, keyEquivalent: "")
        let shortcutMenu = NSMenu()
        for option in Shortcut.allCases {
            let entry = NSMenuItem(title: option.rawValue, action: #selector(chooseShortcut(_:)), keyEquivalent: "")
            entry.target = self
            entry.representedObject = option.rawValue
            entry.state = option == shortcut ? .on : .off
            shortcutMenu.addItem(entry)
        }
        shortcutItem.submenu = shortcutMenu
        menu.addItem(shortcutItem)
        menu.addItem(withTitle: "Refresh", action: #selector(refresh), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",").target = self
        let login = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin(_:)), keyEquivalent: "")
        login.target = self
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        let auto = NSMenuItem(title: "Open on New Activity", action: #selector(toggleAutoOpen(_:)), keyEquivalent: "")
        auto.target = self
        auto.state = autoOpen ? .on : .off
        menu.addItem(auto)
        let sounds = NSMenuItem(title: "Lock & Unlock Sounds", action: #selector(toggleLockSounds(_:)), keyEquivalent: "")
        sounds.target = self
        sounds.state = lockScreen.soundsEnabled ? .on : .off
        menu.addItem(sounds)
        let widget = NSMenuItem(title: "Media on Lock Screen", action: #selector(toggleLockWidget(_:)), keyEquivalent: "")
        widget.target = self
        widget.state = lockScreen.widgetEnabled ? .on : .off
        menu.addItem(widget)
        let artwork = NSMenuItem(title: "Download Spotify Artwork", action: #selector(toggleArtwork(_:)), keyEquivalent: "")
        artwork.target = self
        artwork.state = UserDefaults.standard.bool(forKey: "downloadSpotifyArtwork") ? .on : .off
        artwork.toolTip = "Spotify only provides artwork as a web link; fetching it contacts Spotify's image server."
        menu.addItem(artwork)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        menu.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Branchglance", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
    }

    var showHideItem: NSMenuItem?

    @objc func toggleHidden() { setHidden(!hiddenByUser) }

    @objc func chooseShortcut(_ item: NSMenuItem) {
        guard let raw = item.representedObject as? String, let option = Shortcut(rawValue: raw) else { return }
        shortcut = option
        item.menu?.items.forEach { $0.state = ($0.representedObject as? String) == raw ? .on : .off }
    }

    @objc func refresh() {
        webView.evaluateJavaScript("window.tokenTrackerRefresh && window.tokenTrackerRefresh(true)", completionHandler: nil)
    }

    @objc func toggleArtwork(_ item: NSMenuItem) {
        let on = !UserDefaults.standard.bool(forKey: "downloadSpotifyArtwork")
        UserDefaults.standard.set(on, forKey: "downloadSpotifyArtwork")
        item.state = on ? .on : .off
        media.refresh()
    }

    /// Reset action settings, including the Claude Code collection switch.
    @objc func openSettings() {
        if settingsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 540),
                                  styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.title = "Branchglance Settings"
            window.isReleasedWhenClosed = false
            window.backgroundColor = NSColor(red: 0x11/255, green: 0x13/255, blue: 0x16/255, alpha: 1)
            let view = WKWebView(frame: .zero)
            view.navigationDelegate = self
            view.uiDelegate = self
            window.contentView = view
            view.load(URLRequest(url: URL(string: "http://127.0.0.1:\(serverPort)/reset_actions.html#t=\(settingsToken)")!))
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    @objc func toggleLogin(_ item: NSMenuItem) {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch { NSLog("Branchglance: login item change failed: \(error)") }
        item.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }

    @objc func toggleAutoOpen(_ item: NSMenuItem) {
        autoOpen.toggle()
        item.state = autoOpen ? .on : .off
        if !autoOpen { updateOpenState() }
    }

    @objc func toggleLockSounds(_ item: NSMenuItem) {
        lockScreen.soundsEnabled.toggle()
        item.state = lockScreen.soundsEnabled ? .on : .off
    }

    @objc func toggleLockWidget(_ item: NSMenuItem) {
        lockScreen.widgetEnabled.toggle()
        item.state = lockScreen.widgetEnabled ? .on : .off
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    @objc func reload() { webView.load(URLRequest(url: pageURL)) }
    @objc func openInBrowser() { NSWorkspace.shared.open(pageURL) }
}


// MARK: Tabs

enum NotchTab: CaseIterable {
    case tracker, media
    var symbol: String { self == .tracker ? "point.3.connected.trianglepath.dotted" : "music.note" }
    var label: String { self == .tracker ? "Branchglance" : "Media" }
}

final class TabState: ObservableObject {
    @Published var selected: NotchTab = .tracker { didSet { if oldValue != selected { onSelect?(selected) } } }
    var onSelect: ((NotchTab) -> Void)?
}

/// Tab buttons in the left wing of the header; the camera housing sits in the middle.
struct TabHeader: View {
    @ObservedObject var tabs: TabState
    let notchWidth: CGFloat

    var body: some View {
        HStack(spacing: 0) {
            HStack(spacing: 4) {
                ForEach(NotchTab.allCases, id: \.self) { tab in
                    Button { tabs.selected = tab } label: {
                        Image(systemName: tab.symbol)
                            .font(.system(size: 11, weight: .semibold))
                            .frame(width: 26, height: 20)
                            .background(Capsule().fill(Color.white.opacity(tabs.selected == tab ? 0.16 : 0)))
                    }
                    .buttonStyle(.plain)
                    .help(tab.label)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Color.clear.frame(width: notchWidth)
            Spacer().frame(maxWidth: .infinity)
        }
        .padding(.horizontal, 8)
        .foregroundStyle(.white)
    }
}

