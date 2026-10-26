// Branchglance macOS notch host.
// Hosts the existing runway web UI in a borderless panel pinned to the top-centre
// of the notched display, and answers the window.pywebview.api calls that
// token_tracker/widget.py answers on Windows.
import Cocoa
import WebKit

let serverPort = 8765
let pageURL = URL(string: "http://127.0.0.1:\(serverPort)/runway.html")!

// Mirrors COMPACT_NOTCH_SIZES / NOTCH_* in token_tracker/widget.py.
let compactSizes: [String: CGSize] = [
    "idle": CGSize(width: 420, height: 100), "rest": CGSize(width: 420, height: 100),
    "controls": CGSize(width: 420, height: 110), "inspect": CGSize(width: 560, height: 114),
    "dashboard": CGSize(width: 720, height: 540),
]
let compactInspectMaxHeight: CGFloat = 420
let notchWidth: CGFloat = 680, notchCollapsedHeight: CGFloat = 148

final class NotchPanel: NSPanel {
    init() {
        super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        hidesOnDeactivate = false
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        isMovable = false
        isReleasedWhenClosed = false
        // Above the menu bar so the panel can sit over the notch, on every Space.
        level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3)
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    }
    override var canBecomeKey: Bool { true }
}

/// Notch outline: the top corners curve outward into the menu bar, the bottom corners round off.
func notchPath(in rect: CGRect, top: CGFloat = 8, bottom: CGFloat = 18) -> CGPath {
    // AppKit coordinates: rect.maxY is the top edge touching the screen bezel.
    let path = CGMutablePath()
    let b = min(bottom, (rect.width - 2 * top) / 2, rect.height / 2)
    path.move(to: CGPoint(x: rect.minX, y: rect.maxY))
    path.addQuadCurve(to: CGPoint(x: rect.minX + top, y: rect.maxY - top),
                      control: CGPoint(x: rect.minX + top, y: rect.maxY))
    path.addLine(to: CGPoint(x: rect.minX + top, y: rect.minY + b))
    path.addQuadCurve(to: CGPoint(x: rect.minX + top + b, y: rect.minY),
                      control: CGPoint(x: rect.minX + top, y: rect.minY))
    path.addLine(to: CGPoint(x: rect.maxX - top - b, y: rect.minY))
    path.addQuadCurve(to: CGPoint(x: rect.maxX - top, y: rect.minY + b),
                      control: CGPoint(x: rect.maxX - top, y: rect.minY))
    path.addLine(to: CGPoint(x: rect.maxX - top, y: rect.maxY - top))
    path.addQuadCurve(to: CGPoint(x: rect.maxX, y: rect.maxY),
                      control: CGPoint(x: rect.maxX - top, y: rect.maxY))
    path.closeSubpath()
    return path
}

/// Clips its content to the notch outline and reports hover over the whole panel.
final class NotchContainerView: NSView {
    let mask = CAShapeLayer()
    var onHover: ((Bool) -> Void)?
    private var tracking: NSTrackingArea?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        layer?.mask = mask
    }
    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        mask.frame = bounds
        mask.path = notchPath(in: bounds)
        CATransaction.commit()
        subviews.forEach { $0.frame = bounds.insetBy(dx: 8, dy: 0) }
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                  owner: self, userInfo: nil)
        addTrackingArea(area)
        tracking = area
    }
    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }
}

/// The display with a camera housing, else the main display.
func notchScreen() -> NSScreen? {
    NSScreen.screens.first { $0.safeAreaInsets.top > 0 } ?? NSScreen.main
}

/// Size of the camera housing, so the collapsed panel hides inside it. Screens without
/// one get a slim menu-bar-height pill as the hover target.
func hardwareNotchSize(_ screen: NSScreen) -> CGSize {
    let top = screen.safeAreaInsets.top
    if top > 0, let left = screen.auxiliaryTopLeftArea, let right = screen.auxiliaryTopRightArea {
        return CGSize(width: screen.frame.width - left.width - right.width, height: top)
    }
    return CGSize(width: 180, height: max(24, screen.frame.maxY - screen.visibleFrame.maxY))
}

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
        let container = NotchContainerView(frame: .zero)
        container.addSubview(webView)
        container.onHover = { [weak self] inside in self?.hoverChanged(inside) }
        panel.contentView = container
        panel.acceptsMouseMovedEvents = true

        webView.alphaValue = 0
        collapse(animate: false)
        panel.orderFrontRegardless()
        loadWhenReady(attempt: 0)

        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.isOpen ? self.place(size: self.requestedSize, animate: false) : self.collapse(animate: false)
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
        // Repo root: BRANCHGLANCE_ROOT, else three levels up from macos/build/BranchglanceNotch.
        let root = ProcessInfo.processInfo.environment["BRANCHGLANCE_ROOT"].map { URL(fileURLWithPath: $0) }
            ?? URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
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

    func open() {
        isOpen = true
        place(size: requestedSize, animate: true)
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = reducedMotion ? 0 : 0.18
            webView.animator().alphaValue = 1
        }
    }

    /// Shrink to the camera housing; the black panel then disappears into the notch.
    func collapse(animate: Bool) {
        guard let screen = notchScreen() else { return }
        isOpen = false
        webView.alphaValue = 0
        place(size: hardwareNotchSize(screen), animate: animate)
    }

    // MARK: Placement

    /// Pin the panel's top edge to the top of the notched screen, centred on the notch.
    func place(size: CGSize, animate: Bool) {
        guard let screen = notchScreen() else { return }
        let frame = screen.frame
        let width = min(size.width, frame.width), height = min(size.height, frame.height)
        let rect = NSRect(x: frame.midX - width / 2, y: frame.maxY - height, width: width, height: height)
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

    @objc func reload() { webView.load(URLRequest(url: pageURL)) }
    @objc func openInBrowser() { NSWorkspace.shared.open(pageURL) }
}

let app = NSApplication.shared
let host = Host()
app.delegate = host
app.run()
