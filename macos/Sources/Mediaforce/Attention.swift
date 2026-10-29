import Foundation

struct AttentionNotice: Equatable, Sendable {
    let id: String
    let title: String
    let body: String
    // Where clicking the notification takes the Director.
    var opens: Place?
}

struct AttentionMemory: Codable, Equatable, Sendable {
    var lastFinishedAt = ""
    var waiting: Set<String> = []
    var backupCount = 0
}

struct Attention: Equatable, Sendable {
    static let mostSeasonsNamedSeparately = 3

    let notices: [AttentionNotice]
    let badgeCount: Int
    let memory: AttentionMemory

    static func review(_ facts: MainWindowFacts, remembered: AttentionMemory?) -> Attention {
        let waiting = waitingNotices(facts)
        let memory = AttentionMemory(
            lastFinishedAt: ([remembered?.lastFinishedAt ?? ""] + facts.recentlyFinishedSeasons.compactMap(\.finishedAt)).max() ?? "",
            waiting: Set(waiting.map(\.id)),
            backupCount: facts.originalBackups.count
        )
        guard let remembered else {
            return Attention(notices: [], badgeCount: waitingOnYou(facts), memory: memory)
        }
        var notices = finishedNotices(facts, since: remembered)
        notices += waiting.filter { !remembered.waiting.contains($0.id) }
        if facts.originalBackups.count > remembered.backupCount {
            notices.append(backupsNotice(facts.originalBackups))
        }
        return Attention(notices: notices, badgeCount: waitingOnYou(facts), memory: memory)
    }

    static func waitingOnYou(_ facts: MainWindowFacts) -> Int {
        facts.samples.waitingOnYou.count
            + facts.work.seasonsNeedingYou.count
            + facts.computers.filter(\.needsYou).count
            + (facts.originalBackups.count > 0 ? 1 : 0)
    }

    private static func finishedNotices(_ facts: MainWindowFacts, since remembered: AttentionMemory) -> [AttentionNotice] {
        let newlyFinished = facts.recentlyFinishedSeasons.filter { ($0.finishedAt ?? "") > remembered.lastFinishedAt }
        guard newlyFinished.count > mostSeasonsNamedSeparately else {
            return newlyFinished.map { season in
                AttentionNotice(
                    id: "finished:\(season.prefix)",
                    title: "\(Words.season(season.show, season.season)) is finished",
                    body: Words.finishedNotice(season, keptReasons: keptReasons(facts, season.prefix)),
                    opens: .item(season.prefix)
                )
            }
        }
        let mayHaveMore = newlyFinished.count == facts.recentlyFinishedSeasons.count
        return [AttentionNotice(
            id: "finished:\(newlyFinished.count)",
            title: mayHaveMore ? "At least \(newlyFinished.count) seasons finished" : "\(newlyFinished.count) seasons finished",
            body: "See Finished lately in Mediaforce for what was saved.",
            opens: .library
        )]
    }

    private static func keptReasons(_ facts: MainWindowFacts, _ prefix: String) -> [String] {
        facts.keptOriginal.kept.filter { $0.prefix == prefix }.map(\.reason)
    }

    static func waitingNotices(_ facts: MainWindowFacts) -> [AttentionNotice] {
        let samples = facts.samples.waitingOnYou.map(sampleNotice)
        let computers = facts.computers.filter(\.needsYou).map { computer in
            AttentionNotice(
                id: "computer:\(computer.key):\(computer.attentionSince ?? "")",
                title: "\(computer.name ?? computer.key) needs you",
                body: computer.attentionReason ?? "\(Words.computerState(computer.state)). Open Mediaforce to check it.",
                opens: .computer(computer.key)
            )
        }
        let seasons = facts.work.seasonsNeedingYou.map { season in
            AttentionNotice(
                id: "season:\(season.prefix)",
                title: "\(Words.season(season.showName, season.season)) needs you",
                body: Words.tvSeasonState(season),
                opens: .item(season.prefix)
            )
        }
        return samples + seasons + computers
    }

    private static func sampleNotice(_ sample: MainWindowFacts.Sample) -> AttentionNotice {
        let name = Words.season(sample.show, sample.season)
        if sample.state == "ready_to_compare" {
            return AttentionNotice(
                id: "sample:\(sample.prefix):ready",
                title: "\(name) sample is ready",
                body: "Watch the sample and approve it, and Mediaforce will compress the rest of the season.",
                opens: .sample(sample.prefix)
            )
        }
        if sample.action == "choose_size" {
            return AttentionNotice(
                id: "sample:\(sample.prefix):size",
                title: "\(name) needs a size decision",
                body: "Its sample can't reach the size goal at good quality. Choose a size in Mediaforce.",
                opens: .sample(sample.prefix)
            )
        }
        return AttentionNotice(
            id: "sample:\(sample.prefix):again",
            title: "\(name) sample needs making again",
            body: "Make the sample again in Mediaforce, then watch it.",
            opens: .sample(sample.prefix)
        )
    }

    static func backupsNotice(_ backups: MainWindowFacts.OriginalBackups) -> AttentionNotice {
        AttentionNotice(
            id: "backups",
            title: "Original backups are ready to delete",
            body: "\(Words.count(backups.count, "original backup")), \(Words.size(backups.bytes)). Delete them in Mediaforce when you're ready.",
            opens: .spaceSaved
        )
    }
}
