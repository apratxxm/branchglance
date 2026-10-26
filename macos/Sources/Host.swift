import Cocoa
import SwiftUI
import WebKit

final class Host: NSObject, NSApplicationDelegate, WKScriptMessageHandlerWithReply, WKNavigationDelegate {
    let panel = NotchPanel()
    var webView: WKWebView!
    var server: Process?
    var statusItem: NSStatusItem?
    var keepOpen = false, keepOpenRevision = 0
    var expanded = false, lockRevision = 0
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
    lazy var lockScreen = LockScreenController(media: media)
    let tabs = TabState()
    var container: NotchContainerView!
    var mediaHost: NSHostingView<MediaView>!
    var hudKind: HUDKind?
    var hudWork: DispatchWorkItem?
    var placedFrame = NSRect.zero

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
        lockScreen.onLockChange = { [weak self] locked in if locked { self?.hovering = false; self?.updateOpenState() } }
        panel.orderFrontRegardless()
        loadWhenReady(attempt: 0)

        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.isOpen ? self.place(size: self.openSize, animate: false) : self.collapse(animate: false)
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
        env["BRANCHGLANCE_SETTINGS_TOKEN"] = settingsToken
        env["BRANCHGLANCE_POLICY_FILE"] = env["BRANCHGLANCE_POLICY_FILE"] ?? NSHomeDirectory()
            + "/Library/Application Support/Branchglance/policy.json"
        process.environment = env
        do { try process.run(); server = process } catch { NSLog("Branchglance: server start failed: \(error)") }
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

    /// Open the inspection view after a short dwell, close it shortly after the pointer leaves,
    /// so passing the cursor across the menu bar doesn't flicker the notch.
    func hoverChanged(_ inside: Bool) {
        // Tracking areas are rebuilt as the panel resizes, which re-sends "entered".
        guard inside != hovering else { return }
        hoverWork?.cancel()
        hovering = inside
        if inside { open() }
        let script = inside ? "window.tokenTrackerInspect && window.tokenTrackerInspect()"
                            : "window.tokenTrackerCloseInspection && window.tokenTrackerCloseInspection()"
        let work = DispatchWorkItem { [weak self] in
            guard let self, !(self.keepOpen && !inside) else { return }
            self.webView.evaluateJavaScript(script, completionHandler: nil)
            self.updateOpenState()
        }
        hoverWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + (inside ? 0.12 : 0.35), execute: work)
    }

    // MARK: Open / collapsed

    func updateOpenState() {
        (hovering || keepOpen || webWantsOpen) ? open() : collapse(animate: true)
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
        hideHUD()
        container.headerHeight = headerHeight
        place(size: openSize, animate: true)
        media.setVisible(tabs.selected == .media)
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = reducedMotion ? 0 : 0.18
            container.content?.animator().alphaValue = 1
            container.header?.animator().alphaValue = 1
        }
    }

    /// Shrink to the camera housing; the black panel then disappears into the notch.
    /// While music plays the closed notch keeps a small activity in its wings.
    func collapse(animate: Bool) {
        guard let screen = notchScreen() else { return }
        isOpen = false
        container.content?.alphaValue = 0
        container.header?.alphaValue = 0
        media.setVisible(false)
        if hudKind == nil, media.isPlaying, !lockScreen.isLocked {
            showIndicator(.media, animate: animate)
            return
        }
        if hudKind == nil { container.indicator = nil }
        let notch = hardwareNotchSize(screen)
        place(size: hudKind == nil ? notch : wingSize(notch), animate: animate)
    }

    // MARK: Tabs, activities

    func tabChanged() {
        container.content = tabs.selected == .tracker ? webView : mediaHost
        container.content?.alphaValue = isOpen ? 1 : 0
        if isOpen { open() }
    }

    func wingSize(_ notch: CGSize) -> CGSize { CGSize(width: notch.width + 160, height: notch.height) }

    func showIndicator(_ kind: HUDKind, animate: Bool) {
        guard let screen = notchScreen() else { return }
        let notch = hardwareNotchSize(screen)
        container.indicator = NSHostingView(rootView: HUDView(kind: kind, notchWidth: notch.width, media: media))
        place(size: wingSize(notch), animate: animate)
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
    func place(size: CGSize, animate: Bool) {
        guard let screen = notchScreen() else { return }
        let frame = screen.frame
        let width = min(size.width, frame.width), height = min(size.height, frame.height)
        let rect = NSRect(x: frame.midX - width / 2, y: frame.maxY - height, width: width, height: height)
        // Re-animating to the same frame keeps WebKit resizing and it never paints.
        guard rect != placedFrame else { return }
        placedFrame = rect
        if animate && !reducedMotion {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.22
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
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
        ["locked": false, "expanded": expanded, "revision": lockRevision,
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
        case "get_widget_lock", "set_widget_lock":
            return lockState()
        case "get_keep_open":
            return ["keepOpen": keepOpen, "keepOpenRevision": keepOpenRevision]
        case "set_keep_open":
            if let value = args.first as? Bool, value != keepOpen {
                keepOpen = value; keepOpenRevision += 1; updateOpenState()
            }
            return ["keepOpen": keepOpen, "keepOpenRevision": keepOpenRevision]
        case "get_monitors", "select_monitor", "set_monitor_name":
            return []
        case "size":
            return ["width": panel.frame.width, "height": panel.frame.height]
        case "close":
            NSApp.terminate(nil); return nil
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

    func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted",
                                     accessibilityDescription: "Branchglance")
        let menu = NSMenu()
        menu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",").target = self
        let sounds = NSMenuItem(title: "Lock & Unlock Sounds", action: #selector(toggleLockSounds(_:)), keyEquivalent: "")
        sounds.target = self
        sounds.state = lockScreen.soundsEnabled ? .on : .off
        menu.addItem(sounds)
        let widget = NSMenuItem(title: "Media on Lock Screen", action: #selector(toggleLockWidget(_:)), keyEquivalent: "")
        widget.target = self
        widget.state = lockScreen.widgetEnabled ? .on : .off
        menu.addItem(widget)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        menu.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Branchglance", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
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
            window.contentView = view
            view.load(URLRequest(url: URL(string: "http://127.0.0.1:\(serverPort)/reset_actions.html#t=\(settingsToken)")!))
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
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

