// Now playing for Spotify and Apple Music.
// Track changes arrive through each player's distributed notification, so nothing polls
// while music is idle; the playback position is read once a second only while the media
// tab is on screen.
import AppKit
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
    @Published var artwork: NSImage?

    /// Called on play/pause and track changes, for the closed-notch activity.
    var onChange: (() -> Void)?
    private var positionTimer: Timer?
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

    func playPause() { send("playpause"); isPlaying.toggle() }
    func next() { send("next track") }
    func previous() { send("previous track") }

    func seek(to fraction: Double) {
        guard let player, duration > 0 else { return }
        let seconds = max(0, min(duration, fraction * duration))
        position = seconds
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

    /// Track the position while the media tab is visible; stop when it isn't.
    func setVisible(_ visible: Bool) {
        positionTimer?.invalidate()
        positionTimer = nil
        guard visible else { return }
        refresh()
        positionTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self, self.isPlaying else { return }
            self.position = min(self.duration, self.position + 1)
        }
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
        .clipShape(RoundedRectangle(cornerRadius: size * 0.18, style: .continuous))
    }
}

struct MediaView: View {
    @ObservedObject var media: NowPlaying

    var body: some View {
        if media.hasTrack {
            HStack(spacing: 14) {
                Artwork(image: media.artwork, size: 86)
                    .onTapGesture { media.openPlayer() }
                VStack(alignment: .leading, spacing: 6) {
                    Text(media.title).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                    Text(media.artist).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                    ProgressBar(media: media)
                    HStack(spacing: 26) {
                        ControlButton(symbol: "backward.fill", action: media.previous)
                        ControlButton(symbol: media.isPlaying ? "pause.fill" : "play.fill", size: 20, action: media.playPause)
                        ControlButton(symbol: "forward.fill", action: media.next)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .foregroundStyle(.white)
        } else {
            VStack(spacing: 6) {
                Image(systemName: "music.note").font(.system(size: 22))
                Text("Nothing playing in Spotify or Music").font(.system(size: 12))
            }
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

struct ProgressBar: View {
    @ObservedObject var media: NowPlaying
    var body: some View {
        VStack(spacing: 3) {
            GeometryReader { geo in
                let fraction = media.duration > 0 ? media.position / media.duration : 0
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.18))
                    Capsule().fill(Color.white).frame(width: geo.size.width * max(0, min(1, fraction)))
                }
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onEnded { value in
                    media.seek(to: value.location.x / max(1, geo.size.width))
                })
            }
            .frame(height: 4)
            HStack {
                Text(formatTime(media.position))
                Spacer()
                Text("-" + formatTime(max(0, media.duration - media.position)))
            }
            .font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
        }
    }
}

struct ControlButton: View {
    let symbol: String
    var size: CGFloat = 15
    let action: () -> Void
    var body: some View {
        Button(action: action) { Image(systemName: symbol).font(.system(size: size)) }
            .buttonStyle(.plain)
    }
}
