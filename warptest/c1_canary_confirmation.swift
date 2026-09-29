import AppKit
import Foundation

private let waitMinutes = [5, 10, 20, 30, 45, 60]

private final class SliderController: NSObject {
    private let values: [Int]
    private weak var waitButton: NSButton?

    init(values: [Int], waitButton: NSButton) {
        self.values = values
        self.waitButton = waitButton
    }

    @objc func sliderChanged(_ sender: NSSlider) {
        let index = max(0, min(values.count - 1, Int(sender.doubleValue.rounded())))
        waitButton?.title = "Wait \(values[index]) min"
    }
}

private func emit(action: String, minutes: Int, timedOut: Bool = false) {
    let payload: [String: Any] = [
        "action": action,
        "minutes": minutes,
        "timed_out": timedOut,
    ]
    let data = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

if CommandLine.arguments.contains("--self-test") {
    emit(action: "self-test", minutes: 0)
    exit(EXIT_SUCCESS)
}

let timeoutSeconds = CommandLine.arguments.dropFirst().first.flatMap(Double.init) ?? 120
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let alert = NSAlert()
alert.alertStyle = .warning
alert.messageText = "C1 Canary input isolation check"
alert.informativeText = """
The C1 native canary is about to start.

Do not use the keyboard, mouse, or trackpad during the check, and do not switch the foreground app.

Stop all input right after confirming; the check starts 3 seconds later. If there is no response within 2 minutes, it is confirmed by default.
"""

let confirmButton = alert.addButton(withTitle: "Confirm")
confirmButton.keyEquivalent = "\r"
let waitButton = alert.addButton(withTitle: "Wait 20 min")
let cancelButton = alert.addButton(withTitle: "Cancel")
cancelButton.keyEquivalent = "\u{1b}"

let accessory = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 84))
let prompt = NSTextField(labelWithString: "Choose a delayed start time")
prompt.frame = NSRect(x: 0, y: 64, width: 420, height: 20)
prompt.alignment = .center
prompt.textColor = .secondaryLabelColor
accessory.addSubview(prompt)

let slider = NSSlider(
    value: 2,
    minValue: 0,
    maxValue: Double(waitMinutes.count - 1),
    target: nil,
    action: nil
)
slider.frame = NSRect(x: 18, y: 29, width: 384, height: 30)
slider.numberOfTickMarks = waitMinutes.count
slider.allowsTickMarkValuesOnly = true
slider.tickMarkPosition = .below
slider.isContinuous = true
accessory.addSubview(slider)

for (index, minutes) in waitMinutes.enumerated() {
    let label = NSTextField(labelWithString: "\(minutes)")
    let centerX = 18 + (384 * CGFloat(index) / CGFloat(waitMinutes.count - 1))
    label.frame = NSRect(x: centerX - 18, y: 0, width: 36, height: 18)
    label.alignment = .center
    label.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
    label.textColor = .tertiaryLabelColor
    accessory.addSubview(label)
}

private let sliderController = SliderController(values: waitMinutes, waitButton: waitButton)
slider.target = sliderController
slider.action = #selector(SliderController.sliderChanged(_:))
alert.accessoryView = accessory

var timedOut = false
let timeoutTimer = Timer(timeInterval: timeoutSeconds, repeats: false) { _ in
    timedOut = true
    NSApp.abortModal()
}
RunLoop.current.add(timeoutTimer, forMode: .modalPanel)

app.activate(ignoringOtherApps: true)
let response = alert.runModal()
timeoutTimer.invalidate()

if timedOut {
    emit(action: "confirm", minutes: 0, timedOut: true)
} else if response == .alertFirstButtonReturn {
    emit(action: "confirm", minutes: 0)
} else if response == .alertSecondButtonReturn {
    let index = max(0, min(waitMinutes.count - 1, Int(slider.doubleValue.rounded())))
    emit(action: "wait", minutes: waitMinutes[index])
} else {
    emit(action: "cancel", minutes: 0)
}
