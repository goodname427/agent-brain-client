// Candidate macOS device component. Build/self-check does not access Keychain.
// Real installation/signing, Keychain access group, transport and grants require
// a concrete approved deployment. No raw credential export operation exists.
import Foundation
import AppKit
import Security
import CryptoKit

struct Blocked: Error {}
struct EnrollmentTerminal: Error { let reason: String }
struct SecretRecord: Codable, Equatable { let credential: String; let nonce: String; let device_id: String }
protocol SecretStore { func load() throws -> SecretRecord?; func save(_ value: SecretRecord) throws; func clear(_ expected: SecretRecord) throws }
protocol PairTransport { func prepare(_ code: String, _ nonce: String) throws -> SecretRecord; func confirm(_ saved: SecretRecord) throws -> [String:Any] }
protocol OperatorIssuer { func issue(_ credential: String) throws -> String }
func json(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]) }
func object(_ data: Data) throws -> [String:Any] { guard let value=try JSONSerialization.jsonObject(with:data) as? [String:Any] else { throw Blocked() };return value }
func sha(_ data: Data) -> String { SHA256.hash(data:data).map { String(format:"%02x",$0) }.joined() }
func compatibilityHeaders(_ mode: String) throws -> [String:String] {
    guard ["synthetic","personal"].contains(mode) else { throw Blocked() }
    var value=["X-Agent-Brain-Adapter":"codex","X-Agent-Brain-Protocol":"1","X-Agent-Brain-State-Schema":"1"]
    if mode=="personal" { value["X-Agent-Brain-Data-Mode"]="personal" }
    return value
}
func validateIDs(_ grant: [String:Any]) throws -> [String:Any] {
    guard let types=grant["artifact_types"] as? [String],!types.isEmpty,Set(types).count==types.count,types.allSatisfy({["memory","rule","skill"].contains($0)}),let ids=grant["ids"] as? [String:Any],Set(ids.keys)==Set(types) else { throw Blocked() }
    for (kind,value) in ids {
        if kind=="memory",value as? String == "*" { continue }
        guard let values=value as? [String],values.count<=256,Set(values).count==values.count,values.allSatisfy({$0.range(of:"^[a-z0-9][a-z0-9-]{0,63}$",options:.regularExpression) != nil}) else { throw Blocked() }
    }
    return ids
}
func allowsID(_ ids: [String:Any],_ kind: String,_ identity: String) -> Bool { kind=="memory" && ids[kind] as? String == "*" || (ids[kind] as? [String] ?? []).contains(identity) }
func authorizeRequest(_ request: [String:Any],_ grant: [String:Any]) throws {
    let ids=try validateIDs(grant)
    guard Set(request.keys)==Set(["request_id","method","params"]),let rid=request["request_id"] as? String,rid.range(of:"^[A-Za-z0-9_-]{16,128}$",options:.regularExpression) != nil,let method=request["method"] as? String,let params=request["params"] as? [String:Any],let scopes=grant["scopes"] as? [String],let types=grant["artifact_types"] as? [String] else { throw Blocked() }
    let fields: [String:Set<String>]=["content_catalog":["kinds"],"content_read":["kind","id"],"content_put":["kind","id","record","expected_revision"],"content_remove":["kind","id","expected_revision"]]
    guard let expected=fields[method],Set(params.keys)==expected,scopes.contains(["content_put","content_remove"].contains(method) ? "write":"read") else { throw Blocked() }
    if method=="content_catalog" {
        guard let kinds=params["kinds"] as? [String],!kinds.isEmpty,Set(kinds).count==kinds.count,kinds.allSatisfy({types.contains($0)}) else { throw Blocked() }
    } else {
        guard let kind=params["kind"] as? String,types.contains(kind),let identity=params["id"] as? String,identity.range(of:"^[a-z0-9][a-z0-9-]{0,63}$",options:.regularExpression) != nil,allowsID(ids,kind,identity) else { throw Blocked() }
    }
}
func enroll(_ input: () throws -> String?, _ nonce: () -> String, _ store: SecretStore, _ transport: PairTransport) -> [String:Any] {
    do {
        var saved=try store.load()
        if saved == nil {
            guard let code=try input() else { return ["status":"pairing-cancelled","installed":false] }
            let prepared=try transport.prepare(code,nonce());try store.save(prepared);saved=prepared
        }
        guard let value=saved else { throw Blocked() }
        let result=try transport.confirm(value)
        guard result["phase"] as? String == "active",result["device_id"] as? String == value.device_id,let grant=result["grant"] as? [String:Any] else { throw Blocked() }
        return ["status":"device-paired","device_id":value.device_id,"grant":grant,"installed":false]
    } catch let error as EnrollmentTerminal { return ["status":"pairing-reenrollment-review-required","reason":error.reason,"installed":false] }
    catch { return ["status":"pairing-resume-required","installed":false] }
}
func operatorEnroll(_ secureAuthorization: () throws -> String?,_ nonce: () -> String,_ store: SecretStore,_ transport: PairTransport,_ issuer: OperatorIssuer) -> [String:Any] {
    do {
        // Resume an already stored device before requesting any operator input.
        if try store.load() != nil { return enroll({nil},nonce,store,transport) }
        guard let credential=try secureAuthorization() else { return ["status":"operator-pairing-cancelled","installed":false] }
        let code=try issuer.issue(credential)
        // Both secrets stay inside the signed native process. The operator
        // credential is not saved; the device record follows normal save/confirm.
        return enroll({code},nonce,store,transport)
    } catch { return ["status":"operator-pairing-review-required","installed":false] }
}
func resetEnrollment(_ confirmUI: (String,String) -> Bool,_ store: SecretStore,_ transport: PairTransport) -> [String:Any] {
    do {
        guard let saved=try store.load() else { return ["status":"pairing-not-enrolled","installed":false] }
        do {
            let result=try transport.confirm(saved)
            guard result["phase"] as? String == "active",result["device_id"] as? String == saved.device_id else { throw Blocked() }
            return ["status":"active-enrollment-preserved","installed":false]
        } catch let error as EnrollmentTerminal {
            guard ["pairing_expired","device_revoked"].contains(error.reason) else { throw Blocked() }
            guard confirmUI(error.reason,saved.device_id) else { return ["status":"pairing-reset-cancelled","installed":false] }
            try store.clear(saved)
            return ["status":"pairing-reenrollment-ready","installed":false]
        }
    } catch { return ["status":"pairing-reset-blocked","installed":false] }
}

