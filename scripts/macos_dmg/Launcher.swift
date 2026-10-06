import AppKit
import Darwin
import Foundation

private let corePort: UInt16 = 8000

private func portAvailable() -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    var reuse: Int32 = 1
    let optionSet = withUnsafePointer(to: &reuse) {
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, $0, socklen_t(MemoryLayout<Int32>.size))
    }
    guard optionSet == 0 else { return false }
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = corePort.bigEndian
    address.sin_addr = in_addr(s_addr: in_addr_t(INADDR_LOOPBACK).bigEndian)
    return withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0 && listen(fd, 1) == 0
        }
    }
}

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

    let dataDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Operant Beta 2", isDirectory: true)
    try FileManager.default.createDirectory(
        at: dataDirectory, withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    let logFD = open(dataDirectory.appendingPathComponent("core.log").path, O_CREAT | O_APPEND | O_WRONLY, 0o600)
    guard logFD >= 0 else { throw failure(2, "无法创建 Core 日志。") }
    close(logFD)
    let lockFD = open(dataDirectory.appendingPathComponent("launcher.lock").path, O_CREAT | O_RDWR, 0o600)
    guard lockFD >= 0 else { throw failure(2, "无法创建启动锁。") }
    guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
        close(lockFD)
        throw failure(3, "Operant Beta 2 已在运行。")
    }
    // Keep this descriptor open across exec so a second launch cannot race
    // the Tauri process before its Core has opened port 8000.
    guard portAvailable() else {
        throw failure(4, "本机 8000 端口已被其他服务占用。请先退出该服务或旧版 Operant，再启动 Beta 2；本次未连接该服务，也未访问旧数据库。")
    }

    for key in ProcessInfo.processInfo.environment.keys
    where key.hasPrefix("PYTHON") || key.hasPrefix("UV_") || key.hasPrefix("OPERANT_") {
        unsetenv(key)
    }
    setenv("PATH", "\(wrapperDirectory.path):/usr/bin:/bin:/usr/sbin:/sbin", 1)
    setenv("OPERANT_DB_PATH", dataDirectory.appendingPathComponent("core.sqlite3").path, 1)
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1)
    guard chdir(dataDirectory.path) == 0 else { throw failure(5, "无法进入 Beta 2 私有数据目录。") }

    // Replace the LaunchServices process with the accepted Tauri binary.
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
