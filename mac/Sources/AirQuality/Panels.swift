import AirQualityCore
import AppKit
import SwiftUI

/// Popover from the Mode pill: mode, fan speed, child lock, filter.
struct ControlsPopover: View {
    @EnvironmentObject var store: AppStore

    private var mode: Binding<PurifierMode> {
        Binding(get: { PurifierMode(rawValue: store.state?.mode ?? "") ?? .auto },
                set: { store.setMode($0) })
    }

    private var fan: Binding<Int> {
        Binding(get: { store.state?.fanSpeed ?? 1 }, set: { store.setFanSpeed($0) })
    }

    private var lock: Binding<Bool> {
        Binding(get: { store.state?.childLock ?? false }, set: { store.setChildLock($0) })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Mode").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            Picker("Mode", selection: mode) {
                ForEach(PurifierMode.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            Text("Fan speed").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                .padding(.top, 6)
            Picker("Fan speed", selection: fan) {
                ForEach(1...3, id: \.self) { Text("\($0)").tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            if mode.wrappedValue != .manual {
                Text("Changing the speed switches to Manual.")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }

            Divider().padding(.vertical, 4)
            Toggle("Child lock", isOn: lock)
            if let f = store.state?.filterLife {
                HStack {
                    Text("Filter")
                    Spacer()
                    Text("\(f)% left").foregroundStyle(f < 15 ? Color.orange : Color.secondary)
                }
                .font(.system(size: 12))
            }
        }
        .padding(16)
        .frame(width: 250)
    }
}

/// Sign in with the Qubo account: email + password, or phone + SMS code.
struct SignInView: View {
    @EnvironmentObject var store: AppStore

    enum Method: String, CaseIterable, Identifiable {
        case email = "Email", phone = "Phone"
        var id: String { rawValue }
    }

    @State private var method: Method = .email
    @State private var email = ""
    @State private var password = ""
    @State private var isd = "+91"
    @State private var phone = ""
    @State private var code = ""
    @State private var codeSent = false
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: "aqi.medium")
                    .font(.system(size: 26))
                    .foregroundStyle(qGreen)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Connect your Qubo purifier").font(.headline)
                    Text("Use the account from the Qubo app.").font(.subheadline).foregroundStyle(.secondary)
                }
            }

            Picker("Sign in with", selection: $method) {
                ForEach(Method.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            if method == .email {
                TextField("Email", text: $email).textContentType(.username)
                SecureField("Password", text: $password).textContentType(.password)
                    .onSubmit(signInEmail)
            } else {
                HStack {
                    TextField("+91", text: $isd).frame(width: 56)
                    TextField("Phone number", text: $phone)
                }
                if codeSent {
                    TextField("6-digit code from SMS", text: $code).textContentType(.oneTimeCode)
                        .onSubmit(verifyCode)
                }
                Text("Add an email to your Qubo profile to stay signed in. A phone-only sign-in lasts about an hour.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let error {
                Text(error).font(.system(size: 12)).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 0)
            HStack {
                Text("Your password goes only to Qubo. The session is kept in your Keychain.")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                Button(primaryTitle, action: primary)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || !canSubmit)
            }
        }
        .textFieldStyle(.roundedBorder)
        .padding(20)
        .frame(width: 380, height: 360)
    }

    private var primaryTitle: String {
        method == .email ? "Sign In" : (codeSent ? "Verify" : "Send Code")
    }

    private var canSubmit: Bool {
        switch method {
        case .email: return email.contains("@") && !password.isEmpty
        case .phone: return phone.count >= 6 && (!codeSent || code.count >= 4)
        }
    }

    private func primary() {
        if method == .email { signInEmail() } else if codeSent { verifyCode() } else { sendCode() }
    }

    private func signInEmail() {
        run { try await store.client.signIn(email: email.trimmingCharacters(in: .whitespaces), password: password) }
    }

    private func sendCode() {
        busy = true
        error = nil
        Task {
            do {
                try await store.client.requestPhoneCode(phone: digits(phone), isdCode: isd)
                codeSent = true
            } catch {
                self.error = store.message(error)
            }
            busy = false
        }
    }

    private func verifyCode() {
        run { try await store.client.verifyPhone(phone: digits(phone), code: digits(code), isdCode: isd) }
    }

    private func run(_ op: @escaping () async throws -> Void) {
        busy = true
        error = nil
        Task {
            do {
                try await op()
                store.signedIn = true
                await store.loadPurifiers()
                store.closeWindow("signin")
            } catch {
                self.error = store.message(error)
            }
            busy = false
        }
    }

    private func digits(_ s: String) -> String { s.filter(\.isNumber) }
}

/// Outside location, WAQI token, AQI reference page.
struct SettingsView: View {
    @EnvironmentObject var store: AppStore
    @State private var query = ""
    @State private var results: [Place] = []
    @State private var searching = false

    var body: some View {
        Form {
            Section("Outside location") {
                LabeledContent("Current", value: "\(store.place.name)\(store.place.detail.isEmpty ? "" : ", \(store.place.detail)")")
                HStack {
                    TextField("Search a city or area", text: $query).onSubmit(search)
                    Button("Search", action: search).disabled(query.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                if searching { ProgressView().controlSize(.small) }
                ForEach(results) { p in
                    Button {
                        store.place = p
                        results = []
                        query = ""
                    } label: {
                        VStack(alignment: .leading) {
                            Text(p.name)
                            Text(p.detail).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
            Section {
                TextField("WAQI token (optional)", text: $store.waqiToken)
            } header: {
                Text("Station data")
            } footer: {
                Text("With a free token from aqicn.org/data-platform/token, Outside shows the nearest official monitoring station instead of a modelled estimate.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("AQI") {
                Picker("Scale", selection: $store.scale) {
                    ForEach(AQIScale.allCases) { Text($0.title).tag($0) }
                }
                TextField("Reference page (double-click Outside)", text: $store.referenceURL)
            }
        }
        .formStyle(.grouped)
        .frame(width: 420, height: 440)
    }

    private func search() {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        searching = true
        Task {
            results = await store.searchPlaces(q)
            searching = false
        }
    }
}
