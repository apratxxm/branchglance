// Volume, brightness and battery events, shown as a short activity around the closed notch.
import AppKit
import AudioToolbox
import CoreAudio
import IOKit.ps
import SwiftUI

enum HUDKind: Equatable {
    case volume(level: Float, muted: Bool)
    case brightness(level: Float)
    case battery(percent: Int, charging: Bool)
    case media

    var symbol: String {
        switch self {
        case let .volume(level, muted):
            if muted || level == 0 { return "speaker.slash.fill" }
            return level < 0.34 ? "speaker.wave.1.fill" : level < 0.67 ? "speaker.wave.2.fill" : "speaker.wave.3.fill"
        case let .brightness(level): return level < 0.5 ? "sun.min.fill" : "sun.max.fill"
        case let .battery(percent, charging):
            if charging { return "battery.100.bolt" }
            return percent < 15 ? "battery.0" : percent < 40 ? "battery.25" : percent < 65 ? "battery.50"
                : percent < 90 ? "battery.75" : "battery.100"
        case .media: return "music.note"
        }
    }
}

final class SystemMonitor {
    var onEvent: ((HUDKind) -> Void)?

    private var outputDevice = AudioObjectID(kAudioObjectUnknown)
    private var lastVolume: (Float, Bool)?
    private var brightnessTimer: Timer?
    private var lastBrightness: Float?
    private var batterySource: CFRunLoopSource?
    private var lastBattery: (Int, Bool)?

    func start() {
        watchDefaultOutput()
        watchBrightness()
        watchBattery()
    }

    // MARK: Volume (CoreAudio property listeners, no polling)

    private func address(_ selector: AudioObjectPropertySelector,
                         scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    private func watchDefaultOutput() {
        var defaultAddress = address(kAudioHardwarePropertyDefaultOutputDevice)
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &defaultAddress, .main) { [weak self] _, _ in
            self?.attachToOutput()
        }
        attachToOutput()
    }

