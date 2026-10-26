// `Branchglance --render-preview <dir>` draws the media views with sample data to PNGs and exits.
// Used to check layout without opening the notch.
import AppKit
import SwiftUI

@MainActor
func renderPreviews(to directory: String) {
    let media = NowPlaying()
    media.player = .spotify
    media.title = "A Fairly Long Song Title That Needs To Scroll"
    media.artist = "Sample Artist"
    media.duration = 214
    media.position = 83
    media.isPlaying = true
    let art = NSImage(size: NSSize(width: 300, height: 300), flipped: false) { rect in
        NSGradient(colors: [.systemPink, .systemOrange, .systemPurple])?.draw(in: rect, angle: 45)
        return true
    }
    media.artwork = art

    func save<V: View>(_ view: V, _ name: String, _ size: CGSize) {
        let renderer = ImageRenderer(content: view.frame(width: size.width, height: size.height).background(Color.black)
            .environment(\.colorScheme, .dark))
        renderer.scale = 2
        guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
              let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: URL(fileURLWithPath: directory).appendingPathComponent(name))
    }
    save(MediaView(media: media), "media.png", CGSize(width: 484, height: 150))
    save(HUDView(kind: .media, notchWidth: 200, media: media), "activity-media.png", CGSize(width: 200 + 2 * 48, height: 38))
    save(HUDView(kind: .volume(level: 0.6, muted: false), notchWidth: 200, media: media), "activity-volume.png", CGSize(width: 200 + 2 * 84, height: 38))
    save(HUDView(kind: .capsLock(on: true), notchWidth: 200, media: media), "activity-caps.png", CGSize(width: 200 + 2 * 56, height: 38))
    save(HUDView(kind: .output(name: "AirPods Pro", symbol: "airpodspro"), notchWidth: 200, media: media), "activity-output.png", CGSize(width: 200 + 2 * 120, height: 38))
    save(HUDView(kind: .microphone, notchWidth: 200, media: media), "activity-mic.png", CGSize(width: 200 + 2 * 48, height: 38))
    save(LockScreenMediaView(media: media), "lockscreen.png", CGSize(width: 400, height: 140))
}

/// `Branchglance --probe-audio <bundle id>` prints the visualizer bands for four seconds.
func probeAudio(bundleID: String) {
    let levels = AudioLevels()
    levels.start(bundleID: bundleID)
    for _ in 0..<8 {
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        print(levels.live ? "live" : "no audio yet", levels.bands.map { String(format: "%.2f", $0) }.joined(separator: " "))
    }
    levels.stop()
}
