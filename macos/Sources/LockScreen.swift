// Lock and unlock sounds, and a now-playing widget on the lock screen.
//
// macOS draws the lock screen above every normal window level. Putting a window there
// needs a SkyLight space with a raised absolute level; this is the same private-API
// approach other notch apps use. If any symbol is missing the widget is skipped and the
// sounds still play.
import AppKit
import SwiftUI

private enum SkyLight {
    typealias MainConnection = @convention(c) () -> Int32
    typealias SpaceCreate = @convention(c) (Int32, Int32, Int32) -> Int32
    typealias SpaceSetLevel = @convention(c) (Int32, Int32, Int32) -> Int32
    typealias ShowSpaces = @convention(c) (Int32, CFArray) -> Int32
    typealias AddWindows = @convention(c) (Int32, Int32, CFArray, Int32) -> Int32

    static let handle = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY)
    static func load<T>(_ name: String, as type: T.Type) -> T? {
        guard let handle, let symbol = dlsym(handle, name) else { return nil }
        return unsafeBitCast(symbol, to: type)
    }

    /// A space that stays above the lock screen; nil when SkyLight is unavailable.
    static let lockSpace: (connection: Int32, space: Int32, add: AddWindows)? = {
        guard let main = load("SLSMainConnectionID", as: MainConnection.self),
              let create = load("SLSSpaceCreate", as: SpaceCreate.self),
              let setLevel = load("SLSSpaceSetAbsoluteLevel", as: SpaceSetLevel.self),
              let show = load("SLSShowSpaces", as: ShowSpaces.self),
              let add = load("SLSSpaceAddWindowsAndRemoveFromSpaces", as: AddWindows.self) else { return nil }
        let connection = main()
        let space = create(connection, 1, 0)
        _ = setLevel(connection, space, 100)
        _ = show(connection, [space] as CFArray)
        return (connection, space, add)
    }()

    static func moveToLockScreen(_ window: NSWindow) -> Bool {
        guard let lockSpace else { return false }
        return lockSpace.add(lockSpace.connection, lockSpace.space, [window.windowNumber] as CFArray, 7) == 0
    }
}

final class LockScreenController {
    private let media: NowPlaying
    private var widget: NSPanel?
    private(set) var isLocked = false
    var soundsEnabled = UserDefaults.standard.object(forKey: "lockSounds") as? Bool ?? true {
        didSet { UserDefaults.standard.set(soundsEnabled, forKey: "lockSounds") }
    }
    var widgetEnabled = UserDefaults.standard.object(forKey: "lockWidget") as? Bool ?? true {
        didSet { UserDefaults.standard.set(widgetEnabled, forKey: "lockWidget"); if !widgetEnabled { hideWidget() } }
    }
    var onLockChange: ((Bool) -> Void)?

    init(media: NowPlaying) {
        self.media = media
        let center = DistributedNotificationCenter.default()
        center.addObserver(forName: .init("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            self?.locked(true)
        }
        center.addObserver(forName: .init("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in
            self?.locked(false)
        }
    }

    private func locked(_ locked: Bool) {
        guard locked != isLocked else { return }
        isLocked = locked
        if soundsEnabled { NSSound(named: locked ? "Bottle" : "Pop")?.play() }
        if locked, widgetEnabled, media.hasTrack { showWidget() } else { hideWidget() }
        onLockChange?(locked)
    }

    private func showWidget() {
        guard let screen = NSScreen.main else { return }
        let size = CGSize(width: 380, height: 120)
        let panel = widget ?? {
            let panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = false
            panel.level = .screenSaver
            panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
            let host = NSHostingView(rootView: LockScreenMediaView(media: media))
            panel.contentView = host
            widget = panel
            return panel
        }()
        // Below the clock, above the password field.
        let frame = screen.frame
        panel.setFrame(NSRect(x: frame.midX - size.width / 2, y: frame.minY + frame.height * 0.22,
                              width: size.width, height: size.height), display: true)
        panel.orderFrontRegardless()
        if !SkyLight.moveToLockScreen(panel) { NSLog("Branchglance: lock screen space unavailable") }
        media.setVisible(true)
    }

    private func hideWidget() {
        widget?.orderOut(nil)
    }
}

struct LockScreenMediaView: View {
    @ObservedObject var media: NowPlaying
    var body: some View {
        HStack(spacing: 14) {
            Artwork(image: media.artwork, size: 72)
            VStack(alignment: .leading, spacing: 6) {
                Text(media.title).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                Text(media.artist).font(.system(size: 12)).opacity(0.7).lineLimit(1)
                HStack(spacing: 24) {
                    ControlButton(symbol: "backward.fill", action: media.previous)
                    ControlButton(symbol: media.isPlaying ? "pause.fill" : "play.fill", size: 18, action: media.playPause)
                    ControlButton(symbol: "forward.fill", action: media.next)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(16)
        .foregroundStyle(.white)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .environment(\.colorScheme, .dark)
    }
}
