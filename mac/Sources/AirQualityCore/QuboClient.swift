import Foundation

public enum QuboError: LocalizedError, Equatable {
    case notSignedIn
    case auth(String)
    case http(Int, String)
    case badResponse(String)

    public var errorDescription: String? {
        switch self {
        case .notSignedIn: return "Sign in to your Qubo account."
        case .auth(let m): return m
        case .http(let code, let m): return "Qubo cloud error (HTTP \(code)): \(m)"
        case .badResponse(let m): return "Unexpected reply from Qubo: \(m)"
        }
    }
}

/// Client for the Qubo (Hero Electronix) cloud.
///
/// Qubo has no public API. Endpoints, headers and payloads follow the open-source qubopy
/// library (github.com/selvakk2k/qubopy, MIT), which reverse-engineered them from the
/// Android app. They may change without notice.
public final class QuboClient: @unchecked Sendable {
    public static let defaultBase = URL(string: "https://srvcapp.platform.quboworld.com")!
    static let sp = "d10e4bfb0153496e8e8bb955f7ebe413"
    static let appID = "com.hero.iot"
    public static let deviceLabel = "Air Quality for Mac"

    public let base: URL
    /// Stable per-install id sent as Source-Device-Id (like the Android app's uniqueAndroidID).
    public let installID: String
    public var onTokensChanged: ((QuboTokens?) -> Void)?

    private let session: URLSession
    private let lock = NSLock()
    private var _tokens: QuboTokens?
    private var phoneSession: String?

    public var tokens: QuboTokens? {
        lock.lock(); defer { lock.unlock() }
        return _tokens
    }

    public init(tokens: QuboTokens?, installID: String, session: URLSession = .shared, base: URL = QuboClient.defaultBase) {
        self._tokens = tokens
        self.installID = installID
        self.session = session
        self.base = base
    }

    private func setTokens(_ t: QuboTokens?) {
        lock.lock(); _tokens = t; lock.unlock()
        onTokensChanged?(t)
    }

    // MARK: - Sign in

    public func signIn(email: String, password: String) async throws {
        let body: [String: Any] = ["username": email, "email": email, "password": password,
                                   "deviceAttribute": Self.deviceLabel]
        var json = try await post("/sms/api/v1/sp/\(Self.sp)/user/login?system=CS", body: body,
                                   headers: ["Source": "USER", "Token-Type": "USER"])
        if let list = json as? [Any], let first = list.first { json = first }
        guard let d = json as? [String: Any] else { throw QuboError.badResponse("login") }
        setTokens(try Self.tokens(from: d, userUUID: nil))
    }

    /// Sends an SMS code. Call `verifyPhone` with it next.
    public func requestPhoneCode(phone: String, isdCode: String = "+91") async throws {
        let body: [String: Any] = ["phone": phone, "isdCode": Self.isd(isdCode), "deviceAttribute": Self.deviceLabel]
        let json = try await post("/sms/api/v1/sp/\(Self.sp)/user/phone/login", body: body, headers: ["Source": "USER"])
        guard let uuid = (json as? [String: Any])?["uuid"] as? String else {
            throw QuboError.badResponse("phone login")
        }
        phoneSession = uuid
    }

    /// Verifies the SMS code. If the account has an email on its profile, the short-lived phone
    /// session is exchanged for a long-lived one (refresh token valid for about six months).
    public func verifyPhone(phone: String, code: String, isdCode: String = "+91") async throws {
        guard let session = phoneSession else { throw QuboError.auth("Request a code first.") }
        let body: [String: Any] = ["phone": phone, "isdCode": Self.isd(isdCode), "otp": code,
                                   "deviceAttribute": Self.deviceLabel, "uuid": session]
        let json = try await post("/sms/api/v1/sp/\(Self.sp)/user/phone/verify", body: body, headers: ["Source": "USER"])
        let profiles: [[String: Any]] = (json as? [[String: Any]]) ?? ((json as? [String: Any]).map { [$0] } ?? [])
        guard let profile = profiles.first else { throw QuboError.auth("No Qubo account found for that number.") }
        let temp = try Self.tokens(from: profile, userUUID: nil)
        setTokens(temp)

        let email = QuboShadow.string(profile, "email")
        guard !email.isEmpty else { return }  // works, but only for about an hour
        let finalized = try await post(
            "/sms/api/v1/sp/\(Self.sp)/user/emailprofile/login",
            body: ["username": email, "deviceAttribute": Self.deviceLabel],
            headers: ["Token-Type": "USER", "Source": "Android", "User-UUID": temp.userUUID,
                      "Subscriber-Key": temp.accessToken])
        if let d = finalized as? [String: Any] {
            setTokens(try Self.tokens(from: d, userUUID: temp.userUUID))
        }
    }

