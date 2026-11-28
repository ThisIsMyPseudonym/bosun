// Bosun Helper: a small LSUIElement native helper (com.bosun.helper) that holds macOS Accessibility
// separately from Python. Every request and reply travels over a private 0700 Unix domain socket (--sock)
// and is authenticated with HMAC-SHA256 using a per-launch key from the environment or an inherited fd.
import ApplicationServices
import Cocoa
import CryptoKit
import Darwin

let maxRequestBytes = 256 * 1024
let maxReplyBytes = 16 * 1024 * 1024
let requestTtlSeconds: Double = 30.0
let maxClockSkewSeconds: Double = 5.0
let minKeyLength = 32
let axTimeoutSeconds: Float = 2.0
let allowedVerbs: Set<String> = ["trusted", "state", "tree", "press", "key", "menu", "quit"]

// --- key loading & canonical JSON / HMAC --------------------------------------------------------------------

func loadLaunchKey() -> String? {
    let env = ProcessInfo.processInfo.environment
    if let raw = env["BOSUN_HELPER_KEY"] {
        unsetenv("BOSUN_HELPER_KEY")
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return key.count >= minKeyLength ? key : nil
    }
    if let fdStr = env["BOSUN_HELPER_KEY_FD"], let fd = Int32(fdStr), fd >= 3 {
        unsetenv("BOSUN_HELPER_KEY_FD")
        var buf = [UInt8](repeating: 0, count: 4096)
        var data = Data()
        while data.count < 4096 {
            let n = Darwin.read(fd, &buf, min(buf.count, 4096 - data.count))
            if n > 0 { data.append(contentsOf: buf[0..<n]) }
            else if n == 0 { break }
            else if errno == EINTR { continue }
            else { break }
        }
        Darwin.close(fd)
        let key = (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return key.count >= minKeyLength ? key : nil
    }
    return nil
}

func escapeJSONString(_ s: String) -> String {
    var out = "\""
    for scalar in s.unicodeScalars {
        switch scalar.value {
        case 0x22: out += "\\\""
        case 0x5C: out += "\\\\"
        case 0x08: out += "\\b"
        case 0x0C: out += "\\f"
        case 0x0A: out += "\\n"
        case 0x0D: out += "\\r"
        case 0x09: out += "\\t"
        case 0x00..<0x20: out += String(format: "\\u%04x", scalar.value)
        default: out.unicodeScalars.append(scalar)
        }
    }
    out += "\""
    return out
}

func canonicalJSON(_ value: Any) -> String? {
    if value is NSNull { return "null" }
    if let s = value as? String { return escapeJSONString(s) }
    if let num = value as? NSNumber {
        if CFGetTypeID(num) == CFBooleanGetTypeID() { return num.boolValue ? "true" : "false" }
        let objCType = String(cString: num.objCType)
        if objCType == "d" || objCType == "f" {
            let d = num.doubleValue
            if !d.isFinite { return nil }
            return d.rounded(.towardZero) == d && abs(d) < 1e15 ? String(format: "%.1f", d) : String(d)
        }
        return String(num.int64Value)
    }
    if let arr = value as? [Any] {
        var parts: [String] = []
        parts.reserveCapacity(arr.count)
        for item in arr {
            guard let enc = canonicalJSON(item) else { return nil }
            parts.append(enc)
        }
        return "[" + parts.joined(separator: ",") + "]"
    }
    if let dict = value as? [String: Any] {
        var parts: [String] = []
        parts.reserveCapacity(dict.count)
        for k in dict.keys.sorted() {
            guard let v = dict[k], let enc = canonicalJSON(v) else { return nil }
            parts.append(escapeJSONString(k) + ":" + enc)
        }
        return "{" + parts.joined(separator: ",") + "}"
    }
    return nil
}

func sha256Hex(_ text: String) -> String {
    SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
}

func hmacHex(key: String, message: String) -> String {
    let mac = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: SymmetricKey(data: Data(key.utf8)))
    return mac.map { String(format: "%02x", $0) }.joined()
}

