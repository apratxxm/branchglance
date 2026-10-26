// Volume, brightness and battery events, shown as a short activity around the closed notch.
import AppKit
import AudioToolbox
import CoreAudio
import CoreMediaIO
import IOKit.ps
import SwiftUI

enum HUDKind: Equatable {
    case volume(level: Float, muted: Bool)
    case brightness(level: Float)
    case battery(percent: Int, charging: Bool)
    case media
    case capsLock(on: Bool)
    case microphone
    case camera
    case output(name: String, symbol: String)

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
        case let .capsLock(on): return on ? "capslock.fill" : "capslock"
        case .microphone: return "mic.fill"
        case .camera: return "video.fill"
        case let .output(_, symbol): return symbol
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
        watchCapsLock()
        watchMicrophone()
        watchCameras()
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
        let switched = outputDevice != kAudioObjectUnknown
        outputDevice = device
        if switched, let name = deviceName(device) { onEvent?(.output(name: name, symbol: outputSymbol(device, name))) }
        for selector in [kAudioHardwareServiceDeviceProperty_VirtualMainVolume, kAudioDevicePropertyMute] {
            var property = address(selector, scope: kAudioDevicePropertyScopeOutput)
            AudioObjectAddPropertyListenerBlock(device, &property, .main) { [weak self, device] _, _ in
                guard let self, device == self.outputDevice else { return }
                self.reportVolume()
            }
        }
        lastVolume = currentVolume()
    }

    private func deviceName(_ device: AudioObjectID) -> String? {
        var name: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var property = address(kAudioObjectPropertyName)
        guard AudioObjectGetPropertyData(device, &property, 0, nil, &size, &name) == noErr else { return nil }
        return name?.takeRetainedValue() as String?
    }

    private func outputSymbol(_ device: AudioObjectID, _ name: String) -> String {
        var transport: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        var property = address(kAudioDevicePropertyTransportType)
        _ = AudioObjectGetPropertyData(device, &property, 0, nil, &size, &transport)
        let lower = name.lowercased()
        if lower.contains("airpods max") { return "airpodsmax" }
        if lower.contains("airpods pro") { return "airpodspro" }
        if lower.contains("airpods") { return "airpods" }
        switch transport {
        case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE: return "headphones"
        case kAudioDeviceTransportTypeBuiltIn: return lower.contains("headphone") ? "headphones" : "laptopcomputer"
        case kAudioDeviceTransportTypeAirPlay: return "airplayaudio"
        case kAudioDeviceTransportTypeHDMI, kAudioDeviceTransportTypeDisplayPort: return "tv"
        default: return "hifispeaker"
        }
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

    // MARK: Caps Lock (modifier state; global key monitors would need Accessibility)

    private var capsTimer: Timer?
    private var capsOn = NSEvent.modifierFlags.contains(.capsLock)

    private func watchCapsLock() {
        let timer = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self else { return }
            let on = CGEventSource.flagsState(.combinedSessionState).contains(.maskAlphaShift)
            guard on != self.capsOn else { return }
            self.capsOn = on
            self.onEvent?(.capsLock(on: on))
        }
        timer.tolerance = 0.05
        RunLoop.main.add(timer, forMode: .common)
        capsTimer = timer
    }

    // MARK: Microphone and camera in use (Core Audio / CoreMediaIO listeners)

    private var inputDevice = AudioObjectID(kAudioObjectUnknown)
    private var micRunning = false

    private func watchMicrophone() {
        var defaultInput = address(kAudioHardwarePropertyDefaultInputDevice)
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &defaultInput, .main) { [weak self] _, _ in
            self?.attachToInput()
        }
        attachToInput()
    }

    private func attachToInput() {
        var device = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var defaultInput = address(kAudioHardwarePropertyDefaultInputDevice)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &defaultInput, 0, nil, &size, &device) == noErr,
              device != inputDevice, device != kAudioObjectUnknown else { return }
        inputDevice = device
        micRunning = isRunning(device)
        var running = address(kAudioDevicePropertyDeviceIsRunningSomewhere)
        AudioObjectAddPropertyListenerBlock(device, &running, .main) { [weak self, device] _, _ in
            guard let self, device == self.inputDevice else { return }
            let now = self.isRunning(device)
            if now && !self.micRunning { self.onEvent?(.microphone) }
            self.micRunning = now
        }
    }

    private func isRunning(_ device: AudioObjectID) -> Bool {
        var running: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        var property = address(kAudioDevicePropertyDeviceIsRunningSomewhere)
        return AudioObjectGetPropertyData(device, &property, 0, nil, &size, &running) == noErr && running != 0
    }

    private var cameraRunning: [CMIOObjectID: Bool] = [:]

    private func watchCameras() {
        var devicesAddress = CMIOObjectPropertyAddress(mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyDevices),
                                                       mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
                                                       mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
        var size: UInt32 = 0
        guard CMIOObjectGetPropertyDataSize(CMIOObjectID(kCMIOObjectSystemObject), &devicesAddress, 0, nil, &size) == noErr else { return }
        var devices = [CMIOObjectID](repeating: 0, count: Int(size) / MemoryLayout<CMIOObjectID>.size)
        var used: UInt32 = 0
        guard CMIOObjectGetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &devicesAddress, 0, nil, size, &used, &devices) == noErr
        else { return }
        for device in devices {
            var running = CMIOObjectPropertyAddress(mSelector: CMIOObjectPropertySelector(kCMIODevicePropertyDeviceIsRunningSomewhere),
                                                    mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeWildcard),
                                                    mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementWildcard))
            cameraRunning[device] = cameraIsRunning(device)
            CMIOObjectAddPropertyListenerBlock(device, &running, .main) { [weak self] _, _ in
                guard let self else { return }
                let now = self.cameraIsRunning(device)
                if now && self.cameraRunning[device] != true { self.onEvent?(.camera) }
                self.cameraRunning[device] = now
            }
        }
    }

    private func cameraIsRunning(_ device: CMIOObjectID) -> Bool {
        var address = CMIOObjectPropertyAddress(mSelector: CMIOObjectPropertySelector(kCMIODevicePropertyDeviceIsRunningSomewhere),
                                                mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeWildcard),
                                                mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementWildcard))
        var running: UInt32 = 0
        var used: UInt32 = 0
        return CMIOObjectGetPropertyData(device, &address, 0, nil, UInt32(MemoryLayout<UInt32>.size), &used, &running) == noErr
            && running != 0
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
    var levels: AudioLevels?

    var body: some View {
        // Each wing's content is centred in the wing, hugging the camera housing.
        HStack(spacing: 0) {
            leading.frame(maxWidth: .infinity)
            Color.clear.frame(width: notchWidth)
            trailing.frame(maxWidth: .infinity)
        }
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }

    @ViewBuilder private var leading: some View {
        if case .media = kind {
            Artwork(image: media.artwork, size: 22)
        } else {
            let icon = Image(systemName: kind.symbol).font(.system(size: 13, weight: .semibold)).foregroundStyle(tint)
            if glows {
                // Two soft shadows in the icon's colour give the lit-key bloom.
                icon.shadow(color: tint.opacity(0.9), radius: 3).shadow(color: tint.opacity(0.6), radius: 8)
            } else {
                icon
            }
        }
    }

    @ViewBuilder private var trailing: some View {
        switch kind {
        case let .volume(level, muted): LevelBar(level: muted ? 0 : level)
        case let .brightness(level): LevelBar(level: level)
        case let .battery(percent, _):
            Text("\(percent)%").font(.system(size: 12, weight: .semibold, design: .rounded)).foregroundStyle(tint)
        case .media:
            MusicBars(media: media, levels: levels)
        case let .capsLock(on):
            Text(on ? "On" : "Off").font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(on ? tint : .white.opacity(0.6))
        case .microphone, .camera:
            Circle().fill(tint).frame(width: 7, height: 7)
        case let .output(name, _):
            Text(name).font(.system(size: 11, weight: .semibold)).lineLimit(1).minimumScaleFactor(0.7)
        }
    }

    /// Lit states that should bloom: Caps Lock turning on, mic and camera in use.
    private var glows: Bool {
        switch kind {
        case let .capsLock(on): return on
        case .microphone, .camera: return true
        default: return false
        }
    }

    private var tint: Color {
        if case let .battery(percent, charging) = kind {
            return charging ? .green : percent <= 20 ? .red : .white
        }
        if case let .capsLock(on) = kind { return on ? Color(red: 0.3, green: 0.95, blue: 0.45) : .white.opacity(0.6) }
        if case .microphone = kind { return .orange }
        if case .camera = kind { return .green }
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
