import XCTest
@testable import AirQualityCore

final class AQITests: XCTestCase {
    func testUSScaleMatchesEPABreakpoints() {
        XCTAssertEqual(AQI.reading(pm25: 0, scale: .us), AQIReading(value: 0, category: "Good"))
        XCTAssertEqual(AQI.reading(pm25: 9.0, scale: .us).value, 50)
        XCTAssertEqual(AQI.reading(pm25: 66, scale: .us), AQIReading(value: 158, category: "Unhealthy"))
        XCTAssertEqual(AQI.reading(pm25: 132, scale: .us), AQIReading(value: 207, category: "Very Unhealthy"))
        XCTAssertEqual(AQI.reading(pm25: 1000, scale: .us).value, 500)
    }

    func testIndiaScale() {
        XCTAssertEqual(AQI.reading(pm25: 22, scale: .india), AQIReading(value: 37, category: "Good"))
        XCTAssertEqual(AQI.reading(pm25: 66, scale: .india), AQIReading(value: 121, category: "Moderate"))
        XCTAssertEqual(AQI.reading(pm25: 215, scale: .india).category, "Very Poor")
    }

    func testWAQIIndexRoundTrips() {
        // A WAQI/aqi.in reading of 189 (US) is ≈ 110 µg/m³
        let pm = AQI.pm25(fromUSIndex: 189)
        XCTAssertEqual(pm, 109.7, accuracy: 0.2)
        XCTAssertEqual(AQI.reading(pm25: pm, scale: .us).value, 189)
    }
}