func constantTimeEquals(_ a: String, _ b: String) -> Bool {
    let ab = Array(a.utf8), bb = Array(b.utf8)
    guard ab.count == bb.count else { return false }
    var diff: UInt8 = 0
    for i in 0..<ab.count { diff |= ab[i] ^ bb[i] }
    return diff == 0
}

struct VerifiedRequest {
    let verb: String
    let bundleId: String
    let bundlePath: String
    let params: [String: Any]
    let nonce: String
}

func verifyRequest(key: String, raw: [String: Any]) -> VerifiedRequest? {
    guard let verb = raw["verb"] as? String, allowedVerbs.contains(verb),
          let bundleId = raw["bundle_id"] as? String,
          let bundlePath = raw["bundle_path"] as? String,
          let params = raw["params"] as? [String: Any],
          let tsNum = raw["ts"] as? NSNumber, CFGetTypeID(tsNum) != CFBooleanGetTypeID(),
          let nonce = raw["nonce"] as? String, !nonce.isEmpty,
          let mac = raw["mac"] as? String, !mac.isEmpty else { return nil }
    let ts = tsNum.int64Value
    let age = Date().timeIntervalSince1970 - Double(ts)
    guard age >= -maxClockSkewSeconds && age <= requestTtlSeconds else { return nil }
    guard let paramsJSON = canonicalJSON(params) else { return nil }
    let msg = "helper-req|\(ts)|\(nonce)|\(verb)|\(bundleId)|\(bundlePath)|\(sha256Hex(paramsJSON))"
    guard constantTimeEquals(mac, hmacHex(key: key, message: msg)) else { return nil }
    return VerifiedRequest(verb: verb, bundleId: bundleId, bundlePath: bundlePath, params: params, nonce: nonce)
}

func signReply(key: String, nonce: String, payload: [String: Any]) -> Data? {
    guard let payloadJSON = canonicalJSON(payload) else { return nil }
    let mac = hmacHex(key: key, message: "helper-reply|\(nonce)|\(sha256Hex(payloadJSON))")
    let envelope: [String: Any] = ["nonce": nonce, "payload": payload, "mac": mac]
    guard let text = canonicalJSON(envelope) else { return nil }
    return Data(text.utf8)
}

// --- Unix domain socket IPC with peer-credential verification -----------------------------------------------

func connectSocket(_ path: String) -> Int32? {
    let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    var tv = timeval(tv_sec: 15, tv_usec: 0)
    _ = withUnsafePointer(to: &tv) {
        Darwin.setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, $0, socklen_t(MemoryLayout<timeval>.size))
    }
    _ = withUnsafePointer(to: &tv) {
        Darwin.setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, $0, socklen_t(MemoryLayout<timeval>.size))
    }
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(path.utf8)
    let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
    guard !pathBytes.isEmpty, pathBytes.count < maxLen else { Darwin.close(fd); return nil }
    withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
        ptr.withMemoryRebound(to: UInt8.self, capacity: maxLen) { dst in
            for (i, b) in pathBytes.enumerated() { dst[i] = b }
            dst[pathBytes.count] = 0
        }
    }
    let addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
    let rc = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, addrLen) }
    }
    guard rc == 0 else { Darwin.close(fd); return nil }
    var peerUID: uid_t = 0
    var peerGID: gid_t = 0
    guard getpeereid(fd, &peerUID, &peerGID) == 0, peerUID == geteuid() else {
        Darwin.close(fd)
        return nil
    }
    return fd
}

func readExact(fd: Int32, count: Int) -> Data? {
    var out = Data(capacity: count)
    var buf = [UInt8](repeating: 0, count: min(65536, max(count, 1)))
    while out.count < count {
        let want = min(buf.count, count - out.count)
        let n = Darwin.read(fd, &buf, want)
        if n > 0 { out.append(contentsOf: buf[0..<n]) }
        else if n == 0 { return nil }
        else if errno == EINTR { continue }
        else { return nil }
    }
    return out
}

