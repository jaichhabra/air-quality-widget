import AirQualityCore
import AppKit
import SwiftUI

let qGreen = Color(red: 48 / 255, green: 209 / 255, blue: 88 / 255)

/// AQI 0–500 colour scale (Apple system colours).
let aqiStops: [Color] = [
    Color(red: 48 / 255, green: 209 / 255, blue: 88 / 255),
    Color(red: 1, green: 214 / 255, blue: 10 / 255),
    Color(red: 1, green: 159 / 255, blue: 10 / 255),
    Color(red: 1, green: 69 / 255, blue: 58 / 255),
    Color(red: 191 / 255, green: 90 / 255, blue: 242 / 255),
    Color(red: 162 / 255, green: 56 / 255, blue: 79 / 255),
]

func aqiColor(_ value: Int) -> Color {
    let t = AQI.scalePosition(value) * Double(aqiStops.count - 1)
    return aqiStops[min(aqiStops.count - 1, Int(t.rounded()))]
}

struct WidgetView: View {
    static let size = NSSize(width: 348, height: 224)
    @EnvironmentObject var store: AppStore
    @State private var showControls = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 0) {
                inside.frame(maxWidth: .infinity, alignment: .leading)
                Rectangle().fill(.white.opacity(0.12)).frame(width: 0.5).padding(.horizontal, 18)
                outside.frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 138, alignment: .top)
            .padding(.top, 18)
            .padding(.horizontal, 22)

            Rectangle().fill(.white.opacity(0.12)).frame(height: 0.5).padding(.horizontal, 22)
            footer.padding(.horizontal, 20).frame(maxHeight: .infinity)
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .foregroundStyle(.white)
        .background(GlassBackground())
        .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .strokeBorder(LinearGradient(colors: [.white.opacity(0.32), .white.opacity(0.06)],
                                             startPoint: .top, endPoint: .bottom), lineWidth: 1))
        .environment(\.colorScheme, .dark)
        .contentShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
        .gesture(DragGesture(minimumDistance: 3)
            .onChanged { _ in store.dragChanged() }
            .onEnded { _ in store.dragEnded() })
        .contextMenu { contextMenu }
    }

    // MARK: inside

    private var inside: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: showRoomMenu) {
                HeaderLabel(icon: "house.fill", title: store.roomName, chevron: true)
            }
            .buttonStyle(HoverPillStyle())
            .padding(.leading, -6)
            .help("Switch purifier")

            ReadingView(pm: store.state?.pm25.map(Double.init),
                        scale: store.scale,
                        placeholder: store.insideError ?? (store.signedIn ? "Loading…" : "Not signed in"),
                        caption1: store.state?.pm25.map { "PM2.5 \($0) µg/m³" } ?? "",
                        caption2: store.insideError == nil ? store.insideCaption : "")
        }
    }

    private func showRoomMenu() {
        guard store.signedIn else { return store.openSignIn() }
        var items: [MenuItem] = store.purifiers.map { d in
            MenuItem(title: store.name(d), checked: d.id == store.selected?.id) { store.select(d) }
        }
        if items.isEmpty { items.append(MenuItem(title: "No Qubo purifiers found", enabled: false)) }
        items.append(.separator)
        if let d = store.selected {
            items.append(MenuItem(title: "Rename “\(store.name(d))”…") { store.renameSelected() })
        }
        items.append(MenuItem(title: "Reload purifiers") { Task { await store.loadPurifiers() } })
        popUpMenu(items)
    }

    // MARK: outside

    private var outside: some View {
        VStack(alignment: .leading, spacing: 0) {
            HeaderLabel(icon: "location.fill", title: "Outside", chevron: false)
                .frame(height: 22, alignment: .leading)
            ReadingView(pm: store.outside?.pm25,
                        scale: store.scale,
                        placeholder: store.outsideFailed ? "Unavailable" : "Loading…",
                        caption1: store.outside.map { "PM2.5 \(Int($0.pm25.rounded())) µg/m³" } ?? "",
                        caption2: store.outside?.source ?? "")
        }
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { store.openReference() }
        .help("Double-click to open your AQI reference page")
    }

    // MARK: footer

    private var footer: some View {
        let s = store.state
        let on = s?.isOn ?? false
        return HStack(spacing: 8) {
            Button(action: store.togglePower) {
                Image(systemName: "power")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 28, height: 28)
                    .background(Circle().fill(on ? qGreen : .white.opacity(0.11)))
                    .overlay(Circle().strokeBorder(.white.opacity(on ? 0 : 0.1), lineWidth: 0.5))
            }
            .buttonStyle(.plain)
            .disabled(s == nil)
            .help(on ? "Turn off" : "Turn on")

            Button { showControls.toggle() } label: {
                HStack(spacing: 6) {
                    Text(modeTitle).font(.system(size: 13, weight: .semibold))
                    Image(systemName: "chevron.down").font(.system(size: 8, weight: .bold)).opacity(0.6)
                }
                .padding(.horizontal, 14)
                .frame(height: 28)
                .background(Capsule().fill(.white.opacity(showControls ? 0.2 : 0.11)))
                .overlay(Capsule().strokeBorder(.white.opacity(0.1), lineWidth: 0.5))
            }
            .buttonStyle(.plain)
            .disabled(s == nil)
            .popover(isPresented: $showControls, arrowEdge: .bottom) {
                ControlsPopover().environmentObject(store)
            }

            Spacer(minLength: 4)

            if let u = store.updated {
                (Text("Updated ") + Text(u, style: .time))
                    .font(.system(size: 11))
                    .foregroundStyle(.white.opacity(0.42))
                    .lineLimit(1)
            }
            IconButton(symbol: "arrow.clockwise", help: "Refresh") { store.refreshAll() }
            IconButton(symbol: "xmark", help: "Quit") { NSApp.terminate(nil) }
        }
    }

    private var modeTitle: String {
        guard let s = store.state else { return "—" }
        return (s.mode ?? "—").capitalized
    }

    // MARK: right-click

    @ViewBuilder private var contextMenu: some View {
        Button("Refresh") { store.refreshAll() }
        Picker("AQI scale", selection: $store.scale) {
            ForEach(AQIScale.allCases) { Text($0.title).tag($0) }
        }
        Button("Settings…") { store.openSettings() }
        Button("Open AQI reference page") { store.openReference() }
        Toggle("Open at login", isOn: $store.launchAtLogin)
        Divider()
        if store.signedIn {
            Button("Sign out of Qubo") { store.signOut() }
        } else {
            Button("Sign in to Qubo…") { store.openSignIn() }
        }
        Button("Quit Air Quality") { NSApp.terminate(nil) }
    }
}

