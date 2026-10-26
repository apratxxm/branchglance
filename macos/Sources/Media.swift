// Now playing for Spotify and Apple Music.
// Track changes arrive through each player's distributed notification, so nothing polls
// while music is idle; the playback position is read once a second only while the media
// tab is on screen.
import AppKit
import CoreImage
import SwiftUI

enum Player: String, CaseIterable {
    case spotify = "Spotify", music = "Music"

    var bundleID: String { self == .spotify ? "com.spotify.client" : "com.apple.Music" }
    var notification: String { self == .spotify ? "com.spotify.client.PlaybackStateChanged" : "com.apple.Music.playerInfo" }
    var isRunning: Bool { !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty }
}

final class NowPlaying: ObservableObject {
    @Published var player: Player?
    @Published var title = ""
    @Published var artist = ""
    @Published var album = ""
    @Published var isPlaying = false
    @Published var duration: Double = 0
    @Published var position: Double = 0
    @Published var artwork: NSImage? { didSet { accent = artwork.flatMap(averageColor) ?? .white } }
    /// Average artwork colour, used for the glow, artist line and closed-notch bars.
    @Published var accent: NSColor = .white
    @Published var shuffling = false
    /// Progress bar position while the user drags it.
    @Published var dragFraction: Double?
    @Published var repeating = false
    /// When `position` was last read, so the progress bar can advance smoothly without polling.
    var positionDate = Date()

    func estimatedPosition(at date: Date = Date()) -> Double {
        guard isPlaying else { return position }
        return min(duration, position + date.timeIntervalSince(positionDate))
    }

    /// Called on play/pause and track changes, for the closed-notch activity.
    var onChange: (() -> Void)?
    private var artworkKey = ""
    private let scriptQueue = DispatchQueue(label: "branchglance.media.applescript")

    var hasTrack: Bool { player != nil && !title.isEmpty }

    init() {
        for player in Player.allCases {
            DistributedNotificationCenter.default().addObserver(
                forName: Notification.Name(player.notification), object: nil, queue: .main
            ) { [weak self] note in self?.apply(note.userInfo ?? [:], from: player) }
        }
        refresh()
    }

    /// Read the current state directly, e.g. at launch or when the tab opens.
    func refresh() {
        for player in Player.allCases where player.isRunning {
            let script = """
            tell application "\(player.rawValue)"
              if player state is stopped then return ""
              set t to current track
              return (name of t) & "\\n" & (artist of t) & "\\n" & (album of t) & "\\n" & \
                ((player state is playing) as text) & "\\n" & (player position as text) & "\\n" & (duration of t as text)
            end tell
            """
            run(script) { [weak self] output in
                guard let self, let output, !output.isEmpty else { return }
                let parts = output.components(separatedBy: "\n")
                guard parts.count >= 6 else { return }
                // Spotify reports duration in milliseconds, Music in seconds.
                var duration = Double(parts[5].replacingOccurrences(of: ",", with: ".")) ?? 0
                if player == .spotify { duration /= 1000 }
                self.update(player: player, title: parts[0], artist: parts[1], album: parts[2],
                            playing: parts[3] == "true",
                            position: Double(parts[4].replacingOccurrences(of: ",", with: ".")) ?? 0,
                            duration: duration)
            }
        }
    }

    private func apply(_ info: [AnyHashable: Any], from player: Player) {
        let state = info["Player State"] as? String ?? ""
        if state == "Stopped" {
            if self.player == player { clear() }
            return
        }
        let total = (info["Total Time"] as? Double) ?? (info["Duration"] as? Double) ?? 0
        update(player: player,
               title: info["Name"] as? String ?? title,
               artist: info["Artist"] as? String ?? artist,
               album: info["Album"] as? String ?? album,
               playing: state == "Playing",
               position: info["Playback Position"] as? Double ?? position,
               // Both players put milliseconds in Total Time; Spotify's Duration is milliseconds too.
               duration: total > 0 ? total / 1000 : duration)
    }

    private func update(player: Player, title: String, artist: String, album: String,
                        playing: Bool, position: Double, duration: Double) {
        // A paused player never replaces one that is playing.
        if let current = self.player, current != player, isPlaying, !playing { return }
        let changed = self.player != player || self.title != title || isPlaying != playing
        self.player = player
        self.title = title
        self.artist = artist
        self.album = album
        self.isPlaying = playing
        self.position = position
        self.positionDate = Date()
        self.duration = duration
        loadArtwork()
        if changed { onChange?() }
    }