func readFrame(fd: Int32, maxBytes: Int) -> Data? {
    guard let hdr = readExact(fd: fd, count: 4) else { return nil }
    let length = Int(hdr[0]) << 24 | Int(hdr[1]) << 16 | Int(hdr[2]) << 8 | Int(hdr[3])
    guard length > 0 && length <= maxBytes else { return nil }
    return readExact(fd: fd, count: length)
}

func writeFrame(fd: Int32, data: Data, maxBytes: Int) -> Bool {
    guard !data.isEmpty && data.count <= maxBytes else { return false }
    var frame = Data(capacity: 4 + data.count)
    var beLen = UInt32(data.count).bigEndian
    withUnsafeBytes(of: &beLen) { frame.append(contentsOf: $0) }
    frame.append(data)
    return frame.withUnsafeBytes { raw -> Bool in
        guard let base = raw.baseAddress else { return false }
        var sent = 0
        while sent < frame.count {
            let n = Darwin.write(fd, base.advanced(by: sent), frame.count - sent)
            if n > 0 { sent += n }
            else if n < 0 && errno == EINTR { continue }
            else { return false }
        }
        return true
    }
}

// --- macOS session, target bundle, and Accessibility helpers ------------------------------------------------

func isScreenLocked() -> Bool {
    guard let info = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    if (info["CGSSessionScreenIsLocked"] as? Bool) == true { return true }
    if (info[kCGSessionOnConsoleKey as String] as? Bool) == false { return true }
    return false
}

func isSecurityAgentActive() -> Bool {
    let ids: Set<String> = ["com.apple.SecurityAgent", "com.apple.authorizationhost"]
    let pids = Set(NSWorkspace.shared.runningApplications.filter {
        guard let bid = $0.bundleIdentifier else { return false }
        return ids.contains(bid)
    }.map { $0.processIdentifier })
    guard !pids.isEmpty else { return false }
    let wins = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    return wins.contains { ($0[kCGWindowOwnerPID as String] as? pid_t).map(pids.contains) == true }
}

func targetApps(bundleId: String, bundlePath: String) -> [NSRunningApplication] {
    guard !bundleId.isEmpty, bundlePath.hasPrefix("/"), !bundlePath.contains("/../") else { return [] }
    let std = URL(fileURLWithPath: bundlePath).standardizedFileURL.path
    let prefix = std.hasSuffix("/") ? std : std + "/"
    return NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).filter { app in
        guard let exe = app.executableURL?.standardizedFileURL.path else { return false }
        return exe.hasPrefix(prefix)
    }
}

