import AppKit
import Darwin
import Foundation

private let corePort: UInt16 = 8000
private let bootstrap = "from operant.cli import app; app()"
private var interrupted = false
private let signalLock = NSLock()
private var signalSources: [DispatchSourceSignal] = []

private func wasInterrupted() -> Bool {
    signalLock.lock()
    defer { signalLock.unlock() }
    return interrupted
}

private func showError(_ detail: String) {
    let alert = NSAlert()
    alert.alertStyle = .critical
    alert.messageText = "Operant 无法启动"
    alert.informativeText = detail
    alert.addButton(withTitle: "确定")
    alert.runModal()
}

private func portAvailable() -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = corePort.bigEndian
    address.sin_addr = in_addr(s_addr: in_addr_t(INADDR_LOOPBACK).bigEndian)
    return withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
        }
    }
}

private func healthy() -> Bool {
    guard let url = URL(string: "http://127.0.0.1:\(corePort)/healthz") else { return false }
    var request = URLRequest(url: url)
    request.timeoutInterval = 0.5
    let semaphore = DispatchSemaphore(value: 0)
    var result = false
    let session = URLSession(configuration: .ephemeral)
    session.dataTask(with: request) { data, response, _ in
        if let response = response as? HTTPURLResponse, response.statusCode == 200,
           let data = data, let body = String(data: data, encoding: .utf8) {
            result = body.contains("\"status\":\"ok\"")
        }
        semaphore.signal()
    }.resume()
    _ = semaphore.wait(timeout: .now() + 0.8)
    session.invalidateAndCancel()
    return result
}

private func stop(_ process: Process?) {
    guard let process = process, process.isRunning else { return }
    kill(process.processIdentifier, SIGTERM)
    for _ in 0..<30 {
        if !process.isRunning { break }
        Thread.sleep(forTimeInterval: 0.1)
    }
    if process.isRunning { kill(process.processIdentifier, SIGKILL) }
    process.waitUntilExit()
}

private func cleanEnvironment(_ dataDirectory: URL) -> [String: String] {
    var environment = ProcessInfo.processInfo.environment
    for key in environment.keys where key.hasPrefix("PYTHON") || key.hasPrefix("UV_") || key.hasPrefix("OPERANT_") {
        environment.removeValue(forKey: key)
    }
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    environment["OPERANT_DB_PATH"] = dataDirectory.appendingPathComponent("core.sqlite3").path
    // The Core uses -B; a plugin launched through sys.executable may not.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment
}

private func run() throws {
    let executable = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
    let contents = executable.deletingLastPathComponent().deletingLastPathComponent()
    let resources = contents.appendingPathComponent("Resources")
    let pythonRoot = resources.appendingPathComponent("python")
    let python = pythonRoot.appendingPathComponent("bin/python3.13")
    let packages = pythonRoot.appendingPathComponent("lib/python3.13/site-packages")
    let desktop = contents.appendingPathComponent("MacOS/operant-desktop")
    for path in [python, packages, desktop] where !FileManager.default.fileExists(atPath: path.path) {
        throw NSError(domain: "Operant", code: 1, userInfo: [NSLocalizedDescriptionKey: "应用文件不完整：\(path.lastPathComponent)。请重新安装 DMG。"])
    }

    let dataDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Operant Beta 2", isDirectory: true)
    try FileManager.default.createDirectory(
        at: dataDirectory, withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    let lockURL = dataDirectory.appendingPathComponent("launcher.lock")
    let lockFD = open(lockURL.path, O_CREAT | O_RDWR, 0o600)
    guard lockFD >= 0 else { throw NSError(domain: "Operant", code: 2, userInfo: [NSLocalizedDescriptionKey: "无法创建启动锁。"] ) }
    defer { close(lockFD) }
    guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
        throw NSError(domain: "Operant", code: 3, userInfo: [NSLocalizedDescriptionKey: "Operant Beta 2 已在运行。"])
    }
    guard portAvailable() else {
        throw NSError(domain: "Operant", code: 4, userInfo: [NSLocalizedDescriptionKey: "本机 8000 端口已被其他服务占用。请先退出该服务或旧版 Operant，再启动 Beta 2；本次未连接该服务，也未访问旧数据库。"])
    }

    let logURL = dataDirectory.appendingPathComponent("core.log")
    if !FileManager.default.fileExists(atPath: logURL.path) {
        FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
    }
    let log = try FileHandle(forWritingTo: logURL)
    try log.seekToEnd()
    defer { try? log.close() }

    let core = Process()
    core.executableURL = python
    core.arguments = ["-I", "-B", "-c", bootstrap, "serve", "--host", "127.0.0.1", "--port", String(corePort), "--desktop"]
    core.currentDirectoryURL = dataDirectory
    core.environment = cleanEnvironment(dataDirectory)
    core.standardInput = FileHandle.nullDevice
    core.standardOutput = log
    core.standardError = log
    try core.run()
    defer { stop(core) }

    let deadline = Date().addingTimeInterval(20)
    while Date() < deadline && !wasInterrupted() {
        if !core.isRunning {
            throw NSError(domain: "Operant", code: 5, userInfo: [NSLocalizedDescriptionKey: "内置 Core 启动失败。请查看 \(logURL.path)。"])
        }
        if healthy() { break }
        Thread.sleep(forTimeInterval: 0.2)
    }
    guard healthy() else {
        throw NSError(domain: "Operant", code: 6, userInfo: [NSLocalizedDescriptionKey: "内置 Core 未在 20 秒内就绪。请查看 \(logURL.path)。"])
    }

    let ui = Process()
    ui.executableURL = desktop
    ui.currentDirectoryURL = dataDirectory
    ui.environment = cleanEnvironment(dataDirectory)
    ui.standardInput = FileHandle.nullDevice
    try ui.run()
    defer { stop(ui) }
    NSRunningApplication(processIdentifier: ui.processIdentifier)?.activate(options: [.activateIgnoringOtherApps])
    while !wasInterrupted() && ui.isRunning && core.isRunning {
        Thread.sleep(forTimeInterval: 0.15)
    }
    if !wasInterrupted() && ui.isRunning && !core.isRunning {
        throw NSError(domain: "Operant", code: 7, userInfo: [NSLocalizedDescriptionKey: "内置 Core 意外退出。请查看 \(logURL.path)。"])
    }
    if !wasInterrupted() && !ui.isRunning && ui.terminationStatus != 0 {
        throw NSError(domain: "Operant", code: 8, userInfo: [NSLocalizedDescriptionKey: "桌面窗口意外退出（状态 \(ui.terminationStatus)）。"])
    }
}

for number in [SIGTERM, SIGINT] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .global(qos: .userInitiated))
    source.setEventHandler {
        signalLock.lock()
        interrupted = true
        signalLock.unlock()
    }
    source.resume()
    signalSources.append(source)
}

do {
    try run()
} catch {
    showError(error.localizedDescription)
    exit(1)
}
