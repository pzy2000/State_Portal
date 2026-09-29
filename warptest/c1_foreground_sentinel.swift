import AppKit
import Foundation

final class SentinelDelegate: NSObject, NSApplicationDelegate {
    private let outputURL: URL
    private let marker = "WARPTEST-FOREGROUND-SENTINEL-UNCHANGED"
    private let initialPointer: NSPoint
    private let initialClipboardChangeCount: Int
    private var observedFrontmostPIDs = Set<Int32>()
    private var field: NSTextField?
    private var window: NSWindow?
    private var timer: Timer?
    private var activationSource: DispatchSourceSignal?

    init(outputPath: String) {
        outputURL = URL(fileURLWithPath: outputPath)
        initialPointer = NSEvent.mouseLocation
        initialClipboardChangeCount = NSPasteboard.general.changeCount
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let window = NSWindow(
            contentRect: NSRect(x: 160, y: 160, width: 760, height: 180),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "WarpTest C1 Foreground Isolation Sentinel"
        let label = NSTextField(labelWithString: "The background target must not receive focus or change this text:")
        label.frame = NSRect(x: 28, y: 112, width: 700, height: 24)
        let editable = NSTextField(frame: NSRect(x: 28, y: 58, width: 700, height: 38))
        editable.stringValue = marker
        editable.isEditable = true
        editable.isSelectable = true
        window.contentView?.addSubview(label)
        window.contentView?.addSubview(editable)
        field = editable
        self.window = window
        NSApp.setActivationPolicy(.regular)
        signal(SIGUSR1, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
        source.setEventHandler { [weak self] in self?.activateForMeasurement() }
        source.resume()
        activationSource = source
        activateForMeasurement()
        timer = Timer.scheduledTimer(withTimeInterval: 0.01, repeats: true) { [weak self] _ in
            self?.snapshot()
        }
        snapshot()
    }

    private func activateForMeasurement() {
        window?.makeKeyAndOrderFront(nil)
        if let editable = field {
            window?.makeFirstResponder(editable)
        }
        NSApp.activate(ignoringOtherApps: true)
        snapshot()
    }

    private func snapshot() {
        let frontmostPID = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1
        observedFrontmostPIDs.insert(frontmostPID)
        let pointer = NSEvent.mouseLocation
        let payload: [String: Any] = [
            "schema_version": 1,
            "pid": ProcessInfo.processInfo.processIdentifier,
            "frontmost_pid": frontmostPID,
            "observed_frontmost_pids": observedFrontmostPIDs.sorted(),
            "initial_pointer": [initialPointer.x, initialPointer.y],
            "pointer": [pointer.x, pointer.y],
            "initial_clipboard_change_count": initialClipboardChangeCount,
            "clipboard_change_count": NSPasteboard.general.changeCount,
            "marker": marker,
            "foreground_text": field?.stringValue ?? "",
            "sampled_at_unix_ms": Int(Date().timeIntervalSince1970 * 1000),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else {
            return
        }
        try? data.write(to: outputURL, options: [.atomic])
    }
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: c1-foreground-sentinel STATUS_JSON\n", stderr)
    exit(2)
}

let application = NSApplication.shared
let delegate = SentinelDelegate(outputPath: CommandLine.arguments[1])
application.delegate = delegate
application.run()