#if arch(arm64)
let actualPlatform="darwin-arm64"
#elseif arch(x86_64)
let actualPlatform="darwin-x86_64"
#else
let actualPlatform="unsupported"
#endif
struct Profile {
    let endpoint: URL;let namespace: String;let accessGroup: String;let platform: String;let dataMode: String;let grant: [String:Any];let issuerPolicySHA: String?
    init(_ path: String,_ digest: String) throws {
        let url=URL(fileURLWithPath:path);let attrs=try FileManager.default.attributesOfItem(atPath:path)
        guard attrs[.type] as? FileAttributeType == .typeRegular,let mode=attrs[.posixPermissions] as? NSNumber,mode.intValue & 0o077 == 0 else { throw Blocked() }
        let raw=try Data(contentsOf:url);guard raw.count<=16384,sha(raw)==digest else { throw Blocked() }
        let value=try object(raw)
        let fields=Set(["schema","transport","endpoint","adapter","platform","data_mode","access_group","grant"])
        guard (Set(value.keys)==fields || Set(value.keys)==fields.union(["issuer_policy_sha256"])),value["schema"] as? Int == 1,value["transport"] as? String == "https",value["adapter"] as? String == "codex",let platform=value["platform"] as? String,platform==actualPlatform,let dataMode=value["data_mode"] as? String,["synthetic","personal"].contains(dataMode),let endpointString=value["endpoint"] as? String,let parsed=URLComponents(string:endpointString),parsed.scheme=="https",parsed.host != nil,parsed.user==nil,parsed.password==nil,parsed.query==nil,parsed.fragment==nil,["","/"].contains(parsed.path),let endpoint=parsed.url,let group=value["access_group"] as? String,!group.isEmpty,let grant=value["grant"] as? [String:Any] else { throw Blocked() }
        let issuerSHA=value["issuer_policy_sha256"] as? String
        if value["issuer_policy_sha256"] != nil { guard let digest=issuerSHA,digest.range(of:"^[a-f0-9]{64}$",options:.regularExpression) != nil else { throw Blocked() } }
        guard Set(grant.keys)==Set(["actor","scopes","artifact_types","ids","adapter","platform"]),let actor=grant["actor"] as? String,actor.range(of:"^[a-z0-9][a-z0-9-]{0,63}$",options:.regularExpression) != nil,let scopes=grant["scopes"] as? [String],!scopes.isEmpty,Set(scopes).count==scopes.count,scopes.allSatisfy({["read","write"].contains($0)}),grant["adapter"] as? String == "codex",grant["platform"] as? String == platform else { throw Blocked() }
        _=try validateIDs(grant)
        self.endpoint=endpoint;self.namespace="agent-brain.device."+digest;self.accessGroup=group;self.platform=platform;self.dataMode=dataMode;self.grant=grant;self.issuerPolicySHA=issuerSHA
    }
}
final class KeychainStore: SecretStore {
    let profile: Profile
    init(_ profile: Profile) { self.profile=profile }
    func query() -> [String:Any] {
        [kSecClass as String:kSecClassGenericPassword,kSecAttrService as String:profile.namespace,kSecAttrAccount as String:"enrollment",kSecAttrAccessGroup as String:profile.accessGroup,kSecUseDataProtectionKeychain as String:true]
    }
    func load() throws -> SecretRecord? {
        var q=query();q[kSecReturnData as String]=true;q[kSecMatchLimit as String]=kSecMatchLimitOne
        var found: CFTypeRef?;let status=SecItemCopyMatching(q as CFDictionary,&found)
        if status==errSecItemNotFound { return nil }
        guard status==errSecSuccess,let data=found as? Data else { throw Blocked() }
        return try JSONDecoder().decode(SecretRecord.self,from:data)
    }
    func save(_ value: SecretRecord) throws {
        // Never update/delete an unknown pre-existing item to force enrollment.
        var q=query();q[kSecAttrAccessible as String]=kSecAttrAccessibleWhenUnlockedThisDeviceOnly;q[kSecValueData as String]=try JSONEncoder().encode(value)
        guard SecItemAdd(q as CFDictionary,nil)==errSecSuccess else { throw Blocked() }
    }
    func clear(_ expected: SecretRecord) throws {
        // Read both data and this exact item's persistent reference. Never
        // delete by broad service/account alone, or clear an active/unknown item.
        var q=query();q[kSecReturnData as String]=true;q[kSecReturnPersistentRef as String]=true;q[kSecMatchLimit as String]=kSecMatchLimitOne
        var found: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary,&found)==errSecSuccess,let result=found as? [String:Any],let data=result[kSecValueData as String] as? Data,let reference=result[kSecValuePersistentRef as String] as? Data,try JSONDecoder().decode(SecretRecord.self,from:data)==expected else { throw Blocked() }
        var deletion=query();deletion[kSecMatchItemList as String]=[reference]
        guard SecItemDelete(deletion as CFDictionary)==errSecSuccess else { throw Blocked() }
    }
}
final class NoRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession,task: URLSessionTask,willPerformHTTPRedirection response: HTTPURLResponse,newRequest request: URLRequest,completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
final class HTTPTransport: PairTransport,OperatorIssuer {
    let profile: Profile;let delegate=NoRedirect()
    init(_ profile: Profile) { self.profile=profile }
    func post(_ path: String,_ body: [String:Any],_ token: String? = nil,_ contentRequest: Bool = false) throws -> [String:Any] {
        var request=URLRequest(url:profile.endpoint.appendingPathComponent(path));request.httpMethod="POST";request.timeoutInterval=15;request.setValue("application/json",forHTTPHeaderField:"Content-Type");request.httpBody=try json(body)
        if let token=token { request.setValue("Bearer "+token,forHTTPHeaderField:"Authorization") }
        if contentRequest {
            for (key,value) in try compatibilityHeaders(profile.dataMode) { request.setValue(value,forHTTPHeaderField:key) }
        }
        let config=URLSessionConfiguration.ephemeral;config.httpCookieStorage=nil;config.urlCredentialStorage=nil;config.requestCachePolicy = .reloadIgnoringLocalCacheData;config.timeoutIntervalForResource=15
        let session=URLSession(configuration:config,delegate:delegate,delegateQueue:nil);defer { session.invalidateAndCancel() }
        let done=DispatchSemaphore(value:0);var result: Data?
        let task=session.dataTask(with:request) { data,response,error in
            if error==nil,let response=response as? HTTPURLResponse,(response.statusCode==200 || contentRequest && (400...599).contains(response.statusCode) || path=="v1/pairing/confirm" && response.statusCode==403),let data=data,data.count<=262144 { result=data };done.signal()
        };task.resume()
        guard done.wait(timeout:.now()+16) == .success,let data=result else { throw Blocked() }
        let value=try object(data)
        if path=="v1/pairing/confirm",let error=value["error"] as? [String:Any] {
            if let code=error["code"] as? String,["pairing_expired","device_revoked"].contains(code) { throw EnrollmentTerminal(reason:code) }
            throw Blocked()
        }
        return value
    }
    func prepare(_ code: String,_ nonce: String) throws -> SecretRecord {
        let result=try post("v1/pairing/prepare",["code":code,"nonce":nonce,"device":["adapter":"codex","platform":profile.platform]])
        guard result["phase"] as? String == "pending",let grant=result["grant"] as? [String:Any],try json(grant)==json(profile.grant),let token=result["credential"] as? String,let device=result["device_id"] as? String,result["nonce"] as? String == nonce else { throw Blocked() }
        return SecretRecord(credential:token,nonce:nonce,device_id:device)
    }
    func issue(_ credential: String) throws -> String {
        guard let digest=profile.issuerPolicySHA,profile.dataMode=="personal",credential.range(of:"^[A-Za-z0-9_-]{32,128}$",options:.regularExpression) != nil else { throw Blocked() }
        let result=try post("v1/pairing/issue",["issuer_policy_sha256":digest,"device":["adapter":"codex","platform":profile.platform]],credential)
        guard result["phase"] as? String == "issued",let grant=result["grant"] as? [String:Any],try json(grant)==json(profile.grant),let code=result["code"] as? String,code.range(of:"^[A-Za-z0-9_-]{32,128}$",options:.regularExpression) != nil,let ttl=result["expires_in"] as? Int,(30...600).contains(ttl) else { throw Blocked() }
        return code
    }
    func confirm(_ saved: SecretRecord) throws -> [String:Any] {
        let result=try post("v1/pairing/confirm",["nonce":saved.nonce],saved.credential)
        guard let grant=result["grant"] as? [String:Any],try json(grant)==json(profile.grant) else { throw Blocked() };return result
    }
    func request(_ value: [String:Any],_ saved: SecretRecord) throws -> [String:Any] {
        try authorizeRequest(value,profile.grant)
        var result=try post("v1/requests",value,saved.credential,true)
        guard result["request_id"] as? String == value["request_id"] as? String,!String(data:try json(result),encoding:.utf8)!.contains(saved.credential) else { throw Blocked() }
        if ["content_put","content_remove"].contains(value["method"] as? String ?? ""),result["error"]==nil {
            guard let persistence=result["persistence"] as? [String:Any],persistence["durable"] as? Bool == true else { throw Blocked() }
        }
        if value["method"] as? String == "content_catalog",result["error"]==nil {
            guard var catalog=result["result"] as? [String:Any],let artifacts=catalog["artifacts"] as? [[String:Any]],let params=value["params"] as? [String:Any],let kinds=params["kinds"] as? [String] else { throw Blocked() }
            let ids=try validateIDs(profile.grant)
            for item in artifacts { guard let kind=item["kind"] as? String,kinds.contains(kind),item["id"] is String else { throw Blocked() } }
            catalog["artifacts"]=artifacts.filter({allowsID(ids,$0["kind"] as! String,$0["id"] as! String)});result["result"]=catalog
        }
        return result
    }
}
func nativeInput(_ profile: Profile) -> String? {
    let app=NSApplication.shared;app.setActivationPolicy(.accessory);app.activate(ignoringOtherApps:true)
    let dialog=NSAlert();dialog.messageText="Connect Agent Brain";dialog.informativeText="Destination: "+(profile.endpoint.host ?? "")+"\nEnter the short-lived pairing code here. It is not sent to chat."
    dialog.addButton(withTitle:"Connect");dialog.addButton(withTitle:"Cancel")
    let field=NSSecureTextField(frame:NSRect(x:0,y:0,width:340,height:24));dialog.accessoryView=field;dialog.window.initialFirstResponder=field
    guard dialog.runModal() == .alertFirstButtonReturn else { return nil };return field.stringValue
}
func nativeReset(_ profile: Profile,_ reason: String,_ device: String) -> Bool {
    let app=NSApplication.shared;app.setActivationPolicy(.accessory);app.activate(ignoringOtherApps:true)
    let dialog=NSAlert();dialog.messageText="Replace inactive Agent Brain enrollment?"
    dialog.informativeText="Destination: "+(profile.endpoint.host ?? "")+"\nDevice: "+device+"\nStatus: "+reason+"\nClear this inactive enrollment, then enter a fresh pairing code."
    dialog.addButton(withTitle:"Cancel");dialog.addButton(withTitle:"Clear inactive enrollment")
    return dialog.runModal() == .alertSecondButtonReturn
}
func nativeOperatorInput(_ profile: Profile) -> String? {
    let app=NSApplication.shared;app.setActivationPolicy(.accessory);app.activate(ignoringOtherApps:true)
    let dialog=NSAlert();dialog.messageText="Authorize this Agent Brain device"
    dialog.informativeText="Destination: "+(profile.endpoint.host ?? "")+"\nActor: "+(profile.grant["actor"] as? String ?? "")+"\nScopes: "+((profile.grant["scopes"] as? [String] ?? []).joined(separator:", "))+"\nIssuer policy: "+(profile.issuerPolicySHA ?? "")+"\nEnter the separately approved operator authorization here. It is used once, not saved or sent to chat. The short-lived device code stays inside this component."
    dialog.addButton(withTitle:"Authorize device");dialog.addButton(withTitle:"Cancel")
    let scope=NSScrollView(frame:NSRect(x:0,y:0,width:380,height:140));scope.hasVerticalScroller=true;scope.borderType = .bezelBorder
    let metadata=NSTextView(frame:NSRect(x:0,y:0,width:360,height:140));metadata.isEditable=false;metadata.string=String(data:(try? json(profile.grant)) ?? Data(),encoding:.utf8) ?? "";scope.documentView=metadata
    let field=NSSecureTextField(frame:NSRect(x:0,y:0,width:380,height:24))
    let stack=NSStackView(views:[scope,field]);stack.orientation = .vertical;stack.spacing=8;stack.frame=NSRect(x:0,y:0,width:380,height:180);dialog.accessoryView=stack;dialog.window.initialFirstResponder=field
    defer { field.stringValue="" }
    guard dialog.runModal() == .alertFirstButtonReturn else { return nil };return field.stringValue
}
func selfCheck() throws -> [String:Any] {
    // No GUI, network, Keychain, random real credentials or native registration.
    guard try compatibilityHeaders("personal")["X-Agent-Brain-Data-Mode"]=="personal",try compatibilityHeaders("synthetic")["X-Agent-Brain-Data-Mode"]==nil else { throw Blocked() }
    var invalidMode=false;do { _=try compatibilityHeaders("unknown") } catch { invalidMode=true };guard invalidMode else { throw Blocked() }
    final class Store: SecretStore {
        var value: SecretRecord?;var events=[String]()
        func load() throws -> SecretRecord? { value }
        func save(_ saved: SecretRecord) throws { events.append("stored");value=saved }
        func clear(_ expected: SecretRecord) throws { guard value==expected else { throw Blocked() };value=nil;events.append("cleared") }
    }
    final class Transport: PairTransport {
        var failed=false;var calls=0;var terminal=false;let store: Store
        init(_ store: Store) { self.store=store }
        func prepare(_ code: String,_ nonce: String) throws -> SecretRecord { calls+=1;return SecretRecord(credential:"SYNTHETIC_ONLY_CREDENTIAL_FIXTURE",nonce:nonce,device_id:"synthetic-device") }
        func confirm(_ saved: SecretRecord) throws -> [String:Any] {
            guard store.value != nil else { throw Blocked() }
            if terminal { throw EnrollmentTerminal(reason:"pairing_expired") }
            if !failed { failed=true;throw Blocked() }
            return ["phase":"active","device_id":saved.device_id,"grant":["scopes":["read"]]]
        }
    }
    final class Issuer: OperatorIssuer {
        var calls=0
        func issue(_ credential: String) throws -> String { guard credential=="SYNTHETIC_ONLY_OPERATOR_CREDENTIAL" else { throw Blocked() };calls+=1;return "SYNTHETIC_ONLY_INTERNAL_PAIRING_CODE" }
    }
    let operatorStore=Store();let operatorTransport=Transport(operatorStore);let issuer=Issuer();var operatorInputs=0
    let authorization={ () -> String? in operatorInputs+=1;return "SYNTHETIC_ONLY_OPERATOR_CREDENTIAL" }
    let operatorFirst=operatorEnroll(authorization,{"synthetic-nonce"},operatorStore,operatorTransport,issuer)
    let operatorResumed=operatorEnroll(authorization,{"unused"},operatorStore,operatorTransport,issuer)
    guard operatorFirst["status"] as? String == "pairing-resume-required",operatorResumed["status"] as? String == "device-paired",operatorInputs==1,issuer.calls==1,operatorStore.events==["stored"],!String(data:try json(operatorResumed),encoding:.utf8)!.contains("SYNTHETIC_ONLY_OPERATOR"),!String(data:try json(operatorResumed),encoding:.utf8)!.contains("INTERNAL_PAIRING_CODE") else { throw Blocked() }
    let store=Store();let transport=Transport(store);var inputs=0
    let input={ () -> String? in inputs+=1;return "SYNTHETIC_ONLY_PAIRING_CODE" }
    let blocked=enroll(input,{"synthetic-nonce"},store,transport);let paired=enroll(input,{"synthetic-nonce"},store,transport)
    guard blocked["status"] as? String == "pairing-resume-required",paired["status"] as? String == "device-paired",inputs==1,transport.calls==1,store.events==["stored"],!String(data:try json(paired),encoding:.utf8)!.contains("SYNTHETIC_ONLY_CREDENTIAL") else { throw Blocked() }
    guard resetEnrollment({_,_ in true},store,transport)["status"] as? String == "active-enrollment-preserved",store.value != nil else { throw Blocked() }
    transport.terminal=true
    guard enroll(input,{"unused"},store,transport)["status"] as? String == "pairing-reenrollment-review-required",resetEnrollment({_,_ in false},store,transport)["status"] as? String == "pairing-reset-cancelled",store.value != nil else { throw Blocked() }
    guard resetEnrollment({_,_ in true},store,transport)["status"] as? String == "pairing-reenrollment-ready",store.value==nil else { throw Blocked() }
    let grant: [String:Any]=["scopes":["read"],"artifact_types":["memory"],"ids":["memory":["synthetic"]]]
    try authorizeRequest(["request_id":"SYNTHETIC_REQUEST_ID","method":"content_read","params":["kind":"memory","id":"synthetic"]],grant)
    for request: [String:Any] in [
        ["request_id":"SYNTHETIC_REQUEST_ID","method":"content_put","params":["kind":"memory","id":"synthetic","record":[:],"expected_revision":"missing"]],
        ["request_id":"SYNTHETIC_REQUEST_ID","method":"content_read","params":["kind":"skill","id":"synthetic"]],
        ["request_id":"SYNTHETIC_REQUEST_ID","method":"content_read","params":["kind":"memory","id":"other"]],
        ["request_id":"SYNTHETIC_REQUEST_ID","method":"credential_export","params":[:]]
    ] {
        var denied=false;do { try authorizeRequest(request,grant) } catch { denied=true };guard denied else { throw Blocked() }
    }
    let writeGrant: [String:Any]=["scopes":["read","write"],"artifact_types":["rule"],"ids":["rule":["rule-a"]]]
    for method in ["content_read","content_put","content_remove"] {
        var params: [String:Any]=["kind":"rule","id":"rule-b"]
        if method != "content_read" { params["expected_revision"]="missing" }
        if method == "content_put" { params["record"]=[String:Any]() }
        var denied=false;do { try authorizeRequest(["request_id":"SYNTHETIC_REQUEST_ID","method":method,"params":params],writeGrant) } catch { denied=true };guard denied else { throw Blocked() }
    }
    return ["device_component_self_check":"passed","keychain_accessed":false,"gui_opened":false,"network_used":false,"native_changed":false]
}
let args=Array(CommandLine.arguments.dropFirst())
do {
    let result: [String:Any]
    if args==["--self-check"] { result=try selfCheck() }
    else if args.count==5,args[0]=="--operator-pair",args[1]=="--profile",args[3]=="--profile-sha256" {
        let profile=try Profile(args[2],args[4]);guard profile.issuerPolicySHA != nil,profile.dataMode=="personal" else { throw Blocked() }
        let transport=HTTPTransport(profile)
        result=operatorEnroll({nativeOperatorInput(profile)},{UUID().uuidString},KeychainStore(profile),transport,transport)
    } else if args.count==5,args[0]=="--pair",args[1]=="--profile",args[3]=="--profile-sha256" {
        let profile=try Profile(args[2],args[4]);let store=KeychainStore(profile);let transport=HTTPTransport(profile)
        result=enroll({nativeInput(profile)},{UUID().uuidString},store,transport)
    } else if args.count==5,args[0]=="--request",args[1]=="--profile",args[3]=="--profile-sha256" {
        let profile=try Profile(args[2],args[4]);let raw=try FileHandle.standardInput.read(upToCount:131073) ?? Data()
        guard !raw.isEmpty,raw.count<=131072 else { throw Blocked() }
        let value=try object(raw);try authorizeRequest(value,profile.grant)
        guard let saved=try KeychainStore(profile).load() else { throw Blocked() }
        result=try HTTPTransport(profile).request(value,saved)
    } else if args.count==5,args[0]=="--reset-enrollment",args[1]=="--profile",args[3]=="--profile-sha256" {
        let profile=try Profile(args[2],args[4])
        result=resetEnrollment({reason,device in nativeReset(profile,reason,device)},KeychainStore(profile),HTTPTransport(profile))
    } else { result=["status":"device-component-configuration-required","installed":false] }
    print(String(data:try json(result),encoding:.utf8)!)
} catch { print("{\"status\":\"device-component-review-required\",\"installed\":false}") }
