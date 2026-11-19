import Foundation

public enum AQIScale: String, CaseIterable, Codable, Identifiable {
    case us, india

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .us: return "US AQI (like aqi.in)"
        case .india: return "India NAQI (CPCB)"
        }
    }
}

public struct AQIReading: Equatable {
    public let value: Int
    public let category: String
}

/// AQI computed from PM2.5 only, from live readings.
public enum AQI {
    typealias Band = (cLo: Double, cHi: Double, iLo: Double, iHi: Double, label: String)

    /// US EPA (2024) PM2.5 breakpoints.
    static let usBands: [Band] = [
        (0.0, 9.0, 0, 50, "Good"),
        (9.1, 35.4, 51, 100, "Moderate"),
        (35.5, 55.4, 101, 150, "Unhealthy for Some"),
        (55.5, 125.4, 151, 200, "Unhealthy"),
        (125.5, 225.4, 201, 300, "Very Unhealthy"),
        (225.5, 325.4, 301, 500, "Hazardous"),
    ]

    /// India NAQI (CPCB) PM2.5 sub-index.
    static let indiaBands: [Band] = [
        (0, 30, 0, 50, "Good"),
        (30, 60, 51, 100, "Satisfactory"),
        (60, 90, 101, 200, "Moderate"),
        (90, 120, 201, 300, "Poor"),
        (120, 250, 301, 400, "Very Poor"),
        (250, 600, 401, 500, "Severe"),
    ]

    public static func reading(pm25: Double, scale: AQIScale) -> AQIReading {
        let pm = max(0, pm25)
        let bands = scale == .us ? usBands : indiaBands
        let slack = scale == .us ? 0.05 : 0  // US bands have 0.1 gaps between them
        for b in bands where pm <= b.cHi + slack {
            let c = max(pm, b.cLo)
            let v = b.iLo + (c - b.cLo) * (b.iHi - b.iLo) / (b.cHi - b.cLo)
            return AQIReading(value: Int(v.rounded()), category: b.label)
        }
        return AQIReading(value: 500, category: scale == .us ? "Hazardous" : "Severe")
    }

    /// WAQI reports a US AQI sub-index; turn it back into µg/m³.
    public static func pm25(fromUSIndex idx: Double) -> Double {
        for b in usBands where idx <= b.iHi {
            return b.cLo + (idx - b.iLo) * (b.cHi - b.cLo) / (b.iHi - b.iLo)
        }
        return 325.4
    }

    /// Position on the 0–500 colour scale, 0…1.
    public static func scalePosition(_ value: Int) -> Double {
        min(1, max(0, Double(value) / 500))
    }
}
