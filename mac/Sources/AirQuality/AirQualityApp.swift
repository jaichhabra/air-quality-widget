import AppKit
import SwiftUI

@main
struct AirQualityApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate

    var body: some Scene {
        Settings { EmptyView() }
    }
}

/// Borderless glass panel that lives on the desktop: above the wallpaper and icons,
/// below every app window, on all Spaces.
final class WidgetPanel: NSPanel {
    init(content: NSView) {
        super.init(contentRect: NSRect(origin: .zero, size: WidgetView.size),
                   styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
        collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenNone]
        hidesOnDeactivate = false
        contentView = content
    }

    override var canBecomeKey: Bool { true }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let store = AppStore()
    private var panel: WidgetPanel?
    private var windows: [String: NSWindow] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)  // no Dock icon, like a widget

        let host = NSHostingView(rootView: WidgetView().environmentObject(store))
        host.frame = NSRect(origin: .zero, size: WidgetView.size)
        let p = WidgetPanel(content: host)
        if !p.setFrameUsingName(AppStore.frameName), let screen = NSScreen.main {
            let v = screen.visibleFrame
            p.setFrameOrigin(NSPoint(x: v.maxX - WidgetView.size.width - 24, y: v.maxY - WidgetView.size.height - 24))
        }
        p.orderFrontRegardless()
        panel = p
        store.panel = p

        store.openSignIn = { [weak self] in self?.showSignIn() }
        store.openSettings = { [weak self] in self?.showSettings() }
        store.closeWindow = { [weak self] id in self?.windows[id]?.close() }
        store.start()
        if !store.signedIn { showSignIn() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func showSignIn() {
        show("signin", title: "Sign in to Qubo", size: NSSize(width: 380, height: 360)) { SignInView() }
    }

    func showSettings() {
        show("settings", title: "Air Quality Settings", size: NSSize(width: 420, height: 440)) { SettingsView() }
    }

    private func show<V: View>(_ id: String, title: String, size: NSSize, @ViewBuilder _ content: () -> V) {
        NSApp.activate(ignoringOtherApps: true)
        if let w = windows[id] {
            w.makeKeyAndOrderFront(nil)
            return
        }
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = title
        w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: content().environmentObject(store))
        w.center()
        w.makeKeyAndOrderFront(nil)
        windows[id] = w
    }
}