// MARK: - Pieces

struct ReadingView: View {
    let pm: Double?
    let scale: AQIScale
    let placeholder: String
    let caption1: String
    let caption2: String

    var body: some View {
        let r = pm.map { AQI.reading(pm25: $0, scale: scale) }
        VStack(alignment: .leading, spacing: 0) {
            Text(r.map { String($0.value) } ?? "—")
                .font(.system(size: 44, weight: .semibold))
                .monospacedDigit()
                .lineLimit(1)
                .padding(.top, 2)
            Text(r?.category ?? placeholder)
                .font(.system(size: 14, weight: .semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            AQIBar(value: r?.value).padding(.top, 9)
            Text(caption1)
                .font(.system(size: 11))
                .foregroundStyle(.white.opacity(0.62))
                .padding(.top, 9)
            Text(caption2)
                .font(.system(size: 11))
                .foregroundStyle(.white.opacity(0.42))
                .lineLimit(1)
        }
    }
}

struct AQIBar: View {
    let value: Int?

    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(LinearGradient(colors: aqiStops, startPoint: .leading, endPoint: .trailing))
                    .opacity(value == nil ? 0.35 : 0.95)
                    .frame(height: 5)
                if let v = value {
                    Circle()
                        .fill(.white)
                        .overlay(Circle().fill(aqiColor(v)).padding(3.8))
                        .frame(width: 14, height: 14)
                        .shadow(color: .black.opacity(0.3), radius: 1.5, y: 1)
                        .offset(x: (g.size.width - 14) * AQI.scalePosition(v))
                }
            }
            .frame(height: 14)
        }
        .frame(height: 14)
    }
}

