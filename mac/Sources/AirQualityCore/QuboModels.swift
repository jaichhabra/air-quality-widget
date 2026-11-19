import Foundation

public struct QuboTokens: Codable, Equatable {
    public var accessToken: String
    public var refreshToken: String?
    public var userUUID: String
    public var expiresAt: Date

    public init(accessToken: String, refreshToken: String?, userUUID: String, expiresAt: Date) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.userUUID = userUUID
        self.expiresAt = expiresAt
    }
}

/// A Qubo appliance as listed in the account.
public struct QuboDevice: Codable, Equatable, Identifiable, Hashable {
    public var id: String          // device UUID
    public var name: String
    public var model: String
    public var type: String
    public var unitUUID: String
    public var entityUUID: String
    public var isOnline: Bool

    public init(id: String, name: String, model: String, type: String,
                unitUUID: String, entityUUID: String, isOnline: Bool) {
        self.id = id
        self.name = name
        self.model = model
        self.type = type
        self.unitUUID = unitUUID
        self.entityUUID = entityUUID
        self.isOnline = isOnline
    }

    /// Qubo purifiers are the HPH series (Q200 to Q1000).
    public var isPurifier: Bool {
        model.uppercased().hasPrefix("HPH") || type.uppercased().contains("PURIFIER")
    }
}

public enum PurifierMode: String, CaseIterable, Identifiable {
    case auto, sleep, manual

    public var id: String { rawValue }
    public var title: String { rawValue.capitalized }
}

/// What the purifier reports through its cloud "shadow".
public struct PurifierState: Equatable {
    public var isOn: Bool
    public var mode: String?
    public var fanSpeed: Int?        // 1…3
    public var pm25: Int?
    public var filterLife: Int?      // %
    public var childLock: Bool

    public init(isOn: Bool, mode: String?, fanSpeed: Int?, pm25: Int?, filterLife: Int?, childLock: Bool) {
        self.isOn = isOn
        self.mode = mode
        self.fanSpeed = fanSpeed
        self.pm25 = pm25
        self.filterLife = filterLife
        self.childLock = childLock
    }

    /// Parse a shadow response. Handles both the `services` array the hardware uses and the
    /// older `state.reported` dictionary. Key names follow qubopy (MIT).
    public static func parse(shadow: Any) -> PurifierState {
        let r = QuboShadow.flatten(shadow)
        return PurifierState(
            isOn: QuboShadow.bool(QuboShadow.first(r, "lcSwitchControl_power", "state", "power", "power_state")) ?? false,
            mode: (QuboShadow.first(r, "fanControlMode_state", "mode", "preset_mode")).map { "\($0)".lowercased() },
            fanSpeed: QuboShadow.int(QuboShadow.first(r, "fanSpeedControl_speed", "speed", "fan_speed", "fanSpeed")),
            pm25: QuboShadow.int(QuboShadow.first(r, "aqiStatus_PM25", "pm2_5", "pm25", "aqi")),
            filterLife: QuboShadow.int(QuboShadow.first(r, "filterReset_percentage", "filter_life", "filter_life_percent")),
            childLock: QuboShadow.bool(QuboShadow.first(r, "childLockControl_state", "child_lock", "childLock")) ?? false
        )
    }
}

enum QuboShadow {
    /// Flatten `services[].attributes` into "service_attribute" keys, plus any `state.reported`.
    static func flatten(_ resp: Any) -> [String: Any] {
        guard let d = resp as? [String: Any] else { return [:] }
        var out: [String: Any] = [:]
        if let services = d["services"] as? [[String: Any]] {
            for s in services {
                let name = s["service"] as? String ?? ""
                let attrs = s["attributes"] as? [String: Any] ?? [:]
                for (k, v) in attrs {
                    let value: Any = (v as? [String: Any])?["value"] ?? v
                    if !(value is NSNull) { out["\(name)_\(k)"] = value }
                }
            }
        }
        var reported = d["reported"] as? [String: Any]
        if let state = d["state"] as? [String: Any], let rep = state["reported"] as? [String: Any] {
            reported = rep
        }
        for (k, v) in reported ?? [:] { out[k] = v }
        return out
    }

    static func first(_ r: [String: Any], _ keys: String...) -> Any? {
        for k in keys {
            if let v = r[k], !(v is NSNull) { return v }
        }
        return nil
    }

    static func bool(_ v: Any?) -> Bool? {
        if let s = v as? String {
            switch s.trimmingCharacters(in: .whitespaces).lowercased() {
            case "true", "1", "yes", "on", "enable", "enabled": return true
            case "false", "0", "no", "off", "disable", "disabled": return false
            default: return nil
            }
        }
        if let n = v as? NSNumber {
            if n.doubleValue == 1 { return true }
            if n.doubleValue == 0 { return false }
        }
        return nil
    }

    static func int(_ v: Any?) -> Int? {
        if let s = v as? String { return Int(s.trimmingCharacters(in: .whitespaces)) }
        if let n = v as? NSNumber { return n.intValue }
        return nil
    }

    static func string(_ d: [String: Any], _ keys: String...) -> String {
        for k in keys {
            if let v = d[k], !(v is NSNull) {
                let s = "\(v)"
                if !s.isEmpty { return s }
            }
        }
        return ""
    }
}
