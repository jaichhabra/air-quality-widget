import Foundation

public struct Place: Codable, Equatable, Identifiable, Hashable {
    public var name: String
    public var detail: String
    public var latitude: Double
    public var longitude: Double

    public var id: String { "\(latitude),\(longitude)" }

    public init(name: String, detail: String, latitude: Double, longitude: Double) {
        self.name = name
        self.detail = detail
        self.latitude = latitude
        self.longitude = longitude
    }

    public static let newDelhi = Place(name: "New Delhi", detail: "Delhi, India", latitude: 28.6139, longitude: 77.2090)
}

public struct OutdoorReading: Equatable {
    public let pm25: Double
    public let source: String   // "Station name" or "Place · estimate"
}

/// Outdoor PM2.5: the nearest official station via WAQI when a token is set,
/// otherwise Open-Meteo's modelled value for the exact location (no key needed).
public struct OutdoorAir {
    let session: URLSession

    public init(session: URLSession = .shared) { self.session = session }

    public func reading(at place: Place, waqiToken: String?) async throws -> OutdoorReading {
        if let token = waqiToken, !token.isEmpty,
           let url = URL(string: "https://api.waqi.info/feed/geo:\(place.latitude);\(place.longitude)/?token=\(token)") {
            let (data, _) = try await session.data(from: url)
            if let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               d["status"] as? String == "ok",
               let body = d["data"] as? [String: Any],
               let iaqi = body["iaqi"] as? [String: Any],
               let pm = (iaqi["pm25"] as? [String: Any])?["v"] as? NSNumber {
                let city = ((body["city"] as? [String: Any])?["name"] as? String ?? place.name)
                    .components(separatedBy: ",").first ?? place.name
                return OutdoorReading(pm25: AQI.pm25(fromUSIndex: pm.doubleValue), source: city)
            }
        }
        var c = URLComponents(string: "https://air-quality-api.open-meteo.com/v1/air-quality")!
        c.queryItems = [URLQueryItem(name: "latitude", value: String(place.latitude)),
                        URLQueryItem(name: "longitude", value: String(place.longitude)),
                        URLQueryItem(name: "current", value: "pm2_5")]
        let (data, _) = try await session.data(from: c.url!)
        guard let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let pm = (d["current"] as? [String: Any])?["pm2_5"] as? NSNumber else {
            throw QuboError.badResponse("Open-Meteo")
        }
        return OutdoorReading(pm25: pm.doubleValue, source: "\(place.name) · estimate")
    }

    /// Place search for the Outside location (Open-Meteo geocoding, no key needed).
    public func search(_ query: String) async throws -> [Place] {
        var c = URLComponents(string: "https://geocoding-api.open-meteo.com/v1/search")!
        c.queryItems = [URLQueryItem(name: "name", value: query), URLQueryItem(name: "count", value: "8")]
        let (data, _) = try await session.data(from: c.url!)
        guard let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let results = d["results"] as? [[String: Any]] else { return [] }
        return results.compactMap { r in
            guard let lat = r["latitude"] as? NSNumber, let lon = r["longitude"] as? NSNumber,
                  let name = r["name"] as? String else { return nil }
            let detail = [r["admin1"] as? String, r["country"] as? String].compactMap { $0 }.joined(separator: ", ")
            return Place(name: name, detail: detail, latitude: lat.doubleValue, longitude: lon.doubleValue)
        }
    }
}
