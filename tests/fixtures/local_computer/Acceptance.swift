import Cocoa

final class AcceptanceDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var status: NSTextField!

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(
            contentRect: NSRect(x: 300, y: 300, width: 420, height: 180),
            styleMask: [.titled, .closable], backing: .buffered, defer: false
        )
        window.title = "Operant Acceptance"
        status = NSTextField(labelWithString: "Pending")
        status.frame = NSRect(x: 24, y: 105, width: 350, height: 30)
        let button = NSButton(title: "Run Check", target: self, action: #selector(runCheck))
        button.frame = NSRect(x: 24, y: 35, width: 130, height: 36)
        window.contentView?.addSubview(status)
        window.contentView?.addSubview(button)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func runCheck() {
        status.stringValue = "Clicked"
        window.title = "Operant Acceptance Clicked"
    }
}

let app = NSApplication.shared
let delegate = AcceptanceDelegate()
app.delegate = delegate
app.run()
