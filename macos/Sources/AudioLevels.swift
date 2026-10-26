// Live spectrum of the music player's output, for the closed-notch bars.
//
// A Core Audio process tap (macOS 14.2+) reads the player's audio without changing what
// you hear; an FFT of each buffer is folded into four bands (bass to treble). The tap
// runs only while music plays and the bars are on screen. macOS asks once for System
// Audio Recording permission; without it the bars fall back to a gentle idle motion.
import Accelerate
import AppKit
import CoreAudio

final class AudioLevels: ObservableObject {
    static let bandCount = 4
    /// 0...1 per band, smoothed, published at display rate.
    @Published private(set) var bands = [Float](repeating: 0, count: bandCount)
    /// False until real samples arrive; views use an idle animation meanwhile.
    @Published private(set) var live = false

    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private let queue = DispatchQueue(label: "branchglance.audio.tap", qos: .userInteractive)
    private var running = false

    // FFT state, touched only on `queue`.
    private let fftSize = 1024
    private var dft: OpaquePointer?
    private var window: [Float] = []
    private var peaks = [Float](repeating: 0.05, count: bandCount)
    private var smoothed = [Float](repeating: 0, count: bandCount)
    private var lastPublish = Date.distantPast

    init() {
        dft = vDSP_DFT_zrop_CreateSetup(nil, vDSP_Length(fftSize), .FORWARD)
        window = [Float](repeating: 0, count: fftSize)
        vDSP_hann_window(&window, vDSP_Length(fftSize), Int32(vDSP_HANN_NORM))
    }

    deinit { stop() }

    /// Tap the given app's audio, or everything if it isn't found.
    func start(bundleID: String) {
        guard !running else { return }
        let description: CATapDescription
        if let process = processObject(for: bundleID) {
            description = CATapDescription(stereoMixdownOfProcesses: [process])
        } else {
            description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        }
        description.uuid = UUID()
        description.muteBehavior = .unmuted
        description.isPrivate = true
        guard AudioHardwareCreateProcessTap(description, &tapID) == noErr else { return }

        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Branchglance Visualizer",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString]],
        ]
        guard AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID) == noErr else {
            stop(); return
        }
        let status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, queue) { [weak self] _, input, _, _, _ in
            self?.process(input)
        }
        guard status == noErr, AudioDeviceStart(aggregateID, procID) == noErr else { stop(); return }
        running = true
    }

    func stop() {
        if aggregateID != kAudioObjectUnknown {
            if let procID {
                AudioDeviceStop(aggregateID, procID)
                AudioDeviceDestroyIOProcID(aggregateID, procID)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        procID = nil
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
        running = false
        DispatchQueue.main.async { self.live = false; self.bands = [Float](repeating: 0, count: Self.bandCount) }
    }

    /// Core Audio's object for the first running process of the app.
    private func processObject(for bundleID: String) -> AudioObjectID? {
        guard let pid = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first?.processIdentifier
        else { return nil }
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var input = pid
        var object = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                                UInt32(MemoryLayout<pid_t>.size), &input, &size, &object)
        return status == noErr && object != kAudioObjectUnknown ? object : nil
    }

    // MARK: Analysis (on `queue`)

    private var samples: [Float] = []

    private func process(_ input: UnsafePointer<AudioBufferList>) {
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard let buffer = list.first, let data = buffer.mData else { return }
        let channels = Int(max(1, buffer.mNumberChannels))
        let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
        let floats = data.assumingMemoryBound(to: Float.self)
        // Mix interleaved channels down to mono.
        for frame in stride(from: 0, to: count - channels + 1, by: channels) {
            var sum: Float = 0
            for c in 0..<channels { sum += floats[frame + c] }
            samples.append(sum / Float(channels))
        }
        while samples.count >= fftSize {
            analyse(Array(samples.prefix(fftSize)))
            samples.removeFirst(fftSize / 2)
        }
    }

    private func analyse(_ frame: [Float]) {
        guard let dft else { return }
        var windowed = [Float](repeating: 0, count: fftSize)
        vDSP_vmul(frame, 1, window, 1, &windowed, 1, vDSP_Length(fftSize))
        let half = fftSize / 2
        var evens = [Float](repeating: 0, count: half), odds = [Float](repeating: 0, count: half)
        for i in 0..<half { evens[i] = windowed[2 * i]; odds[i] = windowed[2 * i + 1] }
        var real = [Float](repeating: 0, count: half), imag = [Float](repeating: 0, count: half)
        vDSP_DFT_Execute(dft, evens, odds, &real, &imag)
        var magnitudes = [Float](repeating: 0, count: half)
        real.withUnsafeMutableBufferPointer { r in
            imag.withUnsafeMutableBufferPointer { i in
                var split = DSPSplitComplex(realp: r.baseAddress!, imagp: i.baseAddress!)
                vDSP_zvabs(&split, 1, &magnitudes, 1, vDSP_Length(half))
            }
        }
        // Bins are ~47 Hz wide at 48 kHz: bass, low-mid, high-mid, treble.
        let ranges = [1..<5, 5..<14, 14..<45, 45..<170]
        for (band, range) in ranges.enumerated() {
            var value: Float = 0
            magnitudes[range].withUnsafeBufferPointer { vDSP_meanv($0.baseAddress!, 1, &value, vDSP_Length(range.count)) }
            // Adaptive ceiling per band: rises instantly, decays slowly, so quiet songs still move.
            peaks[band] = max(value, peaks[band] * 0.995, 0.02)
            // Curve so quieter moments drop visibly instead of hovering near the top.
            let level = powf(min(1, value / peaks[band]), 1.8)
            // Fast attack, slower release.
            smoothed[band] += (level - smoothed[band]) * (level > smoothed[band] ? 0.6 : 0.2)
        }
        let now = Date()
        guard now.timeIntervalSince(lastPublish) > 1.0 / 30 else { return }
        lastPublish = now
        let snapshot = smoothed
        DispatchQueue.main.async { self.bands = snapshot; self.live = true }
    }
}