func makeAxApp(pid: pid_t, bundlePath: String) -> AXUIElement {
    let el = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(el, axTimeoutSeconds)
    let electron = bundlePath + "/Contents/Frameworks/Electron Framework.framework"
    if FileManager.default.fileExists(atPath: electron) {
        AXUIElementSetAttributeValue(el, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    }
    return el
}

func axCopy(_ el: AXUIElement, _ attr: String) -> (AXError, AnyObject?) {
    var out: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(el, attr as CFString, &out)
    return (err, out)
}

func axString(_ el: AXUIElement, _ attr: String) -> String {
    let (_, val) = axCopy(el, attr)
    return val as? String ?? ""
}

func axBool(_ el: AXUIElement, _ attr: String, defaultVal: Bool = true) -> Bool {
    let (_, val) = axCopy(el, attr)
    return (val as? NSNumber)?.boolValue ?? defaultVal
}

func axActions(_ el: AXUIElement) -> [String] {
    var ref: CFArray?
    guard AXUIElementCopyActionNames(el, &ref) == .success, let arr = ref as? [String] else { return [] }
    return arr
}

func axChildren(_ el: AXUIElement) -> [AXUIElement] {
    let (_, val) = axCopy(el, kAXChildrenAttribute)
    return val as? [AXUIElement] ?? []
}

func axSize(_ el: AXUIElement) -> [Int] {
    let (_, val) = axCopy(el, kAXSizeAttribute)
    guard let axVal = val, CFGetTypeID(axVal) == AXValueGetTypeID() else { return [0, 0] }
    var sz = CGSize.zero
    return AXValueGetValue(axVal as! AXValue, .cgSize, &sz) ? [Int(sz.width), Int(sz.height)] : [0, 0]
}

func serializeNode(_ el: AXUIElement, depth: Int, maxDepth: Int, budget: inout Int, path: [Int]) -> [String: Any]? {
    guard budget > 0 else { return nil }
    budget -= 1
    let role = axString(el, kAXRoleAttribute)
    let title = axString(el, kAXTitleAttribute)
    let desc = axString(el, kAXDescriptionAttribute)
    let subrole = axString(el, kAXSubroleAttribute)
    // Never read values from password fields.
    let value = (role == "AXSecureTextField") ? "" : axString(el, kAXValueAttribute)
    var childrenOut: [[String: Any]] = []
    if depth < maxDepth && budget > 0 {
        for (idx, child) in axChildren(el).enumerated() {
            guard budget > 0 else { break }
            if let serialized = serializeNode(child, depth: depth + 1, maxDepth: maxDepth,
                                              budget: &budget, path: path + [idx]) {
                childrenOut.append(serialized)
            }
        }
    }
    return [
        "role": role, "subrole": subrole, "title": title.isEmpty ? desc : title,
        "value": value, "enabled": axBool(el, kAXEnabledAttribute), "actions": axActions(el),
        "path": path, "children": childrenOut,
    ]
}

// --- verb handlers ------------------------------------------------------------------------------------------

func handleTrusted(_ params: [String: Any]) -> [String: Any] {
    let prompt = (params["prompt"] as? NSNumber).map { CFGetTypeID($0) == CFBooleanGetTypeID() && $0.boolValue } ?? false
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary
    return ["ok": true, "trusted": AXIsProcessTrustedWithOptions(opts)]
}

func handleState(_ req: VerifiedRequest) -> [String: Any] {
    let apps = targetApps(bundleId: req.bundleId, bundlePath: req.bundlePath)
    let pids = apps.map { Int($0.processIdentifier) }
    var windowsOut: [[String: Any]] = []
    var hasModal = false
    for app in apps {
        let axApp = makeAxApp(pid: app.processIdentifier, bundlePath: req.bundlePath)
        let (err, rawWins) = axCopy(axApp, kAXWindowsAttribute)
        if err == .cannotComplete { return ["ok": false, "error": "timeout", "message": "App did not respond"] }
        for win in (rawWins as? [AXUIElement] ?? []) {
            let role = axString(win, kAXRoleAttribute)
            let subrole = axString(win, kAXSubroleAttribute)
            if role == "AXSheet" || role == "AXDialog" || subrole == "AXDialog" || subrole == "AXSheet" {
                hasModal = true
            }
            windowsOut.append(["title": axString(win, kAXTitleAttribute), "role": role,
                               "subrole": subrole, "size": axSize(win)])
        }
    }
    return ["ok": true, "running": !apps.isEmpty, "pids": pids, "windows": windowsOut,
            "has_modal": hasModal, "security_agent_active": isSecurityAgentActive()]
}

func handleTree(_ req: VerifiedRequest) -> [String: Any] {
    let apps = targetApps(bundleId: req.bundleId, bundlePath: req.bundlePath)
    guard let app = apps.first else { return ["ok": false, "error": "app_not_running"] }
    let axApp = makeAxApp(pid: app.processIdentifier, bundlePath: req.bundlePath)
    let maxDepth = min(14, max(2, (req.params["max_depth"] as? Int) ?? 10))
    var budget = min(600, max(50, (req.params["max_nodes"] as? Int) ?? 400))
    func collect(_ attr: String) -> (AXError, [[String: Any]]) {
        let (err, raw) = axCopy(axApp, attr)
        if err == .cannotComplete { return (err, []) }
        let roots: [AXUIElement] = (raw as? [AXUIElement]) ?? (raw.map { [$0 as! AXUIElement] } ?? [])
        var out: [[String: Any]] = []
        for (i, r) in roots.enumerated() {
            if let node = serializeNode(r, depth: 0, maxDepth: maxDepth, budget: &budget, path: [i]) {
                out.append(node)
            }
        }
        return (.success, out)
    }
    let (wErr, windows) = collect(kAXWindowsAttribute)
    if wErr == .cannotComplete { return ["ok": false, "error": "timeout", "message": "App did not respond"] }
    let (_, menuBar) = collect(kAXMenuBarAttribute)
    let (_, extras) = collect("AXExtrasMenuBar")
    return ["ok": true, "bundle_id": req.bundleId, "windows": windows, "menu_bar": menuBar,
            "menu_extras": extras, "truncated": budget <= 0, "security_agent_active": isSecurityAgentActive()]
}

func resolveElement(_ axApp: AXUIElement, rootName: String, path: [Int]) -> AXUIElement? {
    let attr: String = rootName == "menu_bar" ? kAXMenuBarAttribute
        : (rootName == "menu_extras" ? "AXExtrasMenuBar" : kAXWindowsAttribute)
    let (_, raw) = axCopy(axApp, attr)
    let roots: [AXUIElement] = (raw as? [AXUIElement]) ?? (raw.map { [$0 as! AXUIElement] } ?? [])
    guard let first = path.first, first >= 0 && first < roots.count else { return nil }
    var cur = roots[first]
    for step in path.dropFirst() {
        let kids = axChildren(cur)
        guard step >= 0 && step < kids.count else { return nil }
        cur = kids[step]
    }
    return cur
}

func handlePress(_ req: VerifiedRequest) -> [String: Any] {
    let apps = targetApps(bundleId: req.bundleId, bundlePath: req.bundlePath)
    guard let app = apps.first else { return ["ok": false, "error": "app_not_running"] }
    let allowedPids = Set(apps.map { $0.processIdentifier })
    guard let root = req.params["root"] as? String,
          let rawPath = req.params["path"] as? [Int], !rawPath.isEmpty,
          let wantRole = req.params["role"] as? String,
          let wantTitle = req.params["title"] as? String else {
        return ["ok": false, "error": "invalid_params"]
    }
    let axApp = makeAxApp(pid: app.processIdentifier, bundlePath: req.bundlePath)
    guard let el = resolveElement(axApp, rootName: root, path: rawPath) else {
        return ["ok": false, "error": "element_not_found"]
    }
    var pid: pid_t = 0
    guard AXUIElementGetPid(el, &pid) == .success, allowedPids.contains(pid) else {
        return ["ok": false, "error": "pid_mismatch"]
    }
    let curRole = axString(el, kAXRoleAttribute)
    let curTitle = axString(el, kAXTitleAttribute).isEmpty ? axString(el, kAXDescriptionAttribute) : axString(el, kAXTitleAttribute)
    guard curRole == wantRole && curTitle == wantTitle else {
        return ["ok": false, "error": "element_changed", "role": curRole, "title": curTitle]
    }
    let err = AXUIElementPerformAction(el, kAXPressAction as CFString)
    if err == .cannotComplete { return ["ok": false, "error": "timeout"] }
    return err == .success ? ["ok": true] : ["ok": false, "error": "ax_error", "code": Int(err.rawValue)]
}

func handleKey(_ req: VerifiedRequest) -> [String: Any] {
    let keyTable: [String: (CGKeyCode, CGEventFlags)] = [
        "Return": (36, []), "Escape": (53, []), "Tab": (48, []), "Shift-Tab": (48, .maskShift),
        "Space": (49, []), "Left": (123, []), "Right": (124, []), "Down": (125, []), "Up": (126, []),
    ]
    guard let name = req.params["key"] as? String, let (code, flags) = keyTable[name] else {
        return ["ok": false, "error": "disallowed_key"]
    }
    guard let app = targetApps(bundleId: req.bundleId, bundlePath: req.bundlePath).first else {
        return ["ok": false, "error": "app_not_running"]
    }
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
        return ["ok": false, "error": "event_failed"]
    }
    down.flags = flags; up.flags = flags
    down.postToPid(app.processIdentifier)
    up.postToPid(app.processIdentifier)
    return ["ok": true]
}

