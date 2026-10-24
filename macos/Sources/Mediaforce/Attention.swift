import Foundation

struct AttentionNotice: Equatable, Sendable {
    let id: String
    let title: String
    let body: String
}

struct AttentionMemory: Codable, Equatable, Sendable {
    var finished: Set<String> = []
    var waiting: Set<String> = []
    var backupCount = 0
}

struct Attention: Equatable, Sendable {
    static let computerStatesNeedingYou: Set<String> = ["password_needed", "identity_changed", "new_name", "remote_login_off"]

    let notices: [AttentionNotice]
    let badgeCount: Int
    let memory: AttentionMemory

    static func review(_ facts: MainWindowFacts, remembered: AttentionMemory?) -> Attention {
        let finished = finishedNotices(facts)
        let waiting = waitingNotices(facts)
        let memory = AttentionMemory(
            finished: (remembered?.finished ?? []).union(finished.map(\.id)),
            waiting: Set(waiting.map(\.id)),
            backupCount: facts.originalBackups.count
        )
        guard let remembered else {
            return Attention(notices: [], badgeCount: waitingOnYou(facts), memory: memory)
        }
        var notices = finished.filter { !remembered.finished.contains($0.id) }
        notices += waiting.filter { !remembered.waiting.contains($0.id) }
        if facts.originalBackups.count > remembered.backupCount {
            notices.append(backupsNotice(facts.originalBackups))
        }
        return Attention(notices: notices, badgeCount: waitingOnYou(facts), memory: memory)
    }

    static func waitingOnYou(_ facts: MainWindowFacts) -> Int {
        facts.samples.waitingOnYou.count
            + facts.computers.filter { computerStatesNeedingYou.contains($0.state) }.count
            + (facts.originalBackups.count > 0 ? 1 : 0)
    }

    private static func finishedNotices(_ facts: MainWindowFacts) -> [AttentionNotice] {
        facts.recentlyFinishedSeasons.map { season in
            AttentionNotice(
                id: "finished:\(season.prefix)",
                title: "\(Words.season(season.show, season.season)) is finished",
                body: "\(Words.count(season.episodeCount, "episode")) compressed, checked and replaced. \(Words.size(season.bytesSaved)) saved."
            )
        }
    }

    private static func waitingNotices(_ facts: MainWindowFacts) -> [AttentionNotice] {
        let samples = facts.samples.waitingOnYou.map { sample in
            let name = Words.season(sample.show, sample.season)
            return sample.state == "ready_to_compare"
                ? AttentionNotice(
                    id: "sample:\(sample.prefix):ready",
                    title: "\(name) sample is ready",
                    body: "Watch the sample and approve it, and Mediaforce will compress the rest of the season."
                )
                : AttentionNotice(
                    id: "sample:\(sample.prefix):again",
                    title: "\(name) sample needs making again",
                    body: "Make the sample again in Mediaforce, then watch it."
                )
        }
        let computers = facts.computers.filter { computerStatesNeedingYou.contains($0.state) }.map { computer in
            AttentionNotice(
                id: "computer:\(computer.key):\(computer.state)",
                title: "\(computer.name ?? computer.key) needs you",
                body: "\(Words.computerState(computer.state)). Open Mediaforce to fix it."
            )
        }
        return samples + computers
    }

    private static func backupsNotice(_ backups: MainWindowFacts.OriginalBackups) -> AttentionNotice {
        AttentionNotice(
            id: "backups",
            title: "Original backups are ready to delete",
            body: "\(Words.count(backups.count, "original backup")), \(Words.size(backups.bytes)). Delete them in Mediaforce when you're ready."
        )
    }
}
