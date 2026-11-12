// Bosun's own window on macOS: a small native app showing the dashboard, so Bosun has its own Dock icon,
// Cmd+Tab entry and window, not a tab or app window that belongs to a browser.
//
// It holds no logic: the dashboard is the local web page. On launch it runs the helper next to it
// (Contents/Resources/login-url, which is `bosun open --print-url`): that starts the dashboard if needed and
// prints a one-time sign-in address, which this window loads. Only the dashboard's own address is shown
// here; any other link opens in your browser.
//
// Built by `bosun install-launcher` with swiftc when the Xcode command line tools are installed.
import Cocoa
import WebKit

let selfTest = ProcessInfo.processInfo.environment["BOSUN_WINDOW_SELFTEST"] == "1"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler,
                         WKDownloadDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var status: NSTextField!
    var home: URL?

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        // The page copies prompts and commands to the clipboard. WebKit only allows that on some origins, so route
        // it through the app, which can always write the pasteboard.
        let bridge = """
        (() => { const send = (t) => window.webkit.messageHandlers.bosunCopy.postMessage(String(t));
          const clip = { writeText: (t) => { send(t); return Promise.resolve(); } };
          try { Object.defineProperty(navigator, 'clipboard', { value: clip, configurable: true }); } catch (e) {} })();
        """
        config.userContentController.addUserScript(
            WKUserScript(source: bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController.add(self, name: "bosunCopy")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.autoresizingMask = [.width, .height]

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Bosun"
        window.minSize = NSSize(width: 720, height: 480)
        window.center()
        window.setFrameAutosaveName("BosunMain")
        let content = NSView(frame: window.contentLayoutRect)
        web.frame = content.bounds
        content.addSubview(web)
        status = NSTextField(labelWithString: "Starting Bosun…")
        status.font = NSFont.systemFont(ofSize: 15)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        status.frame = NSRect(x: 0, y: content.bounds.midY - 12, width: content.bounds.width, height: 24)
        status.autoresizingMask = [.width, .minYMargin, .maxYMargin]
        content.addSubview(status)
        window.contentView = content
        if !selfTest {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        signIn()
    }

    // Ask Bosun for a one-time sign-in address (this also starts the dashboard if it isn't running).
    func signIn() {
        status.stringValue = "Starting Bosun…"
        status.isHidden = false
        guard let helper = Bundle.main.url(forResource: "login-url", withExtension: nil) else {
            return fail("This copy of the Bosun app is incomplete. Run `uv run bosun install-launcher` in Bosun's folder.")
        }
        DispatchQueue.global().async {
            let task = Process()
            task.executableURL = helper
            let out = Pipe()
            task.standardOutput = out
            task.standardError = FileHandle.nullDevice
            do { try task.run() } catch {
                return DispatchQueue.main.async { self.fail("Bosun could not be started: \(error.localizedDescription)") }
            }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            let line = String(data: data, encoding: .utf8)?
                .split(separator: "\n").last.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
            DispatchQueue.main.async {
                guard task.terminationStatus == 0, let url = URL(string: line), self.isLocal(url) else {
                    return self.fail("Bosun didn't start. Details are in its log: ~/Library/Application Support/bosun/bosun.log")
                }
                self.home = url
                self.web.load(URLRequest(url: url))
            }
        }
    }

    func fail(_ message: String) {
        status.stringValue = message
        status.isHidden = false
        if selfTest { print("{\"ok\": false, \"error\": \"\(message)\"}"); exit(1) }
    }

    func isLocal(_ url: URL) -> Bool {
        return url.scheme == "http" && (url.host == "127.0.0.1" || url.host == "localhost")
    }

    // --- navigation: the dashboard stays here, everything else goes to the browser -------------------------
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if action.shouldPerformDownload { return decisionHandler(.download) }
        if url.scheme == "blob" || url.scheme == "about" { return decisionHandler(.allow) }
        if isLocal(url), url.port == home?.port { return decisionHandler(.allow) }
        if url.scheme == "https" || url.scheme == "http" { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, url.scheme == "https" || url.scheme == "http" {  // target=_blank
            if isLocal(url), url.port == home?.port { web.load(action.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        status.isHidden = true
        if selfTest { runSelfTest() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        // the dashboard restarts when it updates itself: sign in again shortly, don't show an error page
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.signIn() }
    }

    // --- dialogs the page uses (confirm before a restart or a push, alerts) --------------------------------
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    // --- clipboard and downloads ---------------------------------------------------------------------------
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "bosunCopy", let text = message.body as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first!
        let safe = suggestedFilename.replacingOccurrences(of: "/", with: "_")
        var dest = folder.appendingPathComponent(safe)
        var n = 1
        while FileManager.default.fileExists(atPath: dest.path) {
            n += 1
            let base = (safe as NSString).deletingPathExtension, ext = (safe as NSString).pathExtension
            dest = folder.appendingPathComponent("\(base) \(n)" + (ext.isEmpty ? "" : ".\(ext)"))
        }
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first {
            NSWorkspace.shared.open(folder)
        }
    }

    // --- app behaviour --------------------------------------------------------------------------------------
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { return true }

    @objc func reload(_ sender: Any?) { signIn() }

    func buildMenu() {
        let main = NSMenu()
        let app = NSMenuItem(); main.addItem(app)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide Bosun", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Bosun", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        app.submenu = appMenu
        let edit = NSMenuItem(); main.addItem(edit)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.submenu = editMenu
        let view = NSMenuItem(); main.addItem(view)
        let viewMenu = NSMenu(title: "View")
        let reloadItem = NSMenuItem(title: "Reload", action: #selector(reload(_:)), keyEquivalent: "r")
        reloadItem.target = self
        viewMenu.addItem(reloadItem)
        view.submenu = viewMenu
        let win = NSMenuItem(); main.addItem(win)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.submenu = winMenu
        NSApp.mainMenu = main
        NSApp.windowsMenu = winMenu
    }

    // BOSUN_WINDOW_SELFTEST=1: load the dashboard without showing a window, report what the page sees, exit.
    func runSelfTest() {
        let js = """
        (async () => { const r = await fetch('/api/state'); await new Promise((ok) => setTimeout(ok, 1500));
          return JSON.stringify({ ok: r.status === 200, state: r.status, title: document.title,
            nav: document.querySelectorAll('#nav a').length, clipboard: typeof navigator.clipboard.writeText }); })()
        """
        web.callAsyncJavaScript("return await \(js)", arguments: [:], in: nil, in: .page) { result in
            switch result {
            case .success(let value): print(value as? String ?? "{\"ok\": false}")
            case .failure(let error): print("{\"ok\": false, \"error\": \"\(error.localizedDescription)\"}")
            }
            exit(0)
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(selfTest ? .accessory : .regular)
app.run()
