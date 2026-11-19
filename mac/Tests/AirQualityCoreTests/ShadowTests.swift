import XCTest
@testable import AirQualityCore

final class ShadowTests: XCTestCase {
    private func json(_ s: String) -> Any {
        try! JSONSerialization.jsonObject(with: Data(s.utf8))
    }

    func testServicesArrayFormat() {
        let shadow = json("""
        {"services": [
          {"service": "lcSwitchControl", "attributes": {"power": {"value": "on"}}},
          {"service": "fanControlMode", "attributes": {"state": {"value": "Manual"}}},
          {"service": "fanSpeedControl", "attributes": {"speed": {"value": "2"}}},
          {"service": "aqiStatus", "attributes": {"PM25": {"value": 87}}},
          {"service": "filterReset", "attributes": {"percentage": {"value": "64"}}},
          {"service": "childLockControl", "attributes": {"state": {"value": "disable"}}}
        ]}
        """)
        XCTAssertEqual(PurifierState.parse(shadow: shadow),
                       PurifierState(isOn: true, mode: "manual", fanSpeed: 2, pm25: 87, filterLife: 64, childLock: false))
    }

    func testLegacyReportedFormat() {
        let shadow = json("""
        {"state": {"reported": {"power": "off", "mode": "auto", "speed": 1, "pm25": "12", "childLock": "enable"}}}
        """)
        XCTAssertEqual(PurifierState.parse(shadow: shadow),
                       PurifierState(isOn: false, mode: "auto", fanSpeed: 1, pm25: 12, filterLife: nil, childLock: true))
    }

    func testMissingValuesAreNil() {
        let s = PurifierState.parse(shadow: json("{}"))
        XCTAssertFalse(s.isOn)
        XCTAssertNil(s.pm25)
        XCTAssertNil(s.mode)
    }

    func testJWTExpiry() {
        // header.payload.signature with payload {"exp": 2000000000}
        let payload = Data(#"{"exp":2000000000}"#.utf8).base64EncodedString()
            .replacingOccurrences(of: "=", with: "")
        let exp = QuboClient.jwtExpiry("eyJhbGciOiJIUzI1NiJ9.\(payload).sig")
        XCTAssertEqual(exp, Date(timeIntervalSince1970: 2_000_000_000))
        XCTAssertNil(QuboClient.jwtExpiry("not-a-jwt"))
    }
}
