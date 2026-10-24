import AppKit
import Foundation
import ServiceManagement
import UserNotifications

@MainActor
final class AttentionCenter {
    static let checkInterval: Duration = .seconds(30)
    static let memoryKey = "attentionMemory"

    private let client: MediaforceClient
    private let defaults: UserDefaults

    init(client: MediaforceClient, defaults: UserDefaults = .standard) {
        self.client = client
        self.defaults = defaults
    }

    func start() {
        Task {
            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
            while !Task.isCancelled {
                await check()
                try? await Task.sleep(for: Self.checkInterval)
            }
        }
    }

    func check() async {
        guard let facts = try? await client.facts() else { return }
        let attention = Attention.review(facts, remembered: rememberedMemory())
        NSApp.dockTile.badgeLabel = attention.badgeCount > 0 ? "\(attention.badgeCount)" : nil
        for notice in attention.notices {
            await post(notice)
        }
        remember(attention.memory)
    }

    private func post(_ notice: AttentionNotice) async {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = .default
        let request = UNNotificationRequest(identifier: "\(notice.id):\(UUID().uuidString)", content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
    }

    private func rememberedMemory() -> AttentionMemory? {
        defaults.data(forKey: Self.memoryKey).flatMap { try? JSONDecoder().decode(AttentionMemory.self, from: $0) }
    }

    private func remember(_ memory: AttentionMemory) {
        defaults.set(try? JSONEncoder().encode(memory), forKey: Self.memoryKey)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var attention: AttentionCenter?

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !Self.isRunningTests else { return }
        if Bundle.main.bundleURL.path.hasPrefix("/Applications/") {
            try? SMAppService.mainApp.register()
        }
        let attention = AttentionCenter(client: MediaforceClient(baseURL: ServerConnection.localServer))
        attention.start()
        self.attention = attention
    }

    private static var isRunningTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }
}
