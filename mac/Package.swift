// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AirQuality",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "AirQuality", targets: ["AirQuality"]),
    ],
    targets: [
        // Pure logic: AQI maths, Qubo cloud client, outdoor air quality. Unit-tested in CI.
        .target(name: "AirQualityCore"),
        // The SwiftUI desktop widget.
        .executableTarget(name: "AirQuality", dependencies: ["AirQualityCore"]),
        .testTarget(name: "AirQualityCoreTests", dependencies: ["AirQualityCore"]),
    ]
)
