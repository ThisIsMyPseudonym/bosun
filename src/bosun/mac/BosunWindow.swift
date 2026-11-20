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
    var signingIn = false
    var retryCount = 0
    var retryWork: DispatchWorkItem?
    var downloadDestinations: [ObjectIdentifier: URL] = [:]
    var signedOut = false  // the page showing is the dashboard's 401: ⌘R signs in again rather than reloading it
    let maxRetries = 10

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

    func emitJSON(_ payload: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
           let text = String(data: data, encoding: .utf8) {
            print(text)
        } else {
            print("{\"ok\": false}")
        }
        fflush(stdout)
    }

    // Ask Bosun for a one-time sign-in address (this also starts the dashboard if it isn't running).
    func signIn() {
        retryWork?.cancel()
        retryWork = nil
        if signingIn { return }
        signingIn = true
        status.stringValue = "Starting Bosun…"
        status.isHidden = false
        guard let helper = Bundle.main.url(forResource: "login-url", withExtension: nil) else {
            signingIn = false
            return fail("This copy of the Bosun app is incomplete. Run `uv run bosun install-launcher` in Bosun's folder.")
        }
        DispatchQueue.global().async {
            let task = Process()
            task.executableURL = helper
            let out = Pipe()
            task.standardOutput = out
            task.standardError = FileHandle.nullDevice
            do { try task.run() } catch {
                return DispatchQueue.main.async {
                    self.signingIn = false
                    self.fail("Bosun could not be started: \(error.localizedDescription)")
                }
            }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            let line = String(data: data, encoding: .utf8)?
                .split(separator: "\n").last.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
            DispatchQueue.main.async {
                self.signingIn = false
                guard task.terminationStatus == 0, let url = URL(string: line), self.isLocal(url) else {
                    return self.fail("Bosun didn't start. Details are in its log: ~/Library/Application Support/bosun/bosun.log")
                }
                self.home = url
                self.web.load(URLRequest(url: url))
            }
        }
    }

    func scheduleRetry() {
        if selfTest { return fail("Bosun dashboard did not respond.") }
        guard retryCount < maxRetries else {
            status.stringValue = "Bosun is not responding. Press ⌘R to try again."
            status.isHidden = false
            return
        }
        retryWork?.cancel()
        let delay = min(30.0, 2.0 * pow(1.5, Double(retryCount)))
        retryCount += 1
        status.stringValue = "Reconnecting to Bosun…"
        status.isHidden = false
        let work = DispatchWorkItem { [weak self] in self?.signIn() }
        retryWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    func fail(_ message: String) {
        status.stringValue = message
        status.isHidden = false
        if selfTest {
            emitJSON(["ok": false, "error": message])
            exit(1)
        }
    }

    func isLocal(_ url: URL) -> Bool {
        return url.scheme == "http" && (url.host == "127.0.0.1" || url.host == "localhost")
    }

    func isHome(_ url: URL) -> Bool {
        guard let home = home, isLocal(url) else { return false }
        return url.port == home.port
    }

    func isHomeOrigin(_ origin: WKSecurityOrigin) -> Bool {
        guard let home = home else { return false }
        let expectedPort = home.port ?? 80
        let localHost = origin.host == "127.0.0.1" || origin.host == "localhost"
        return origin.protocol == "http" && localHost && origin.port == expectedPort
    }

    func safeFilename(_ raw: String) -> String {
        let leaf = (raw as NSString).lastPathComponent
        let cleaned = leaf.unicodeScalars.filter { $0.value >= 0x20 && $0.value != 0x7f }.map(String.init).joined()
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "\\", with: "_")
            .replacingOccurrences(of: ":", with: "_")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var trimmed = cleaned
        while trimmed.hasPrefix(".") { trimmed.removeFirst() }
        if trimmed.isEmpty || trimmed == "." || trimmed == ".." { return "bosun-download.json" }
        return trimmed
    }

    // --- navigation: the dashboard stays here, everything else goes to the browser -------------------------
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        let homeBlob = url.scheme == "blob"
            && (URL(string: String(url.absoluteString.dropFirst("blob:".count))).map(isHome) ?? false)
        // only the dashboard's own files are saved: a link elsewhere never lands in Downloads and in Finder
        if action.shouldPerformDownload { return decisionHandler(homeBlob || isHome(url) ? .download : .cancel) }
        if url.absoluteString == "about:blank" { return decisionHandler(.allow) }
        if homeBlob { return decisionHandler(.allow) }
        if isHome(url) { return decisionHandler(.allow) }
        if url.scheme == "https" || url.scheme == "http" { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if navigationResponse.isForMainFrame {
            signedOut = (navigationResponse.response as? HTTPURLResponse)?.statusCode == 401
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, url.scheme == "https" || url.scheme == "http" {  // target=_blank
            if isHome(url) { web.load(action.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        retryCount = 0
        retryWork?.cancel()
        retryWork = nil
        status.isHidden = true
        if selfTest { runSelfTest() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        // the dashboard restarts when it updates itself: sign in again with backoff, don't show an error page
        scheduleRetry()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        scheduleRetry()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        retryCount = 0
        signIn()
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
        guard message.name == "bosunCopy",
              message.frameInfo.isMainFrame,
              isHomeOrigin(message.frameInfo.securityOrigin),
              let url = message.frameInfo.request.url ?? web.url,
              isHome(url),
              let text = message.body as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        guard let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first else {
            return completionHandler(nil)
        }
        let safe = safeFilename(suggestedFilename)
        let root = folder.standardizedFileURL.path
        var dest = folder.appendingPathComponent(safe)
        var n = 1
        while FileManager.default.fileExists(atPath: dest.path) {
            n += 1
            let base = (safe as NSString).deletingPathExtension, ext = (safe as NSString).pathExtension
            dest = folder.appendingPathComponent("\(base) \(n)" + (ext.isEmpty ? "" : ".\(ext)"))
        }
        guard dest.standardizedFileURL.path.hasPrefix(root + "/") else {
            return completionHandler(nil)
        }
        downloadDestinations[ObjectIdentifier(download)] = dest
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        let key = ObjectIdentifier(download)
        if let dest = downloadDestinations.removeValue(forKey: key),
           FileManager.default.fileExists(atPath: dest.path) {
            NSWorkspace.shared.activateFileViewerSelecting([dest])
        } else if let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first {
            NSWorkspace.shared.open(folder)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloadDestinations.removeValue(forKey: ObjectIdentifier(download))
    }

    // --- app behaviour --------------------------------------------------------------------------------------
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { return true }

    @objc func reload(_ sender: Any?) {
        retryCount = 0
        if let current = web.url, isHome(current), !signedOut {
            web.reload()
        } else {
            signIn()
        }
    }

    @objc func zoomIn(_ sender: Any?) { web.pageZoom = min(2.0, web.pageZoom + 0.1) }
    @objc func zoomOut(_ sender: Any?) { web.pageZoom = max(0.6, web.pageZoom - 0.1) }
    @objc func zoomReset(_ sender: Any?) { web.pageZoom = 1.0 }

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
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
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
        viewMenu.addItem(.separator())
        for (title, sel, key) in [("Actual Size", #selector(zoomReset(_:)), "0"),
                                  ("Zoom In", #selector(zoomIn(_:)), "+"),
                                  ("Zoom Out", #selector(zoomOut(_:)), "-")] {
            let item = NSMenuItem(title: title, action: sel, keyEquivalent: key)
            item.target = self
            viewMenu.addItem(item)
        }
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
            case .success(let value):
                guard let text = value as? String,
                      let data = text.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    self.emitJSON(["ok": false, "error": "unexpected self-test output"])
                    exit(1)
                }
                print(text)
                fflush(stdout)
                exit((obj["ok"] as? Bool) == true ? 0 : 1)
            case .failure(let error):
                self.emitJSON(["ok": false, "error": error.localizedDescription])
                exit(1)
            }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(selfTest ? .accessory : .regular)
app.run()
