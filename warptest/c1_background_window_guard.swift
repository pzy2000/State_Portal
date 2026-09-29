import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

guard CommandLine.arguments.count == 3,
      let processGroup = pid_t(CommandLine.arguments[1]) else {
    fputs("usage: c1-background-window-guard PROCESS_GROUP STATUS_JSON\n", stderr)
    exit(2)
}

let statusURL = URL(fileURLWithPath: CommandLine.arguments[2])
var observedApplicationPIDs = Set<pid_t>()
var observedWindowPIDs = Set<pid_t>()
var observedOnscreenWindowPIDs = Set<pid_t>()
var observedActivePIDs = Set<pid_t>()
var hideAttempts = 0
var hideSuccesses = 0
var appKitHideSuccesses = 0
var axHideSuccesses = 0
var minimizeAttempts = 0
var repositionAttempts = 0
var repositionSuccesses = 0
var accessibilitySuccesses = 0
var axTimeouts = 0

// A busy application (a Unity Editor opening a large project) does not service
// accessibility requests promptly, and the default AX timeout is several seconds.
// Left unbounded, one blocked request per scan consumes the whole window-hiding
// budget: measured at 327 ms per scan against jynew, which kept its startup
// window on screen for 14.4 s against a 10 s budget. Bound every request instead
// so a slow application costs one skipped attempt rather than the budget.
let axMessagingTimeoutSeconds: Float = 0.2
AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), axMessagingTimeoutSeconds)

func axApplication(_ pid: pid_t) -> AXUIElement {
    let element = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(element, axMessagingTimeoutSeconds)
    return element
}

func recordAX(_ error: AXError) -> Bool {
    if error == .success {
        accessibilitySuccesses += 1
        return true
    }
    if error == .cannotComplete { axTimeouts += 1 }
    return false
}
var lastVisiblePIDs: [pid_t] = []
var lastHiddenPIDs: [pid_t] = []
var emptyTicks = 0
var statusTick = 0
var scanCount = 0
var firstOnscreenUnixMs: Int? = nil
var lastOnscreenUnixMs: Int? = nil
var firstHiddenAfterOnscreenUnixMs: Int? = nil
var visibleIntervalStartUnixMs: Int? = nil
var maxOnscreenDurationMs = 0

func processGroupExists() -> Bool {
    errno = 0
    if kill(-processGroup, 0) == 0 { return true }
    return errno == EPERM
}

func writeStatus(groupExists: Bool) {
    let payload: [String: Any] = [
        "schema_version": 1,
        "mode": "macos_process_group_hide_v1",
        "guard_pid": ProcessInfo.processInfo.processIdentifier,
        "target_process_group": processGroup,
        "group_exists": groupExists,
        "observed_application_pids": observedApplicationPIDs.sorted(),
        "observed_window_pids": observedWindowPIDs.sorted(),
        "observed_onscreen_window_pids": observedOnscreenWindowPIDs.sorted(),
        "observed_active_pids": observedActivePIDs.sorted(),
        "hide_attempts": hideAttempts,
        "hide_successes": hideSuccesses,
        "appkit_hide_successes": appKitHideSuccesses,
        "ax_hide_successes": axHideSuccesses,
        "ax_timeouts": axTimeouts,
        "minimize_attempts": minimizeAttempts,
        "reposition_attempts": repositionAttempts,
        "reposition_successes": repositionSuccesses,
        "accessibility_successes": accessibilitySuccesses,
        "scan_count": scanCount,
        "first_onscreen_unix_ms": firstOnscreenUnixMs.map { $0 as Any } ?? NSNull(),
        "last_onscreen_unix_ms": lastOnscreenUnixMs.map { $0 as Any } ?? NSNull(),
        "first_hidden_after_onscreen_unix_ms": firstHiddenAfterOnscreenUnixMs.map { $0 as Any } ?? NSNull(),
        "max_onscreen_duration_ms": maxOnscreenDurationMs,
        "hidden_application_pids": lastHiddenPIDs,
        "visible_application_pids": lastVisiblePIDs,
        "sampled_at_unix_ms": Int(Date().timeIntervalSince1970 * 1000),
    ]
    guard let data = try? JSONSerialization.data(
        withJSONObject: payload, options: [.sortedKeys]
    ) else { return }
    try? data.write(to: statusURL, options: [.atomic])
}

