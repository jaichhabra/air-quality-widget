import Foundation
import Security

/// Stores the Qubo session in the macOS Keychain (never in plain files or preferences).
public enum Keychain {
    static let service = "AirQuality.QuboSession"

    public static func save(_ tokens: QuboTokens?) {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                   kSecAttrService as String: service,
                                   kSecAttrAccount as String: "qubo"]
        SecItemDelete(base as CFDictionary)
        guard let tokens, let data = try? JSONEncoder().encode(tokens) else { return }
        var add = base
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    public static func load() -> QuboTokens? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: "qubo",
                                    kSecReturnData as String: true,
                                    kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(QuboTokens.self, from: data)
    }
}