    private func attachToOutput() {
        var device = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var defaultAddress = address(kAudioHardwarePropertyDefaultOutputDevice)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &defaultAddress, 0, nil, &size, &device) == noErr,
              device != outputDevice else { return }
        outputDevice = device
        for selector in [kAudioHardwareServiceDeviceProperty_VirtualMainVolume, kAudioDevicePropertyMute] {
            var property = address(selector, scope: kAudioDevicePropertyScopeOutput)
            AudioObjectAddPropertyListenerBlock(device, &property, .main) { [weak self, device] _, _ in
                guard let self, device == self.outputDevice else { return }
                self.reportVolume()
            }
        }
        lastVolume = currentVolume()
    }

    private func currentVolume() -> (Float, Bool)? {
        var volume: Float32 = 0, muted: UInt32 = 0
        var size = UInt32(MemoryLayout<Float32>.size)
        var volumeAddress = address(kAudioHardwareServiceDeviceProperty_VirtualMainVolume, scope: kAudioDevicePropertyScopeOutput)
        guard AudioObjectGetPropertyData(outputDevice, &volumeAddress, 0, nil, &size, &volume) == noErr else { return nil }
        size = UInt32(MemoryLayout<UInt32>.size)
        var muteAddress = address(kAudioDevicePropertyMute, scope: kAudioDevicePropertyScopeOutput)
        _ = AudioObjectGetPropertyData(outputDevice, &muteAddress, 0, nil, &size, &muted)
        return (volume, muted != 0)
    }

    private func reportVolume() {
        guard let (level, muted) = currentVolume() else { return }
        if let last = lastVolume, abs(last.0 - level) < 0.001, last.1 == muted { return }
        lastVolume = (level, muted)
        onEvent?(.volume(level: level, muted: muted))
    }

    // MARK: Brightness (DisplayServices; a cheap read twice a second)

    private typealias GetBrightness = @convention(c) (UInt32, UnsafeMutablePointer<Float>) -> Int32
    private lazy var getBrightness: GetBrightness? = {
        guard let handle = dlopen("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices", RTLD_LAZY),
              let symbol = dlsym(handle, "DisplayServicesGetBrightness") else { return nil }
        return unsafeBitCast(symbol, to: GetBrightness.self)
    }()

    private func readBrightness() -> Float? {
        guard let getBrightness else { return nil }
        var level: Float = 0
        return getBrightness(CGMainDisplayID(), &level) == 0 ? level : nil
    }

    private func watchBrightness() {
        lastBrightness = readBrightness()
        guard lastBrightness != nil else { return }
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self, let level = self.readBrightness() else { return }
            defer { self.lastBrightness = level }
            // Ignore auto-brightness drift; react to key-press sized steps.
            if let last = self.lastBrightness, abs(last - level) > 0.02 { self.onEvent?(.brightness(level: level)) }
        }
        timer.tolerance = 0.2
        RunLoop.main.add(timer, forMode: .common)
        brightnessTimer = timer
    }

    // MARK: Battery (IOKit power source notifications)

    private func watchBattery() {
        lastBattery = batteryState()
        let context = Unmanaged.passUnretained(self).toOpaque()
        guard let source = IOPSNotificationCreateRunLoopSource({ context in
            guard let context else { return }
            Unmanaged<SystemMonitor>.fromOpaque(context).takeUnretainedValue().batteryChanged()
        }, context)?.takeRetainedValue() else { return }
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .defaultMode)
        batterySource = source
    }

    private func batteryState() -> (Int, Bool)? {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let list = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef] else { return nil }
        for source in list {
            guard let description = IOPSGetPowerSourceDescription(info, source)?.takeUnretainedValue() as? [String: Any],
                  description[kIOPSTypeKey] as? String == kIOPSInternalBatteryType,
                  let capacity = description[kIOPSCurrentCapacityKey] as? Int else { continue }
            let onAC = description[kIOPSPowerSourceStateKey] as? String == kIOPSACPowerValue
            return (capacity, onAC)
        }
        return nil
    }

    /// Report plugging in, unplugging, and crossing 20% / 10% while on battery.
    private func batteryChanged() {
        guard let (percent, charging) = batteryState() else { return }
        defer { lastBattery = (percent, charging) }
        guard let last = lastBattery else { return }
        let crossedLow = !charging && [20, 10].contains { last.0 > $0 && percent <= $0 }
        if last.1 != charging || crossedLow { onEvent?(.battery(percent: percent, charging: charging)) }
    }
}

// MARK: Activity view

/// Fills the wings either side of the camera housing: an icon on the left, the value on the right.
struct HUDView: View {
    let kind: HUDKind
    let notchWidth: CGFloat
    @ObservedObject var media: NowPlaying

    var body: some View {
        HStack(spacing: 0) {
            leading.frame(maxWidth: .infinity, alignment: .leading)
            Color.clear.frame(width: notchWidth)
            trailing.frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, 10)
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }

    @ViewBuilder private var leading: some View {
        if case .media = kind {
            Artwork(image: media.artwork, size: 20)
        } else {
            Image(systemName: kind.symbol).font(.system(size: 13, weight: .semibold))
                .foregroundStyle(tint)
        }
    }

    @ViewBuilder private var trailing: some View {
        switch kind {
        case let .volume(level, muted): LevelBar(level: muted ? 0 : level)
        case let .brightness(level): LevelBar(level: level)
        case let .battery(percent, _):
            Text("\(percent)%").font(.system(size: 12, weight: .semibold, design: .rounded)).foregroundStyle(tint)
        case .media:
            Image(systemName: media.isPlaying ? "waveform" : "pause.fill")
                .font(.system(size: 13, weight: .semibold))
                .symbolEffect(.variableColor.iterative, isActive: media.isPlaying)
        }
    }

    private var tint: Color {
        if case let .battery(percent, charging) = kind {
            return charging ? .green : percent <= 20 ? .red : .white
        }
        return .white
    }
}

struct LevelBar: View {
    let level: Float
    var body: some View {
        ZStack(alignment: .leading) {
            Capsule().fill(Color.white.opacity(0.2))
            Capsule().fill(Color.white).frame(width: 56 * CGFloat(max(0, min(1, level))))
        }
        .frame(width: 56, height: 5)
    }
}
