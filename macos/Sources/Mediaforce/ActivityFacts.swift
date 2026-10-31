import Foundation

struct ActivityFacts: Decodable, Sendable {
    let startsAt: String
    let endsAt: String
    let computers: [Computer]
    let linedUp: [File]
    let earlierHolds: [File]
    let nextSeason: MainWindowFacts.Season?

    struct Computer: Decodable, Sendable, Identifiable {
        let key: String
        let windows: [Window]
        let running: [File]
        let finishCurrentFile: Bool
        var id: String { key }
    }

    struct Window: Decodable, Sendable {
        let startsAt: String
        let endsAt: String
    }

    struct File: Decodable, Sendable, Identifiable {
        let id: String
        let prefix: String
        let showPrefix: String
        let show: String
        let title: String
        let startsAt: String?
        let finishesAt: String?
        let computerKey: String?
        let computerName: String?
        let waiting: String?
        let progress: Double?

    }

    func fraction(_ timestamp: String) -> Double? {
        guard let date = Self.date(timestamp), let start = Self.date(startsAt), let end = Self.date(endsAt), end > start else { return nil }
        return min(1, max(0, date.timeIntervalSince(start) / end.timeIntervalSince(start)))
    }

    static func date(_ timestamp: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: timestamp) ?? ISO8601DateFormatter().date(from: timestamp)
    }

    static func time(_ timestamp: String?) -> String? {
        guard let timestamp, let date = date(timestamp) else { return nil }
        return date.formatted(.dateTime.weekday(.abbreviated).hour(.defaultDigits(amPM: .abbreviated)).minute())
    }
}