    private func clear() {
        player = nil; title = ""; artist = ""; album = ""; isPlaying = false
        artwork = nil; artworkKey = ""
        onChange?()
    }

    private func loadArtwork() {
        guard let player else { return }
        let key = player.rawValue + title + album
        guard key != artworkKey else { return }
        artworkKey = key
        artwork = nil
        if player == .spotify {
            run("tell application \"Spotify\" to return artwork url of current track") { [weak self] url in
                guard let url, let link = URL(string: url) else { return }
                URLSession.shared.dataTask(with: link) { data, _, _ in
                    guard let data, let image = NSImage(data: data) else { return }
                    DispatchQueue.main.async { if self?.artworkKey == key { self?.artwork = image } }
                }.resume()
            }
        } else {
            scriptQueue.async { [weak self] in
                let script = NSAppleScript(source: "tell application \"Music\" to return data of artwork 1 of current track")
                let data = script?.executeAndReturnError(nil).data
                DispatchQueue.main.async {
                    guard let self, self.artworkKey == key, let data, let image = NSImage(data: data) else { return }
                    self.artwork = image
                }
            }
        }
    }

    // MARK: Controls

    func playPause() {
        position = estimatedPosition(); positionDate = Date()
        send("playpause"); isPlaying.toggle()
    }

    func toggleShuffle() {
        guard let player else { return }
        shuffling.toggle()
        let property = player == .spotify ? "shuffling" : "shuffle enabled"
        run("tell application \"\(player.rawValue)\" to set \(property) to \(shuffling)") { _ in }
    }

    func toggleRepeat() {
        guard let player else { return }
        repeating.toggle()
        let command = player == .spotify ? "set repeating to \(repeating)"
            : "set song repeat to \(repeating ? "all" : "off")"
        run("tell application \"\(player.rawValue)\" to \(command)") { _ in }
    }

    private func readModes() {
        guard let player else { return }
        let script = player == .spotify ? "tell application \"Spotify\" to return (shuffling as text) & \",\" & (repeating as text)"
            : "tell application \"Music\" to return (shuffle enabled as text) & \",\" & ((song repeat is not off) as text)"
        run(script) { [weak self] output in
            guard let parts = output?.components(separatedBy: ","), parts.count == 2 else { return }
            self?.shuffling = parts[0] == "true"
            self?.repeating = parts[1] == "true"
        }
    }
    func next() { send("next track") }
    func previous() { send("previous track") }

    func seek(to fraction: Double) {
        guard let player, duration > 0 else { return }
        let seconds = max(0, min(duration, fraction * duration))
        position = seconds
        positionDate = Date()
        run("tell application \"\(player.rawValue)\" to set player position to \(seconds)") { _ in }
    }

    func openPlayer() {
        guard let player, let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: player.bundleID) else { return }
        NSWorkspace.shared.openApplication(at: url, configuration: .init())
    }

    private func send(_ command: String) {
        guard let player else { return }
        run("tell application \"\(player.rawValue)\" to \(command)") { [weak self] _ in self?.refresh() }
    }

    /// Re-sync when the media tab opens; the progress bar extrapolates between syncs.
    func setVisible(_ visible: Bool) {
        guard visible else { return }
        refresh()
        readModes()
    }

    private func run(_ source: String, completion: @escaping (String?) -> Void) {
        scriptQueue.async {
            var error: NSDictionary?
            let result = NSAppleScript(source: source)?.executeAndReturnError(&error).stringValue
            DispatchQueue.main.async { completion(error == nil ? result : nil) }
        }
    }
}

// MARK: Views

private let ciContext = CIContext(options: [.workingColorSpace: NSNull()])

/// Average colour of an image, brightened enough to read on black.
func averageColor(_ image: NSImage) -> NSColor? {
    guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
    let input = CIImage(cgImage: cg)
    guard let filter = CIFilter(name: "CIAreaAverage", parameters: [kCIInputImageKey: input,
                                                                   kCIInputExtentKey: CIVector(cgRect: input.extent)]),
          let output = filter.outputImage else { return nil }
    var pixel = [UInt8](repeating: 0, count: 4)
    ciContext.render(output, toBitmap: &pixel, rowBytes: 4, bounds: CGRect(x: 0, y: 0, width: 1, height: 1),
                     format: .RGBA8, colorSpace: nil)
    let color = NSColor(red: CGFloat(pixel[0]) / 255, green: CGFloat(pixel[1]) / 255, blue: CGFloat(pixel[2]) / 255, alpha: 1)
    var hue: CGFloat = 0, saturation: CGFloat = 0, brightness: CGFloat = 0, alpha: CGFloat = 0
    color.usingColorSpace(.deviceRGB)?.getHue(&hue, saturation: &saturation, brightness: &brightness, alpha: &alpha)
    return NSColor(hue: hue, saturation: saturation, brightness: max(brightness, 0.7), alpha: 1)
}

