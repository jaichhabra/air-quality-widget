import AirQualityCore
import AppKit
import ServiceManagement
import SwiftUI

@MainActor
final class AppStore: ObservableObject {
    static let frameName = "AirQualityWidget"

    // Qubo
    @Published var signedIn: Bool
    @Published var purifiers: [QuboDevice] = []
    @Published var selectedID: String? { didSet { defaults.set(selectedID, forKey: "selectedPurifier") } }
    @Published var state: PurifierState?
    @Published var insideError: String?
    @Published var updated: Date?
    @Published var names: [String: String] { didSet { defaults.set(names, forKey: "names") } }

    // Outside
    @Published var outside: OutdoorReading?
    @Published var outsideFailed = false
    @Published var place: Place { didSet { save(place, "place"); Task { await refreshOutside() } } }
    @Published var waqiToken: String { didSet { defaults.set(waqiToken, forKey: "waqiToken") } }
    @Published var referenceURL: String { didSet { defaults.set(referenceURL, forKey: "referenceURL") } }

    @Published var scale: AQIScale { didSet { defaults.set(scale.rawValue, forKey: "scale") } }

    let client: QuboClient
    private let outdoor = OutdoorAir()
    private let defaults = UserDefaults.standard
    private var timers: [Timer] = []
    private var lastNudge = Date.distantPast
    private var dragStart: (mouse: NSPoint, origin: NSPoint)?

    weak var panel: NSWindow?
    var openSignIn: () -> Void = {}
    var openSettings: () -> Void = {}
    var closeWindow: (String) -> Void = { _ in }

    init() {
        let d = UserDefaults.standard
        let install = d.string(forKey: "installID") ?? UUID().uuidString
        d.set(install, forKey: "installID")
        client = QuboClient(tokens: Keychain.load(), installID: install)
        signedIn = client.tokens != nil
        selectedID = d.string(forKey: "selectedPurifier")
        names = d.dictionary(forKey: "names") as? [String: String] ?? [:]
        place = (d.data(forKey: "place")).flatMap { try? JSONDecoder().decode(Place.self, from: $0) } ?? .newDelhi
        waqiToken = d.string(forKey: "waqiToken") ?? ""
        referenceURL = d.string(forKey: "referenceURL") ?? "https://www.aqi.in/"
        scale = AQIScale(rawValue: d.string(forKey: "scale") ?? "") ?? .us

        client.onTokensChanged = { [weak self] tokens in
            Keychain.save(tokens)
            Task { @MainActor in self?.signedIn = tokens != nil }
        }
    }

    private func save<T: Encodable>(_ value: T, _ key: String) {
        defaults.set(try? JSONEncoder().encode(value), forKey: key)
    }

    // MARK: lifecycle

