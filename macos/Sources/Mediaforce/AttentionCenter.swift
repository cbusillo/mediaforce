import AppKit
import Foundation
import notify
import ServiceManagement
import UserNotifications

@MainActor
final class AttentionCenter {
    static let memoryKey = "attentionMemory"

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func requestPermission() {
        Task { _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) }
    }

    func review(_ facts: MainWindowFacts) {
        let attention = Attention.review(facts, remembered: rememberedMemory())
        NSApp.dockTile.badgeLabel = attention.badgeCount > 0 ? "\(attention.badgeCount)" : nil
        remember(attention.memory)
        for notice in attention.notices {
            post(notice)
        }
    }

    private func post(_ notice: AttentionNotice) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = .default
        if let opens = notice.opens {
            content.userInfo = [Place.notificationKey: opens.notificationValue]
        }
        let request = UNNotificationRequest(identifier: "\(notice.id):\(UUID().uuidString)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    private func rememberedMemory() -> AttentionMemory? {
        defaults.data(forKey: Self.memoryKey).flatMap { try? JSONDecoder().decode(AttentionMemory.self, from: $0) }
    }

    private func remember(_ memory: AttentionMemory) {
        defaults.set(try? JSONEncoder().encode(memory), forKey: Self.memoryKey)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = MainWindowModel(client: MediaforceClient(baseURL: ServerConnection.localServer))
    private var attention: AttentionCenter?

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !Self.isRunningTests else { return }
        if Bundle.main.bundleURL.path.hasPrefix("/Applications/") {
            try? SMAppService.mainApp.register()
        }
        Task { await Updates.shared.start() }
        // A proof copy never asks for notifications or posts them; they belong to the live app.
        guard !BackgroundService.isProof else {
            Self.chooseCheckForUpdatesOnProofSignal()
            model.startWatching()
            return
        }
        UNUserNotificationCenter.current().delegate = self
        let attention = AttentionCenter()
        attention.requestPermission()
        model.onFacts = { attention.review($0) }
        model.startWatching()
        self.attention = attention
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let value = response.notification.request.content.userInfo[Place.notificationKey] as? String
        await MainActor.run {
            NSApp.activate()
            model.placeRequest = PlaceRequest(place: Attention.openingPlace(model.facts, fallback: value.flatMap(Place.init(notificationValue:)) ?? .home))
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        model.placeRequest = PlaceRequest(place: Attention.openingPlace(model.facts))
        return true
    }

    /// A proof copy chooses Check for Updates… from its own app menu when its driver posts
    /// `notifyutil -p <label>.check-for-updates`, so an update proof needs no click. The live app never listens.
    private static func chooseCheckForUpdatesOnProofSignal() {
        var token: Int32 = 0
        notify_register_dispatch("\(BackgroundService.label).check-for-updates", &token, .main) { _ in
            MainActor.assumeIsolated {
                guard let menu = NSApp.mainMenu?.items.first?.submenu,
                      let index = menu.items.firstIndex(where: { $0.title == Words.checkForUpdates }) else {
                    return Updates.logger.notice("Proof: no Check for Updates… in the app menu")
                }
                // As opening the menu would: SwiftUI brings its items up to date, then AppKit validates them.
                menu.delegate?.menuNeedsUpdate?(menu)
                menu.update()
                let item = menu.items[index]
                let before = menu.items[..<index].last(where: { !$0.isSeparatorItem })?.title ?? ""
                let updater = Updates.shared.canCheckForUpdates
                Updates.logger.notice("Proof: choosing \(item.title, privacy: .public) after \(before, privacy: .public), enabled \(item.isEnabled, privacy: .public), updater can check \(updater, privacy: .public)")
                menu.performActionForItem(at: index)
            }
        }
    }

    private static var isRunningTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }
}