private var iconCache: [String: NSImage] = [:]

/// The app's icon flattened to a plain sRGB bitmap. Drawing the original (with its
/// extended-range layers) switches the whole view to EDR and dims everything around it.
func appIcon(_ bundleID: String, size: CGFloat = 52) -> NSImage {
    if let cached = iconCache[bundleID] { return cached }
    let source = NSWorkspace.shared.icon(forFile:
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID)?.path ?? "")
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                     colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return source }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    source.draw(in: NSRect(x: 0, y: 0, width: size, height: size))
    NSGraphicsContext.restoreGraphicsState()
    let image = NSImage(size: NSSize(width: size, height: size))
    image.addRepresentation(rep)
    iconCache[bundleID] = image
    return image
}

func formatTime(_ seconds: Double) -> String {
    guard seconds.isFinite, seconds >= 0 else { return "0:00" }
    let total = Int(seconds)
    return String(format: "%d:%02d", total / 60, total % 60)
}

struct Artwork: View {
    let image: NSImage?
    var size: CGFloat
    var body: some View {
        Group {
            if let image { Image(nsImage: image).resizable().aspectRatio(contentMode: .fill) }
            else { ZStack { Color.white.opacity(0.08); Image(systemName: "music.note").foregroundStyle(.secondary) } }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: size * 0.16, style: .continuous))
    }
}

/// Album art with a soft glow of its own colours behind it; it settles back and dims when paused.
struct GlowingArtwork: View {
    @ObservedObject var media: NowPlaying
    var size: CGFloat

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            if let image = media.artwork {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
                    .frame(width: size, height: size)
                    .blur(radius: 26)
                    .scaleEffect(1.25)
                    .opacity(media.isPlaying ? 0.55 : 0)
            }
            Artwork(image: media.artwork, size: size)
                .overlay(RoundedRectangle(cornerRadius: size * 0.16, style: .continuous)
                    .fill(Color.black.opacity(media.isPlaying ? 0 : 0.45)))
                .scaleEffect(media.isPlaying ? 1 : 0.88)
            if let player = media.player {
                Image(nsImage: appIcon(player.bundleID))
                    .resizable().frame(width: 26, height: 26)
                    .offset(x: 7, y: 7)
            }
        }
        .frame(width: size, height: size)
        .animation(.spring(response: 0.4, dampingFraction: 0.75), value: media.isPlaying)
        .onTapGesture { media.openPlayer() }
    }
}

/// Width bookkeeping for MarqueeText (an object rather than @State: no SwiftUI macros in this build).
final class MarqueeModel: ObservableObject {
    @Published var textWidth: CGFloat = 0
    @Published var boxWidth: CGFloat = 0
}

/// Scrolls text that doesn't fit, pausing at each end; static text when it fits.
struct MarqueeText: View {
    let text: String
    let font: Font
    var color: Color = .white
    @StateObject private var model = MarqueeModel()

    var body: some View {
        let overflow = model.textWidth - model.boxWidth
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: overflow <= 4)) { timeline in
            Text(text).font(font).foregroundStyle(color).lineLimit(1).fixedSize()
                .background(GeometryReader { geo in
                    Color.clear.preference(key: WidthKey.self, value: geo.size.width)
                })
                .onPreferenceChange(WidthKey.self) { model.textWidth = $0 }
                .offset(x: overflow > 4 ? -scroll(overflow, timeline.date) : 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(GeometryReader { geo in
            Color.clear.onAppear { model.boxWidth = geo.size.width }
                .onChange(of: geo.size.width) { _, width in model.boxWidth = width }
        })
        .clipped()
    }

    /// Pause 1.5s, scroll at 30pt/s, pause, scroll back.
    private func scroll(_ overflow: CGFloat, _ date: Date) -> CGFloat {
        let travel = Double(overflow) / 30, pause = 1.5
        let cycle = 2 * (travel + pause)
        let t = date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: cycle)
        if t < pause { return 0 }
        if t < pause + travel { return overflow * CGFloat((t - pause) / travel) }
        if t < 2 * pause + travel { return overflow }
        return overflow * CGFloat(1 - (t - 2 * pause - travel) / travel)
    }
}