    func start() {
        Task {
            await loadPurifiers()
            await refreshOutside()
        }
        // Qubo's cloud updates about once a minute; outdoor data changes slowly.
        timers = [
            Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.refreshInside() }
            },
            Timer.scheduledTimer(withTimeInterval: 600, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.refreshOutside() }
            },
        ]
    }

    func refreshAll() {
        Task {
            lastNudge = .distantPast
            await refreshInside()
            await refreshOutside()
        }
    }

    // MARK: Qubo

    var selected: QuboDevice? {
        purifiers.first { $0.id == selectedID } ?? purifiers.first
    }

    func name(_ d: QuboDevice) -> String { names[d.id] ?? d.name }

    var roomName: String {
        if let d = selected { return name(d) }
        return signedIn ? "Inside" : "Sign in"
    }

    func loadPurifiers() async {
        guard signedIn else { return }
        do {
            purifiers = try await client.purifiers()
            if selected?.id != selectedID { selectedID = selected?.id }
            insideError = purifiers.isEmpty ? "No Qubo purifiers in this account" : nil
            await refreshInside()
        } catch {
            insideError = message(error)
        }
    }

    func select(_ d: QuboDevice) {
        guard d.id != selected?.id else { return }
        selectedID = d.id
        state = nil
        insideError = nil
        lastNudge = .distantPast
        Task { await refreshInside() }
    }

    func refreshInside() async {
        guard signedIn, let d = selected else { return }
        if Date().timeIntervalSince(lastNudge) > 300 {  // ask for a fresh PM2.5 reading now and then
            lastNudge = Date()
            await client.requestFreshReading(d)
        }
        do {
            let s = try await client.state(of: d)
            guard d.id == selected?.id else { return }
            state = s
            insideError = d.isOnline ? nil : "Purifier is offline"
            updated = Date()
        } catch {
            insideError = message(error)
        }
    }

    var insideCaption: String {
        guard let s = state else { return "" }
        if !s.isOn { return "Purifier is off" }
        if s.mode == PurifierMode.manual.rawValue, let f = s.fanSpeed { return "Purifier · Fan \(f)" }
        return "Purifier · " + (s.mode ?? "").capitalized
    }

    func togglePower() {
        guard let d = selected, let s = state else { return }
        let on = !s.isOn
        state?.isOn = on
        perform { try await self.client.setPower(on, on: d) }
    }

    func setMode(_ mode: PurifierMode) {
        guard let d = selected, state != nil else { return }
        state?.mode = mode.rawValue
        perform { try await self.client.setMode(mode, on: d) }
    }

    /// Fan speed applies in Manual mode, so switch to it as well.
    func setFanSpeed(_ speed: Int) {
        guard let d = selected, let s = state else { return }
        let switchMode = s.mode != PurifierMode.manual.rawValue
        state?.fanSpeed = speed
        state?.mode = PurifierMode.manual.rawValue
        perform {
            if switchMode { try await self.client.setMode(.manual, on: d) }
            try await self.client.setFanSpeed(speed, on: d)
        }
    }

    func setChildLock(_ on: Bool) {
        guard let d = selected, state != nil else { return }
        state?.childLock = on
        perform { try await self.client.setChildLock(on, on: d) }
    }

    private func perform(_ op: @escaping () async throws -> Void) {
        Task {
            do {
                try await op()
                try? await Task.sleep(nanoseconds: 2_000_000_000)  // let the cloud catch up
            } catch {
                insideError = message(error)
            }
            await refreshInside()
        }
    }

    func signOut() {
        client.signOut()
        purifiers = []
        state = nil
        updated = nil
        openSignIn()
    }

    func renameSelected() {
        guard let d = selected else { return }
        let alert = NSAlert()
        alert.messageText = "Rename purifier"
        alert.informativeText = "Shown on the widget only. The name in the Qubo app stays the same."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        field.stringValue = name(d)
        alert.accessoryView = field
        alert.addButton(withTitle: "Rename")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            let t = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            names[d.id] = t.isEmpty ? nil : t
        }
    }

    // MARK: outside

    func refreshOutside() async {
        do {
            outside = try await outdoor.reading(at: place, waqiToken: waqiToken)
            outsideFailed = false
        } catch {
            outsideFailed = true
        }
    }

    func searchPlaces(_ q: String) async -> [Place] {
        (try? await outdoor.search(q)) ?? []
    }

    func openReference() {
        if let url = URL(string: referenceURL), url.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: window

    /// Drag the widget from anywhere that isn't a control.
    func dragChanged() {
        guard let panel else { return }
        let mouse = NSEvent.mouseLocation
        if dragStart == nil { dragStart = (mouse, panel.frame.origin) }
        guard let s = dragStart else { return }
        panel.setFrameOrigin(NSPoint(x: s.origin.x + mouse.x - s.mouse.x, y: s.origin.y + mouse.y - s.mouse.y))
    }

    func dragEnded() {
        dragStart = nil
        panel?.saveFrame(usingName: Self.frameName)
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                NSSound.beep()
            }
            objectWillChange.send()
        }
    }

    func message(_ error: Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}
