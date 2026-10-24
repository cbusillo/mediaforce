import AppKit
import Foundation
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
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = MainWindowModel(client: MediaforceClient(baseURL: ServerConnection.localServer))
    private var attention: AttentionCenter?

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !Self.isRunningTests else { return }
        if Bundle.main.bundleURL.path.hasPrefix("/Applications/") {
            try? SMAppService.mainApp.register()
        }
        let attention = AttentionCenter()
        attention.requestPermission()
        model.onFacts = { attention.review($0) }
        model.startWatching()
        self.attention = attention
    }

    private static var isRunningTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }
}
