import XCTest
@testable import AirQualityCore

/// Fake network: records requests and answers from a handler.
final class StubProtocol: URLProtocol {
    static var handler: ((URLRequest) -> (Int, String))?
    static var requests: [URLRequest] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var req = request
        if req.httpBody == nil, let stream = req.httpBodyStream {  // URLSession moves bodies into a stream
            stream.open()
            var data = Data()
            var buf = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                data.append(buf, count: n)
            }
            stream.close()
            req.httpBody = data
        }
        Self.requests.append(req)
        let (status, body) = Self.handler?(req) ?? (500, "")
        let resp = HTTPURLResponse(url: req.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class QuboClientTests: XCTestCase {
    var client: QuboClient!
    let future = Date().addingTimeInterval(86_400)

    override func setUp() {
        StubProtocol.requests = []
        StubProtocol.handler = nil
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        client = QuboClient(tokens: nil, installID: "install-1", session: URLSession(configuration: config))
    }

    private func body(_ r: URLRequest) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: r.httpBody ?? Data())) as? [String: Any] ?? [:]
    }

    func testEmailSignInSendsAppHeadersAndStoresTokens() async throws {
        StubProtocol.handler = { _ in (200, #"{"accessToken":"A1","refreshToken":"R1","uuid":"U1"}"#) }
        try await client.signIn(email: "me@example.com", password: "pw")

        let r = try XCTUnwrap(StubProtocol.requests.first)
        XCTAssertEqual(r.httpMethod, "POST")
        XCTAssertEqual(r.url?.path, "/sms/api/v1/sp/d10e4bfb0153496e8e8bb955f7ebe413/user/login")
        XCTAssertEqual(r.url?.query, "system=CS")
        XCTAssertEqual(r.value(forHTTPHeaderField: "App-Id"), "com.hero.iot")
        XCTAssertEqual(r.value(forHTTPHeaderField: "Source-Device-Id"), "install-1")
        XCTAssertEqual(body(r)["email"] as? String, "me@example.com")
        XCTAssertEqual(client.tokens?.accessToken, "A1")
        XCTAssertEqual(client.tokens?.refreshToken, "R1")
        XCTAssertEqual(client.tokens?.userUUID, "U1")
    }

    func testWrongPasswordIsAnAuthError() async {
        StubProtocol.handler = { _ in (400, #"{"message":"Invalid credentials"}"#) }
        do {
            try await client.signIn(email: "me@example.com", password: "bad")
            XCTFail("expected an error")
        } catch {
            XCTAssertEqual(error as? QuboError, .auth("Qubo: Invalid credentials"))
        }
        XCTAssertNil(client.tokens)
    }

    func testPhoneSignInFinalizesToLongLivedSession() async throws {
        StubProtocol.handler = { r in
            switch r.url!.path {
            case let p where p.hasSuffix("/user/phone/login"): return (200, #"{"uuid":"S1"}"#)
            case let p where p.hasSuffix("/user/phone/verify"):
                return (200, #"[{"token":"T1","uuid":"U1","email":"me@example.com"}]"#)
            case let p where p.hasSuffix("/user/emailprofile/login"):
                return (200, #"{"accessToken":"A2","refreshToken":"R2"}"#)
            default: return (404, "")
            }
        }
        try await client.requestPhoneCode(phone: "9876543210")
        try await client.verifyPhone(phone: "9876543210", code: "123456")

        XCTAssertEqual(body(StubProtocol.requests[1])["uuid"] as? String, "S1")
        XCTAssertEqual(body(StubProtocol.requests[1])["isdCode"] as? String, "+91")
        XCTAssertEqual(StubProtocol.requests[2].value(forHTTPHeaderField: "Subscriber-Key"), "T1")
        XCTAssertEqual(client.tokens?.accessToken, "A2")
        XCTAssertEqual(client.tokens?.refreshToken, "R2")
        XCTAssertEqual(client.tokens?.userUUID, "U1")
    }

    func testListsOnlyPurifiersAcrossUnits() async throws {
        client = QuboClient(tokens: QuboTokens(accessToken: "A", refreshToken: "R", userUUID: "U", expiresAt: future),
                            installID: "i", session: sessionFromSetUp())
        StubProtocol.handler = { r in
            if r.url!.path.hasSuffix("/units") { return (200, #"[{"unitUUID":"home"}]"#) }
            return (200, """
            {"data":[
              {"deviceUUID":"p1","deviceName":"Bedroom","deviceModel":"HPH03","entityUUID":"e1"},
              {"deviceUUID":"plug","deviceName":"Lamp","deviceModel":"HSP01"},
              {"deviceUUID":"p2","name":"Hall","type":"AIR_PURIFIER","isOnline":false}
            ]}
            """)
        }
        let devices = try await client.purifiers()
        XCTAssertEqual(devices.map(\.id), ["p1", "p2"])
        XCTAssertEqual(devices[0].unitUUID, "home")
        XCTAssertEqual(devices[0].entityUUID, "e1")
        XCTAssertFalse(devices[1].isOnline)
        let auth = StubProtocol.requests[0]
        XCTAssertEqual(auth.value(forHTTPHeaderField: "Authorization"), "Bearer A")
        XCTAssertEqual(auth.value(forHTTPHeaderField: "User-UUID"), "U")
    }

    func testSetFanSpeedPutsShadowAttribute() async throws {
        client = QuboClient(tokens: QuboTokens(accessToken: "A", refreshToken: "R", userUUID: "U", expiresAt: future),
                            installID: "i", session: sessionFromSetUp())
        StubProtocol.handler = { _ in (200, "{}") }
        let device = QuboDevice(id: "p1", name: "Bedroom", model: "HPH03", type: "", unitUUID: "home",
                                entityUUID: "e1", isOnline: true)
        try await client.setFanSpeed(7, on: device)  // clamped to 3

        let r = try XCTUnwrap(StubProtocol.requests.first)
        XCTAssertEqual(r.httpMethod, "PUT")
        XCTAssertTrue(r.url!.path.hasSuffix("/units/home/entities/e1/devices/p1/shadow/attribute"))
        XCTAssertEqual(body(r)["serviceName"] as? String, "fanSpeedControl")
        XCTAssertEqual(body(r)["attributeName"] as? String, "speed")
        XCTAssertEqual(body(r)["attributeValue"] as? String, "3")
    }

    func testRefreshesOnceAfter401() async throws {
        client = QuboClient(tokens: QuboTokens(accessToken: "OLD", refreshToken: "R", userUUID: "U", expiresAt: future),
                            installID: "i", session: sessionFromSetUp())
        var calls = 0
        StubProtocol.handler = { r in
            if r.url!.path.hasSuffix("/auth/refresh") { return (200, #"{"accessToken":"NEW"}"#) }
            calls += 1
            return calls == 1 ? (401, "") : (200, #"{"services":[]}"#)
        }
        let device = QuboDevice(id: "p1", name: "", model: "HPH03", type: "", unitUUID: "h", entityUUID: "", isOnline: true)
        _ = try await client.state(of: device)

        XCTAssertEqual(StubProtocol.requests.count, 3)
        XCTAssertEqual(StubProtocol.requests[2].value(forHTTPHeaderField: "Authorization"), "Bearer NEW")
        XCTAssertEqual(client.tokens?.refreshToken, "R", "keeps the old refresh token when none is returned")
        XCTAssertTrue(StubProtocol.requests[0].url!.path.hasSuffix("/entities/p1/devices/p1/shadow"),
                      "falls back to the device id when there is no entity")
    }

    func testExpiredSessionWithoutRefreshTokenSignsOut() async {
        client = QuboClient(tokens: QuboTokens(accessToken: "A", refreshToken: nil, userUUID: "U", expiresAt: future),
                            installID: "i", session: sessionFromSetUp())
        StubProtocol.handler = { _ in (401, "") }
        let device = QuboDevice(id: "p1", name: "", model: "HPH03", type: "", unitUUID: "h", entityUUID: "e", isOnline: true)
        do {
            _ = try await client.state(of: device)
            XCTFail("expected an error")
        } catch {
            XCTAssertEqual(error as? QuboError, .auth("Qubo rejected the sign-in (HTTP 401). Please sign in again."))
        }
    }

    private func sessionFromSetUp() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return URLSession(configuration: config)
    }
}