struct HeaderLabel: View {
    let icon: String
    let title: String
    let chevron: Bool

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: icon).font(.system(size: 9.5, weight: .semibold))
            Text(title.uppercased())
                .font(.system(size: 10.5, weight: .semibold))
                .tracking(0.9)
                .lineLimit(1)
            if chevron {
                Image(systemName: "chevron.down").font(.system(size: 7, weight: .bold))
            }
        }
        .foregroundStyle(.white.opacity(0.55))
    }
}

struct HoverPillStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        HoverPill(configuration: configuration)
    }

    struct HoverPill: View {
        let configuration: ButtonStyleConfiguration
        @State private var hover = false

        var body: some View {
            configuration.label
                .brightness(hover ? 0.35 : 0)
                .padding(.horizontal, 6)
                .frame(height: 22)
                .background(Capsule().fill(.white.opacity(configuration.isPressed ? 0.18 : hover ? 0.11 : 0)))
                .contentShape(Capsule())
                .onHover { hover = $0 }
        }
    }
}

struct IconButton: View {
    let symbol: String
    let help: String
    let action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.white.opacity(hover ? 1 : 0.62))
                .frame(width: 26, height: 26)
                .background(Circle().fill(.white.opacity(hover ? 0.16 : 0)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(help)
    }
}

/// Real macOS glass: blurs whatever is behind the window (the desktop).
struct GlassBackground: View {
    var body: some View {
        ZStack {
            VisualEffect()
            Color.black.opacity(0.12)
            LinearGradient(colors: [.white.opacity(0.10), .clear], startPoint: .top, endPoint: .center)
        }
    }

    struct VisualEffect: NSViewRepresentable {
        func makeNSView(context: Context) -> NSVisualEffectView {
            let v = NSVisualEffectView()
            v.material = .hudWindow
            v.blendingMode = .behindWindow
            v.state = .active
            return v
        }

        func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
    }
}

// MARK: - Native pop-up menu (full control over the label it's attached to)

struct MenuItem {
    var title = ""
    var checked = false
    var enabled = true
    var isSeparator = false
    var action: (() -> Void)?

    init(title: String, checked: Bool = false, enabled: Bool = true, action: (() -> Void)? = nil) {
        self.title = title
        self.checked = checked
        self.enabled = enabled
        self.action = action
    }

    private init(separator: Bool) { isSeparator = separator }
    static let separator = MenuItem(separator: true)
}

private final class MenuTarget: NSObject {
    let run: () -> Void
    init(_ run: @escaping () -> Void) { self.run = run }
    @objc func fire() { run() }
}

@MainActor
func popUpMenu(_ items: [MenuItem]) {
    let menu = NSMenu()
    menu.autoenablesItems = false
    for item in items {
        if item.isSeparator {
            menu.addItem(.separator())
            continue
        }
        let mi = NSMenuItem(title: item.title, action: nil, keyEquivalent: "")
        mi.state = item.checked ? .on : .off
        mi.isEnabled = item.enabled
        if let action = item.action {
            let target = MenuTarget(action)
            mi.target = target
            mi.action = #selector(MenuTarget.fire)
            mi.representedObject = target  // keeps the target alive
        }
        menu.addItem(mi)
    }
    menu.popUp(positioning: nil, at: NSEvent.mouseLocation, in: nil)
}
