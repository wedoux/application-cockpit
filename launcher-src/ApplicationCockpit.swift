// Native launcher for Application Cockpit's .app bundle.
//
// Why this exists at all: a plain shell script as a bundle's
// CFBundleExecutable never registers with macOS as a real Application (no
// AppKit, no NSApplication, no activation policy) — confirmed directly via
// `lsappinfo`/System Events while a shell-script version of this launcher
// was running: it showed up nowhere as a foreground app, no Dock dot, no
// Cmd+Tab entry, no menu bar. This file is the minimal AppKit shell needed
// to get all of that for real, plus a standard Quit that actually stops
// the Flask server instead of leaking it as an orphaned background process.
//
// Build: see build.sh in this same folder. No Xcode project needed —
// swiftc (part of the Xcode Command Line Tools) compiles this directly.

import Cocoa

let PORT = 8766
let SERVER_URL = "http://127.0.0.1:\(PORT)/"

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var serverProcess: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenuBar()
        startServer()
    }

    // Standard AppKit hook: macOS calls this when the user clicks the Dock
    // icon of an app that's already running, instead of launching a second
    // instance (that suppression is automatic for a regular NSApplication —
    // the shell-script version needed its own curl-based "already running?"
    // check specifically because it never got this for free).
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openBrowser()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
        }
        return .terminateNow
    }

    private func projectDir() -> URL {
        // This binary lives at .../Application Cockpit.app/Contents/MacOS/ApplicationCockpit
        // — walk up to the folder that contains the .app itself, exactly
        // like the shell-script version did, for the same reason (works
        // whether double-clicked in a cloned repo or launched via a
        // ~/Applications symlink, with no hardcoded personal path).
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        return exe
            .deletingLastPathComponent()  // MacOS
            .deletingLastPathComponent()  // Contents
            .deletingLastPathComponent()  // Application Cockpit.app
            .deletingLastPathComponent()  // the project folder
    }

    private func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Application Cockpit"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
        NSApp.terminate(nil)
    }

    private func startServer() {
        let toolDir = projectDir()
        let appPy = toolDir.appendingPathComponent("app.py")
        guard FileManager.default.fileExists(atPath: appPy.path) else {
            fail("app.py not found next to this app — is it still inside the project folder?")
            return
        }

        let venvPython = toolDir.appendingPathComponent(".venv/bin/python3")
        let pythonPath = FileManager.default.isExecutableFile(atPath: venvPython.path)
            ? venvPython.path : "/usr/bin/python3"

        let task = Process()
        task.executableURL = URL(fileURLWithPath: pythonPath)
        task.arguments = [appPy.path]
        task.currentDirectoryURL = toolDir

        // No visible terminal, so keep a log — same convention as the
        // shell-script version, same file, same .gitignore entry.
        let logPath = toolDir.appendingPathComponent(".launcher.log").path
        FileManager.default.createFile(atPath: logPath, contents: nil)
        let logHandle = FileHandle(forWritingAtPath: logPath)
        task.standardOutput = logHandle
        task.standardError = logHandle

        do {
            try task.run()
            serverProcess = task
        } catch {
            fail("Failed to start app.py: \(error.localizedDescription)")
        }
    }

    private func openBrowser() {
        if let url = URL(string: SERVER_URL) {
            NSWorkspace.shared.open(url)
        }
    }

    private func buildMenuBar() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenuItem.submenu = appMenu

        appMenu.addItem(withTitle: "About Application Cockpit", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Open in Browser", action: #selector(openBrowserAction), keyEquivalent: "o")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(
            withTitle: "Quit Application Cockpit",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )

        NSApp.mainMenu = mainMenu
    }

    @objc private func openBrowserAction() {
        openBrowser()
    }

    @objc private func showAbout() {
        let alert = NSAlert()
        alert.messageText = "Application Cockpit"
        alert.informativeText = "Running at \(SERVER_URL)"
        alert.runModal()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
