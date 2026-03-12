// audio_tap.swift
// Captures system or per-app audio using ScreenCaptureKit (macOS 13+).
// Outputs raw float32 interleaved stereo PCM to stdout.
//
// Build:
//   swiftc -O -o audio_tap audio_tap.swift -framework ScreenCaptureKit -framework CoreMedia
//
// Usage:
//   ./audio_tap --system
//   ./audio_tap --name "Google Chrome"
//   ./audio_tap --list

import Foundation
import ScreenCaptureKit
import CoreMedia

// MARK: - Helpers

func log(_ msg: String) {
    fputs("[audio_tap] \(msg)\n", stderr)
}

// MARK: - Audio Capturer

class AudioCapturer: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    var sampleCount = 0
    var maxAmplitude: Float = 0
    var reportedFormat = false

    func startCapture(appBundleIDs: [String]?) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // Don't capture video — just audio
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1) // 1 fps minimum

        let filter: SCContentFilter
        if let bundleIDs = appBundleIDs {
            // Per-app capture: find matching running applications
            let matchingApps = content.applications.filter { app in
                bundleIDs.contains { bid in
                    app.bundleIdentifier.localizedCaseInsensitiveContains(bid) ||
                    app.applicationName.localizedCaseInsensitiveContains(bid)
                }
            }
            guard !matchingApps.isEmpty else {
                log("ERROR: No running apps match: \(bundleIDs)")
                log("Running apps with audio:")
                for app in content.applications {
                    log("  \(app.applicationName) (\(app.bundleIdentifier))")
                }
                exit(1)
            }
            for app in matchingApps {
                log("Capturing: \(app.applicationName) (\(app.bundleIdentifier))")
            }
            // Include only the matching apps' audio
            filter = SCContentFilter(display: content.displays[0],
                                     including: matchingApps,
                                     exceptingWindows: [])
        } else {
            // System-wide capture
            log("Capturing all system audio")
            filter = SCContentFilter(display: content.displays[0],
                                     excludingWindows: [])
        }

        stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream!.addStreamOutput(self, type: .audio, sampleHandlerQueue: .global(qos: .userInteractive))
        try await stream!.startCapture()
        log("Capture started")
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }

        // Report format once
        if !reportedFormat, let fmt = CMSampleBufferGetFormatDescription(sampleBuffer) {
            let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmt)?.pointee
            if let asbd = asbd {
                fputs("[audio_tap] SAMPLE_RATE=\(Int(asbd.mSampleRate))\n", stderr)
                fputs("[audio_tap] CHANNELS=\(asbd.mChannelsPerFrame)\n", stderr)
                log("Format: \(Int(asbd.mSampleRate))Hz, \(asbd.mChannelsPerFrame)ch, \(asbd.mBitsPerChannel)bit")
                log("Flags: \(String(asbd.mFormatFlags, radix: 16))")
            }
            reportedFormat = true
        }

        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        var length = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0,
                                                  lengthAtOffsetOut: nil,
                                                  totalLengthOut: &length,
                                                  dataPointerOut: &dataPointer)
        guard status == noErr, let ptr = dataPointer, length > 0 else { return }

        let floatPtr = UnsafeRawPointer(ptr).assumingMemoryBound(to: Float.self)
        let totalFloats = length / 4
        let channels = 2
        let framesInBuffer = totalFloats / channels

        // Track max amplitude
        sampleCount += framesInBuffer
        for i in 0..<min(framesInBuffer, 1000) {
            let v = abs(floatPtr[i])
            if v > maxAmplitude { maxAmplitude = v }
        }

        // Log periodically
        if sampleCount % 48000 < framesInBuffer {
            log("Streaming: \(sampleCount / 48000)s, peak=\(String(format: "%.4f", maxAmplitude))")
        }

        // SCK delivers non-interleaved: [L0..Ln, R0..Rn]
        // Interleave to [L0, R0, L1, R1, ...] for stdout
        var interleaved = [Float](repeating: 0, count: totalFloats)
        for f in 0..<framesInBuffer {
            for ch in 0..<channels {
                interleaved[f * channels + ch] = floatPtr[ch * framesInBuffer + f]
            }
        }
        interleaved.withUnsafeBufferPointer { buf in
            write(STDOUT_FILENO, buf.baseAddress!, totalFloats * 4)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("Stream stopped: \(error)")
        exit(1)
    }
}

// MARK: - List running apps

func listApps() async {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        fputs("Running applications (available for audio capture):\n", stderr)
        let sorted = content.applications.sorted { $0.applicationName < $1.applicationName }
        for app in sorted {
            if !app.applicationName.isEmpty {
                fputs("  \(app.applicationName) (\(app.bundleIdentifier))\n", stderr)
            }
        }
    } catch {
        fputs("Error listing apps: \(error)\n", stderr)
    }
}

// MARK: - Main

signal(SIGINT) { _ in fputs("\n[audio_tap] Stopped.\n", stderr); exit(0) }
signal(SIGTERM) { _ in exit(0) }
signal(SIGPIPE) { _ in exit(0) }
setbuf(stdout, nil)

let args = CommandLine.arguments

if args.count >= 2 && args[1] == "--list" {
    let sema = DispatchSemaphore(value: 0)
    Task {
        await listApps()
        sema.signal()
    }
    sema.wait()
    exit(0)
}

var appNames: [String]? = nil
var isSystem = false

if args.count >= 3 && args[1] == "--name" {
    appNames = [args[2]]
} else if args.count >= 2 && args[1] == "--system" {
    isSystem = true
} else {
    fputs("Usage: audio_tap [--name <app_name> | --system | --list]\n", stderr)
    exit(1)
}

let capturer = AudioCapturer()
Task {
    do {
        try await capturer.startCapture(appBundleIDs: isSystem ? nil : appNames)
    } catch {
        log("ERROR: \(error)")
        log("")
        log("If you see a TCC/permission error:")
        log("  1. Open System Settings > Privacy & Security > Screen & System Audio Recording")
        log("  2. Add your terminal app (Terminal, iTerm2, etc.) to the list")
        log("  3. Restart your terminal and try again")
        exit(1)
    }
}

dispatchMain()
