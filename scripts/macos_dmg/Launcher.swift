import AppKit
import Darwin
import Foundation

private func failure(_ code: Int, _ message: String) -> NSError {
    NSError(domain: "Operant", code: code, userInfo: [NSLocalizedDescriptionKey: message])
}

private func start() throws {
    let executable = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
    let contents = executable.deletingLastPathComponent().deletingLastPathComponent()
    let pythonRoot = contents.appendingPathComponent("Resources/python")
    let desktop = contents.appendingPathComponent("MacOS/operant-desktop")
    let wrapperDirectory = contents.appendingPathComponent("Resources/bin")
    for path in [
        pythonRoot.appendingPathComponent("bin/python3.13"),
        pythonRoot.appendingPathComponent("lib/python3.13/site-packages/operant"),
        desktop,
        wrapperDirectory.appendingPathComponent("operant"),
    ] where !FileManager.default.fileExists(atPath: path.path) {
        throw failure(1, "应用文件不完整：\(path.lastPathComponent)。请重新安装 DMG。")
    }

    let info = NSDictionary(contentsOf: contents.appendingPathComponent("Info.plist"))
    let candidateID = info?["OperantCandidateID"] as? String
    let identifier = info?["CFBundleIdentifier"] as? String
    let productIdentifier = info?["OperantProductBundleIdentifier"] as? String
    guard let identifier = identifier, let productIdentifier = productIdentifier,
          !productIdentifier.isEmpty else {
        throw failure(2, "应用标识无效。")
    }
    if let candidateID = candidateID {
        guard identifier == "\(productIdentifier).candidate.\(candidateID)",
              candidateID.range(of: "^[a-z0-9]+(?:-[a-z0-9]+)*$", options: .regularExpression) != nil else {
            throw failure(2, "候选应用标识无效。")
        }
        let dataDirectory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Operant Candidates", isDirectory: true)
            .appendingPathComponent(identifier, isDirectory: true)
        try FileManager.default.createDirectory(
            at: dataDirectory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        var metadata = stat()
        guard lstat(dataDirectory.path, &metadata) == 0,
              metadata.st_uid == getuid(),
              (metadata.st_mode & S_IFMT) == S_IFDIR,
              (metadata.st_mode & 0o077) == 0 else {
            throw failure(2, "候选数据目录必须由当前用户拥有，且仅当前用户可访问。")
        }
        let lockFD = open(dataDirectory.appendingPathComponent("launcher.lock").path, O_CREAT | O_RDWR, 0o600)
        guard lockFD >= 0 else { throw failure(2, "无法创建启动锁。") }
        guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            close(lockFD)
            throw failure(3, "此 Operant 候选版已在运行。")
        }
        for key in ProcessInfo.processInfo.environment.keys where key.hasPrefix("OPERANT_") {
            unsetenv(key)
        }
        setenv("OPERANT_DB_PATH", dataDirectory.appendingPathComponent("operant.sqlite3").path, 1)
        guard chdir(dataDirectory.path) == 0 else { throw failure(5, "无法进入候选数据目录。") }
    } else if identifier != productIdentifier {
        throw failure(2, "应用标识与产品配置不一致。")
    }
    // Tauri ManagedCore owns port checks, Core authentication and child cleanup.
    for key in ProcessInfo.processInfo.environment.keys where key.hasPrefix("PYTHON") || key.hasPrefix("UV_") {
        unsetenv(key)
    }
    setenv("PATH", "\(wrapperDirectory.path):/usr/bin:/bin:/usr/sbin:/sbin", 1)
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1)

    // Replace the LaunchServices process with the current Tauri binary.
    // Its window keeps the original App identity and Tauri manages Core.
    let args: [UnsafeMutablePointer<CChar>?] = [strdup(desktop.path), nil]
    defer { free(args[0]) }
    args.withUnsafeBufferPointer { buffer in
        _ = Darwin.execv(desktop.path, UnsafeMutablePointer(mutating: buffer.baseAddress!))
    }
    throw failure(6, "无法运行桌面窗口：\(String(cString: strerror(errno)))")
}

private final class ErrorDelegate: NSObject, NSApplicationDelegate {
    let detail: String
    init(detail: String) { self.detail = detail }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Operant 无法启动"
        alert.informativeText = detail
        alert.addButton(withTitle: "确定")
        alert.runModal()
        NSApp.terminate(nil)
    }
}

do {
    try start()
} catch {
    let application = NSApplication.shared
    let delegate = ErrorDelegate(detail: error.localizedDescription)
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    application.run()
    exit(1)
}