func handleMenu(_ req: VerifiedRequest) -> [String: Any] {
    guard let titles = req.params["titles"] as? [String], !titles.isEmpty else {
        return ["ok": false, "error": "invalid_params"]
    }
    guard let app = targetApps(bundleId: req.bundleId, bundlePath: req.bundlePath).first else {
        return ["ok": false, "error": "app_not_running"]
    }
    let useExtras = (req.params["extras"] as? Bool) == true
    if !useExtras { app.activate(options: []) }
    let axApp = makeAxApp(pid: app.processIdentifier, bundlePath: req.bundlePath)
    let (_, rawRoot) = axCopy(axApp, useExtras ? "AXExtrasMenuBar" : kAXMenuBarAttribute)
    guard let root = rawRoot else { return ["ok": false, "error": "menu_not_found"] }
    var current: AXUIElement = root as! AXUIElement
    for title in titles {
        var next: AXUIElement?
        for child in axChildren(current) {
            let t = axString(child, kAXTitleAttribute)
            if t == title { next = child; break }
            for sub in axChildren(child) where axString(sub, kAXTitleAttribute) == title {
                next = sub; break
            }
            if next != nil { break }
        }
        guard let found = next else { return ["ok": false, "error": "menu_item_not_found", "missing": title] }
        current = found
    }
    let err = AXUIElementPerformAction(current, kAXPressAction as CFString)
    return err == .success ? ["ok": true] : ["ok": false, "error": "ax_error", "code": Int(err.rawValue)]
}