while true {
    autoreleasepool {
        scanCount += 1
        let applications = NSWorkspace.shared.runningApplications.filter {
            getpgid($0.processIdentifier) == processGroup
        }
        let windowInfo = (CGWindowListCopyWindowInfo(
            [.optionAll, .excludeDesktopElements], kCGNullWindowID
        ) as? [[String: Any]]) ?? []
        var windowPIDs = Set<pid_t>()
        var visible = Set<pid_t>()
        for window in windowInfo {
            guard let owner = window[kCGWindowOwnerPID as String] as? NSNumber else {
                continue
            }
            let pid = pid_t(owner.int32Value)
            guard pid > 0 && getpgid(pid) == processGroup else { continue }
            windowPIDs.insert(pid)
            observedWindowPIDs.insert(pid)
            let onScreen = (window[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue ?? false
            let alpha = (window[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1.0
            if onScreen && alpha > 0 {
                visible.insert(pid)
                observedOnscreenWindowPIDs.insert(pid)
            }
        }
        var candidatePIDs = windowPIDs
        candidatePIDs.formUnion(applications.map { $0.processIdentifier })
        var hidden: [pid_t] = []
        for pid in candidatePIDs {
            // Look the application up by pid rather than trusting the workspace
            // list: a detached guard session does not always see a headed Unity
            // Editor in `runningApplications`, which left this whole-application
            // hide unreachable for every Unity backend. AppKit's hide is one
            // asynchronous request for all of the application's windows, so it
            // still lands when the application is too busy to answer the
            // per-window accessibility queries below.
            guard let application = NSRunningApplication(processIdentifier: pid) else {
                continue
            }
            observedApplicationPIDs.insert(pid)
            if application.isActive { observedActivePIDs.insert(pid) }
            if !application.isHidden {
                hideAttempts += 1
                if application.hide() {
                    hideSuccesses += 1
                    appKitHideSuccesses += 1
                }
            }
            if application.isHidden {
                hidden.append(pid)
            }
        }
        for pid in candidatePIDs {
            let element = axApplication(pid)
            hideAttempts += 1
            if recordAX(
                AXUIElementSetAttributeValue(
                    element, kAXHiddenAttribute as CFString, kCFBooleanTrue
                )
            ) {
                axHideSuccesses += 1
                hideSuccesses += 1
            }
            var rawWindows: CFTypeRef?
            let windowsError = AXUIElementCopyAttributeValue(
                element, kAXWindowsAttribute as CFString, &rawWindows
            )
            if recordAX(windowsError), let windows = rawWindows as? [AXUIElement] {
                for window in windows {
                    AXUIElementSetMessagingTimeout(window, axMessagingTimeoutSeconds)
                    minimizeAttempts += 1
                    _ = recordAX(
                        AXUIElementSetAttributeValue(
                            window, kAXMinimizedAttribute as CFString, kCFBooleanTrue
                        )
                    )
                    // Unity progress and splash windows can report a successful
                    // AX minimize/hide request while CoreGraphics still marks
                    // them onscreen. Keep the headed renderer alive, but move
                    // only windows owned by the target process group outside
                    // the desktop as a deterministic isolation fallback.
                    var offscreenPoint = CGPoint(x: -32_768, y: -32_768)
                    if let position = AXValueCreate(.cgPoint, &offscreenPoint) {
                        repositionAttempts += 1
                        if recordAX(
                            AXUIElementSetAttributeValue(
                                window, kAXPositionAttribute as CFString, position
                            )
                        ) {
                            repositionSuccesses += 1
                        }
                    }
                }
            }
        }
        lastVisiblePIDs = visible.sorted()
        lastHiddenPIDs = hidden.sorted()
        let nowUnixMs = Int(Date().timeIntervalSince1970 * 1000)
        if visible.isEmpty {
            if let intervalStart = visibleIntervalStartUnixMs {
                maxOnscreenDurationMs = max(maxOnscreenDurationMs, nowUnixMs - intervalStart)
                visibleIntervalStartUnixMs = nil
                if firstHiddenAfterOnscreenUnixMs == nil {
                    firstHiddenAfterOnscreenUnixMs = nowUnixMs
                }
            }
        } else {
            if firstOnscreenUnixMs == nil { firstOnscreenUnixMs = nowUnixMs }
            lastOnscreenUnixMs = nowUnixMs
            if visibleIntervalStartUnixMs == nil { visibleIntervalStartUnixMs = nowUnixMs }
            maxOnscreenDurationMs = max(
                maxOnscreenDurationMs,
                nowUnixMs - (visibleIntervalStartUnixMs ?? nowUnixMs)
            )
        }
        let groupExists = processGroupExists()
        emptyTicks = (!groupExists && applications.isEmpty) ? emptyTicks + 1 : 0
        statusTick += 1
        if statusTick >= 10 || !visible.isEmpty {
            writeStatus(groupExists: groupExists)
            statusTick = 0
        }
    }
    if emptyTicks >= 100 { break }
    usleep(10_000)
}

writeStatus(groupExists: false)