    public func signOut() { setTokens(nil) }

    /// True when the saved sign-in can keep itself fresh (has a refresh token).
    public var canStaySignedIn: Bool { tokens?.refreshToken != nil }

    func refresh() async throws {
        guard let t = tokens, let refresh = t.refreshToken else {
            setTokens(nil)
            throw QuboError.auth("Your Qubo sign-in expired. Please sign in again.")
        }
        let (status, data) = try await send("POST", "/sms/api/v1/sp/\(Self.sp)/users/\(t.userUUID)/auth/refresh",
                                            body: ["accessToken": t.accessToken, "refreshToken": refresh],
                                            headers: authHeaders(t))
        guard status == 200, let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            if [400, 401, 403].contains(status) { setTokens(nil) }
            throw QuboError.auth("Your Qubo sign-in expired. Please sign in again.")
        }
        var next = try Self.tokens(from: d, userUUID: t.userUUID)
        if next.refreshToken == nil { next.refreshToken = refresh }
        setTokens(next)
    }

    // MARK: - Devices

    /// All air purifiers in the account, across every home ("unit").
    public func purifiers() async throws -> [QuboDevice] {
        let unitsJSON = try await request("GET", "/unit-entity-management/api/v1/sp/\(Self.sp)/units")
        var devices: [QuboDevice] = []
        var seen = Set<String>()
        for unit in Self.list(unitsJSON, keys: "data", "units") {
            let unitID = QuboShadow.string(unit, "unitUUID", "uuid", "unitId")
            guard !unitID.isEmpty else { continue }
            let devJSON = try await request("GET", "/unit-entity-management/api/v1/sp/\(Self.sp)/units/\(unitID)/devices")
            for d in Self.list(devJSON, keys: "data", "devices") {
                let device = Self.device(from: d, unitUUID: unitID)
                if !device.id.isEmpty, !seen.contains(device.id), device.isPurifier {
                    seen.insert(device.id)
                    devices.append(device)
                }
            }
        }
        return devices
    }

    public func state(of device: QuboDevice) async throws -> PurifierState {
        let json = try await request("GET", Self.devicePath(device) + "/shadow")
        return PurifierState.parse(shadow: json)
    }

    public func setPower(_ on: Bool, on device: QuboDevice) async throws {
        try await setAttribute(device, service: "lcSwitchControl", attribute: "power", value: on ? "on" : "off")
    }

    public func setMode(_ mode: PurifierMode, on device: QuboDevice) async throws {
        try await setAttribute(device, service: "fanControlMode", attribute: "state", value: mode.rawValue)
    }

    public func setFanSpeed(_ speed: Int, on device: QuboDevice) async throws {
        try await setAttribute(device, service: "fanSpeedControl", attribute: "speed", value: String(min(3, max(1, speed))))
    }

    public func setChildLock(_ on: Bool, on device: QuboDevice) async throws {
        try await setAttribute(device, service: "childLockControl", attribute: "state", value: on ? "enable" : "disable")
    }

    /// Ask the purifier to publish a fresh PM2.5 reading. Best effort: unverified on hardware.
    public func requestFreshReading(_ device: QuboDevice) async {
        let refresh: [String: Any] = ["parameters": [String: Any](), "instanceId": 0]
        let services: [String: Any] = ["aqiRefresh": ["commands": ["refresh": refresh] as [String: Any]] as [String: Any]]
        let entry: [String: Any] = ["deviceUUID": device.id, "services": services]
        let body: [String: Any] = ["devices": entry]
        _ = try? await request("PUT", Self.devicePath(device) + "/command", body: body)
    }

    func setAttribute(_ device: QuboDevice, service: String, attribute: String, value: String) async throws {
        let body: [String: Any] = ["instanceId": 0, "serviceName": service, "attributeName": attribute,
                                   "attributeValue": value]
        _ = try await request("PUT", Self.devicePath(device) + "/shadow/attribute", body: body)
    }

    static func devicePath(_ d: QuboDevice) -> String {
        // The shadow route falls back to the device id when there is no separate entity.
        let entity = d.entityUUID.isEmpty ? d.id : d.entityUUID
        return "/unit-entity-management/api/v1/sp/\(sp)/units/\(d.unitUUID)/entities/\(entity)/devices/\(d.id)"
    }

    static func device(from d: [String: Any], unitUUID: String) -> QuboDevice {
        let id = QuboShadow.string(d, "deviceUUID", "deviceId", "uuid")
        let online = QuboShadow.bool(d["isOnline"] ?? d["online"]) ?? true
        let name = QuboShadow.string(d, "deviceName", "name")
        return QuboDevice(
            id: id,
            name: name.isEmpty ? "Qubo \(id.prefix(6))" : name,
            model: QuboShadow.string(d, "deviceModel", "model", "aModelNo"),
            type: QuboShadow.string(d, "deviceType", "type"),
            unitUUID: QuboShadow.string(d, "unitUUID").isEmpty ? unitUUID : QuboShadow.string(d, "unitUUID"),
            entityUUID: QuboShadow.string(d, "entityUUID", "entityId"),
            isOnline: online)
    }

    static func list(_ json: Any, keys: String...) -> [[String: Any]] {
        if let l = json as? [[String: Any]] { return l }
        guard let d = json as? [String: Any] else { return [] }
        for k in keys {
            if let l = d[k] as? [[String: Any]] { return l }
        }
        return [d]
    }

    // MARK: - HTTP

    func authHeaders(_ t: QuboTokens) -> [String: String] {
        ["Authorization": "Bearer \(t.accessToken)", "Token-Type": "USER", "User-UUID": t.userUUID,
         "Subscriber-Key": t.accessToken, "Source": "Android"]
    }

    /// Authenticated request; refreshes the token ahead of expiry and once more on HTTP 401.
    func request(_ method: String, _ path: String, body: Any? = nil, retried: Bool = false) async throws -> Any {
        guard var t = tokens else { throw QuboError.notSignedIn }
        if t.expiresAt.timeIntervalSinceNow < 300, t.refreshToken != nil {
            try await refresh()
            guard let fresh = tokens else { throw QuboError.notSignedIn }
            t = fresh
        }
        let (status, data) = try await send(method, path, body: body, headers: authHeaders(t))
        if status == 401, !retried, t.refreshToken != nil {
            try await refresh()
            return try await request(method, path, body: body, retried: true)
        }
        if status == 401 || status == 403 {
            throw QuboError.auth("Qubo rejected the sign-in (HTTP \(status)). Please sign in again.")
        }
        guard [200, 201, 204].contains(status) else {
            throw QuboError.http(status, String(decoding: data.prefix(200), as: UTF8.self))
        }
        if data.isEmpty { return [String: Any]() }
        return (try? JSONSerialization.jsonObject(with: data)) ?? [String: Any]()
    }

    /// Unauthenticated POST used during sign-in.
    func post(_ path: String, body: [String: Any], headers: [String: String]) async throws -> Any {
        let (status, data) = try await send("POST", path, body: body, headers: headers)
        guard status == 200 else {
            throw QuboError.auth(Self.errorMessage(status: status, data: data))
        }
        guard let json = try? JSONSerialization.jsonObject(with: data) else {
            throw QuboError.badResponse("not JSON")
        }
        return json
    }

    func send(_ method: String, _ path: String, body: Any?, headers: [String: String]) async throws -> (Int, Data) {
        guard let url = URL(string: base.absoluteString + path) else { throw QuboError.badResponse("bad URL") }
        var req = URLRequest(url: url, timeoutInterval: 30)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(Self.appID, forHTTPHeaderField: "App-Id")
        req.setValue(installID, forHTTPHeaderField: "Source-Device-Id")
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await session.data(for: req)
        return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
    }

    // MARK: - Helpers

    static func isd(_ code: String) -> String { code.hasPrefix("+") ? code : "+\(code)" }

    static func tokens(from d: [String: Any], userUUID: String?) throws -> QuboTokens {
        let access = QuboShadow.string(d, "accessToken", "token", "access_token")
        let user = userUUID ?? QuboShadow.string(d, "uuid", "userUUID", "userId")
        guard !access.isEmpty, !user.isEmpty else { throw QuboError.auth("Qubo did not return a session.") }
        let refresh = QuboShadow.string(d, "refreshToken", "refresh_token")
        return QuboTokens(accessToken: access, refreshToken: refresh.isEmpty ? nil : refresh, userUUID: user,
                          expiresAt: jwtExpiry(access) ?? Date().addingTimeInterval(3600))
    }

    /// Reads `exp` from a JWT without verifying it (only to know when to refresh).
    public static func jwtExpiry(_ jwt: String) -> Date? {
        let parts = jwt.split(separator: ".")
        guard parts.count >= 2 else { return nil }
        var b64 = String(parts[1]).replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        while b64.count % 4 != 0 { b64 += "=" }
        guard let data = Data(base64Encoded: b64),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let exp = payload["exp"] as? NSNumber else { return nil }
        return Date(timeIntervalSince1970: exp.doubleValue)
    }

    static func errorMessage(status: Int, data: Data) -> String {
        if let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let msg = QuboShadow.string(d, "message", "errorMessage", "error", "errorCode")
            if !msg.isEmpty { return "Qubo: \(msg)" }
        }
        switch status {
        case 400, 401: return "Qubo didn't accept those details."
        case 429: return "Too many attempts. Wait a minute and try again."
        default: return "Qubo sign-in failed (HTTP \(status))."
        }
    }
}
