// Branchglance macOS notch host.
// Hosts the existing runway web UI in a borderless panel pinned to the top-centre
// of the notched display, and answers the window.pywebview.api calls that
// token_tracker/widget.py answers on Windows.
import Cocoa

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
/// Layout: a header row level with the camera housing (tab buttons sit in its wings),
/// the selected tab's content below it, and an indicator layer that covers both.
final class NotchContainerView: NSView {
    let mask = CAShapeLayer()
    var onHover: ((Bool) -> Void)?
    var headerHeight: CGFloat = 32 { didSet { needsLayout = true } }
    var header: NSView? { didSet { swap(oldValue, header) } }
    var content: NSView? { didSet { swap(oldValue, content) } }
    var indicator: NSView? { didSet { swap(oldValue, indicator) } }
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
        let inner = bounds.insetBy(dx: 8, dy: 0)
        header?.frame = NSRect(x: inner.minX, y: inner.maxY - headerHeight, width: inner.width, height: headerHeight)
        content?.frame = NSRect(x: inner.minX, y: inner.minY, width: inner.width,
                                height: max(0, inner.height - headerHeight))
        indicator?.frame = inner
    }

    private func swap(_ old: NSView?, _ new: NSView?) {
        guard old !== new else { return }
        old?.removeFromSuperview()
        guard let new else { needsLayout = true; return }
        if new === indicator || indicator?.superview !== self { addSubview(new) }
        else { addSubview(new, positioned: .below, relativeTo: indicator) }
        needsLayout = true
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

