import Foundation

struct AttentionNotice: Equatable, Sendable {
    let id: String
    let title: String
    let body: String
}

struct AttentionMemory: Codable, Equatable, Sendable {
    var lastFinishedAt = ""
    var finishedCount = 0
    var waiting: Set<String> = []
    var backupCount = 0
}

struct Attention: Equatable, Sendable {
    static let computerStatesNeedingYou: Set<String> = ["password_needed", "identity_changed", "new_name", "remote_login_off"]
    static let mostSeasonsNamedSeparately = 3

    let notices: [AttentionNotice]
    let badgeCount: Int
    let memory: AttentionMemory

    static func review(_ facts: MainWindowFacts, remembered: AttentionMemory?) -> Attention {
        let waiting = waitingNotices(facts)
        let memory = AttentionMemory(
            lastFinishedAt: ([remembered?.lastFinishedAt ?? ""] + facts.recentlyFinishedSeasons.compactMap(\.finishedAt)).max() ?? "",
            finishedCount: facts.spaceSaved.seasonCount,
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
            + facts.computers.filter { computerStatesNeedingYou.contains($0.state) }.count
            + (facts.originalBackups.count > 0 ? 1 : 0)
    }

    private static func finishedNotices(_ facts: MainWindowFacts, since remembered: AttentionMemory) -> [AttentionNotice] {
        let newlyFinished = facts.spaceSaved.seasonCount - remembered.finishedCount
        guard newlyFinished > 0 else { return [] }
        let named = facts.recentlyFinishedSeasons.filter { ($0.finishedAt ?? "") > remembered.lastFinishedAt }
        guard newlyFinished <= mostSeasonsNamedSeparately, named.count >= newlyFinished else {
            return [AttentionNotice(
                id: "finished:\(newlyFinished)",
                title: "\(Words.count(newlyFinished, "season")) finished",
                body: "See Finished lately in Mediaforce for what was saved."
            )]
        }
        return named.prefix(newlyFinished).map { season in
            AttentionNotice(
                id: "finished:\(season.prefix)",
                title: "\(Words.season(season.show, season.season)) is finished",
                body: "\(Words.count(season.episodeCount, "episode")) compressed, checked and replaced. \(Words.size(season.bytesSaved)) saved."
            )
        }
    }

    private static func waitingNotices(_ facts: MainWindowFacts) -> [AttentionNotice] {
        let samples = facts.samples.waitingOnYou.map(sampleNotice)
        let computers = facts.computers.filter { computerStatesNeedingYou.contains($0.state) }.map { computer in
            AttentionNotice(
                id: "computer:\(computer.key):\(computer.state)",
                title: "\(computer.name ?? computer.key) needs you",
                body: "\(Words.computerState(computer.state)). Open Mediaforce to fix it."
            )
        }
        return samples + computers
    }

    private static func sampleNotice(_ sample: MainWindowFacts.Sample) -> AttentionNotice {
        let name = Words.season(sample.show, sample.season)
        if sample.state == "ready_to_compare" {
            return AttentionNotice(
                id: "sample:\(sample.prefix):ready",
                title: "\(name) sample is ready",
                body: "Watch the sample and approve it, and Mediaforce will compress the rest of the season."
            )
        }
        if sample.action == "choose_size" {
            return AttentionNotice(
                id: "sample:\(sample.prefix):size",
                title: "\(name) needs a size decision",
                body: "Its sample can't reach the size goal at good quality. Choose a size in Mediaforce."
            )
        }
        return AttentionNotice(
            id: "sample:\(sample.prefix):again",
            title: "\(name) sample needs making again",
            body: "Make the sample again in Mediaforce, then watch it."
        )
    }

    private static func backupsNotice(_ backups: MainWindowFacts.OriginalBackups) -> AttentionNotice {
        AttentionNotice(
            id: "backups",
            title: "Original backups are ready to delete",
            body: "\(Words.count(backups.count, "original backup")), \(Words.size(backups.bytes)). Delete them in Mediaforce when you're ready."
        )
    }
}
