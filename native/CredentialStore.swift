// Ordinary login Keychain helper. No Team, access group, provisioning or DPKeychain.
// Dedicated item ACL names this executable only; never trust all applications.
// Private stdin/stdout protocol is consumed by the adapter, never exposed as MCP.
import Foundation
import Security
import AppKit

enum Blocked: Error { case request, store }
func dataJSON(_ value: Any) throws -> Data {
    try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
}
func execute(_ request: [String: Any]) throws -> [String: Any] {
    guard let op = request["op"] as? String,
          let service = request["service"] as? String,
          service.range(of: "^agent-brain\\.https\\.[a-f0-9]{64}$", options: .regularExpression) != nil,
          Set(request.keys).isSubset(of: ["op", "service", "expected", "record", "reason", "device_id"]) else { throw Blocked.request }
    if op == "confirm-reset" {
        guard Set(request.keys) == ["op", "service", "reason", "device_id"],
              let reason = request["reason"] as? String,
              ["pairing_expired", "device_revoked"].contains(reason),
              let device = request["device_id"] as? String,
              device.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else { throw Blocked.request }
        let app = NSApplication.shared; app.setActivationPolicy(.accessory)
        let alert = NSAlert(); alert.messageText = "Reset terminal Agent Brain pairing?"
        alert.informativeText = "Device \(device): \(reason). Clear only this exact terminal credential record. Content and offline writes are preserved."
        alert.addButton(withTitle: "Clear terminal record"); alert.addButton(withTitle: "Cancel")
        app.activate(ignoringOtherApps: true)
        return ["confirmed": alert.runModal() == .alertFirstButtonReturn]
    }
    if op == "input" {
        guard Set(request.keys) == ["op", "service"] else { throw Blocked.request }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let alert = NSAlert()
        alert.messageText = "Agent Brain device pairing"
        alert.informativeText = "Enter the short-lived invitation in this trusted local window."
        alert.addButton(withTitle: "Pair device"); alert.addButton(withTitle: "Cancel")
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24))
        alert.accessoryView = field; alert.window.initialFirstResponder = field
        app.activate(ignoringOtherApps: true)
        if alert.runModal() != .alertFirstButtonReturn { return ["cancelled": true] }
        let code = field.stringValue; field.stringValue = ""
        guard code.range(of: "^[A-Za-z0-9_-]{32,128}$", options: .regularExpression) != nil else { throw Blocked.request }
        return ["code": code]
    }
    guard ["load", "save", "clear"].contains(op) else { throw Blocked.request }
    let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service, kSecAttrAccount as String: "agent-brain-device-v1"]
    var read = query; read[kSecReturnData as String] = true; read[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(read as CFDictionary, &result)
    guard status == errSecSuccess || status == errSecItemNotFound else { throw Blocked.store }
    let bytes = result as? Data
    let existing = try bytes.map { try JSONSerialization.jsonObject(with: $0) }
    if op == "load" { return ["record": existing ?? NSNull()] }
    guard let expected = request["expected"],
          try dataJSON([expected]) == dataJSON([existing ?? NSNull()]) else { throw Blocked.store }
    if op == "clear" {
        guard Set(request.keys) == ["op", "service", "expected"] else { throw Blocked.request }
        if status == errSecSuccess && SecItemDelete(query as CFDictionary) != errSecSuccess { throw Blocked.store }
        return ["saved": true]
    }
    guard Set(request.keys) == ["op", "service", "expected", "record"],
          let record = request["record"] as? [String: Any] else { throw Blocked.request }
    let content = try dataJSON(record)
    guard content.count <= 16384 else { throw Blocked.request }
    if status == errSecItemNotFound {
        var trusted: SecTrustedApplication?
        guard SecTrustedApplicationCreateFromPath(CommandLine.arguments[0], &trusted) == errSecSuccess,
              let trusted = trusted else { throw Blocked.store }
        var access: SecAccess?
        guard SecAccessCreate("Agent Brain HTTPS device" as CFString, [trusted] as CFArray, &access) == errSecSuccess,
              let access = access else { throw Blocked.store }
        var add = query; add[kSecValueData as String] = content; add[kSecAttrAccess as String] = access
        guard SecItemAdd(add as CFDictionary, nil) == errSecSuccess else { throw Blocked.store }
    } else {
        guard SecItemUpdate(query as CFDictionary, [kSecValueData as String: content] as CFDictionary) == errSecSuccess else { throw Blocked.store }
    }
    return ["saved": true]
}
if CommandLine.arguments.dropFirst().elementsEqual(["--self-check"]) {
    print("{\"credential_helper_self_check\":\"passed\",\"keychain_accessed\":false,\"ui_opened\":false}")
} else {
    do {
        let data = FileHandle.standardInput.readData(ofLength: 32769)
        guard data.count <= 32768,
              let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw Blocked.request }
        FileHandle.standardOutput.write(try dataJSON(execute(request)))
    } catch {
        print("{\"error\":\"credential_store_blocked\"}"); exit(2)
    }
}