func handleQuit(_ req: VerifiedRequest) -> [String: Any] {
    let apps = targetApps(bundleId: req.bundleId, bundlePath: req.bundlePath)
    var count = 0
    for app in apps where app.terminate() { count += 1 }
    return ["ok": true, "terminated": count]
}

func dispatch(_ req: VerifiedRequest) -> [String: Any] {
    if req.verb == "trusted" { return handleTrusted(req.params) }
    if req.verb == "quit" { return handleQuit(req) }
    if isScreenLocked() { return ["ok": false, "error": "screen_locked", "message": "Unlock your Mac and try again"] }
    if !AXIsProcessTrusted() { return ["ok": false, "error": "untrusted", "message": "Accessibility permission is not granted"] }
    switch req.verb {
    case "state": return handleState(req)
    case "tree": return handleTree(req)
    case "press": return handlePress(req)
    case "key": return handleKey(req)
    case "menu": return handleMenu(req)
    default: return ["ok": false, "error": "unsupported_verb"]
    }
}

// --- entrypoint ---------------------------------------------------------------------------------------------

func parseSockArg() -> String? {
    let args = CommandLine.arguments
    guard let idx = args.firstIndex(of: "--sock"), idx + 1 < args.count else { return nil }
    return args[idx + 1]
}

func run() -> Int32 {
    guard let key = loadLaunchKey(), let sockPath = parseSockArg() else { return 2 }
    guard let fd = connectSocket(sockPath) else { return 3 }
    defer { Darwin.close(fd) }
    guard let frame = readFrame(fd: fd, maxBytes: maxRequestBytes),
          let rawObj = try? JSONSerialization.jsonObject(with: frame, options: []),
          let rawDict = rawObj as? [String: Any] else { return 4 }
    let nonce = (rawDict["nonce"] as? String) ?? ""
    guard let req = verifyRequest(key: key, raw: rawDict) else {
        if !nonce.isEmpty, let reply = signReply(key: key, nonce: nonce, payload: ["ok": false, "error": "auth_failed"]) {
            _ = writeFrame(fd: fd, data: reply, maxBytes: maxReplyBytes)
        }
        return 5
    }
    let payload = dispatch(req)
    guard let reply = signReply(key: key, nonce: req.nonce, payload: payload),
          writeFrame(fd: fd, data: reply, maxBytes: maxReplyBytes) else { return 6 }
    return 0
}

exit(run())