private struct WidthKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

struct MediaView: View {
    @ObservedObject var media: NowPlaying

    var body: some View {
        if media.hasTrack {
            HStack(alignment: .center, spacing: 16) {
                GlowingArtwork(media: media, size: 104)
                VStack(alignment: .leading, spacing: 2) {
                    MarqueeText(text: media.title, font: .system(size: 15, weight: .semibold))
                    MarqueeText(text: media.artist, font: .system(size: 13, weight: .medium),
                                color: Color(nsColor: media.accent).opacity(0.9))
                    ProgressBar(media: media).padding(.top, 8)
                    Controls(media: media).padding(.top, 2)
                }
            }
            .padding(.horizontal, 18).padding(.vertical, 12)
            .foregroundStyle(.white)
        } else {
            VStack(spacing: 6) {
                Image(systemName: "music.note").font(.system(size: 22))
                Text("Play something in Spotify or Music").font(.system(size: 12))
            }
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

struct Controls: View {
    @ObservedObject var media: NowPlaying
    var body: some View {
        HStack {
            ControlButton(symbol: "shuffle", size: 13, active: media.shuffling, action: media.toggleShuffle)
            Spacer()
            ControlButton(symbol: "backward.fill", size: 17, action: media.previous)
            Spacer()
            ControlButton(symbol: media.isPlaying ? "pause.fill" : "play.fill", size: 24, action: media.playPause)
            Spacer()
            ControlButton(symbol: "forward.fill", size: 17, action: media.next)
            Spacer()
            ControlButton(symbol: "repeat", size: 13, active: media.repeating, action: media.toggleRepeat)
        }
        .padding(.horizontal, 4)
    }
}

/// Advances every frame from the last synced position while playing; idle when paused.
struct ProgressBar: View {
    @ObservedObject var media: NowPlaying
    private var dragFraction: Double? { media.dragFraction }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !media.isPlaying || dragFraction != nil)) { timeline in
            let position = dragFraction.map { $0 * media.duration } ?? media.estimatedPosition(at: timeline.date)
            let fraction = media.duration > 0 ? position / media.duration : 0
            HStack(spacing: 8) {
                Text(formatTime(position)).frame(width: 34, alignment: .leading)
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.white.opacity(0.18))
                        Capsule().fill(Color.white).frame(width: geo.size.width * max(0, min(1, fraction)))
                    }
                    .frame(height: dragFraction == nil ? 5 : 8)
                    .frame(maxHeight: .infinity)
                    .contentShape(Rectangle())
                    .gesture(DragGesture(minimumDistance: 0)
                        .onChanged { media.dragFraction = max(0, min(1, $0.location.x / max(1, geo.size.width))) }
                        .onEnded { _ in if let fraction = media.dragFraction { media.seek(to: fraction) }; media.dragFraction = nil })
                }
                .frame(height: 12)
                .animation(.easeOut(duration: 0.15), value: dragFraction == nil)
                Text("-" + formatTime(max(0, media.duration - position))).frame(width: 38, alignment: .trailing)
            }
            .font(.system(size: 10, weight: .medium).monospacedDigit())
            .foregroundStyle(.secondary)
        }
    }
}

struct ControlButton: View {
    let symbol: String
    var size: CGFloat = 15
    var active = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: size, weight: .semibold))
                .foregroundStyle(active ? Color.accentColor : .white)
                .frame(width: size + 16, height: size + 12)
                .contentShape(Circle())
        }
        .buttonStyle(PressStyle())
    }
}

struct PressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(Circle().fill(Color.white.opacity(configuration.isPressed ? 0.16 : 0)))
            .scaleEffect(configuration.isPressed ? 0.9 : 1)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
    }
}

/// Four bars in the artwork colour, beside the closed notch while music plays.
struct MusicBars: View {
    @ObservedObject var media: NowPlaying
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 12, paused: !media.isPlaying)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            HStack(alignment: .center, spacing: 2) {
                ForEach(0..<4, id: \.self) { i in
                    let wave = (sin(t * (5 + Double(i) * 1.7) + Double(i)) + 1) / 2
                    Capsule().fill(Color(nsColor: media.accent))
                        .frame(width: 3, height: media.isPlaying ? 4 + 10 * wave : 3)
                }
            }
            .frame(height: 16)
            .animation(.easeInOut(duration: 0.12), value: t)
        }
    }
}
