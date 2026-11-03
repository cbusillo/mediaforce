import Foundation

struct AttentionNotice: Equatable, Sendable {
    let id: String
    let title: String
    let body: String
    // Where clicking the notification takes the Director.
    var opens: Place?
    var notifies = true
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
        notices += waiting.filter { $0.notifies && !remembered.waiting.contains($0.id) }
        if facts.originalBackups.count > remembered.backupCount {
            notices.append(backupsNotice(facts.originalBackups))
        }
        return Attention(notices: notices, badgeCount: waitingOnYou(facts), memory: memory)
    }

    static func waitingOnYou(_ facts: MainWindowFacts) -> Int {
        waitingItems(facts).filter(\.notifies).count
    }

    static func waitingItems(_ facts: MainWindowFacts) -> [AttentionNotice] {
        waitingNotices(facts) + (facts.originalBackups.count > 0 ? [backupsNotice(facts.originalBackups)] : [])
    }

    static func openingPlace(_ facts: MainWindowFacts?, fallback: Place = .library) -> Place {
        guard let facts else { return .needsYou }
        return waitingOnYou(facts) > 0 ? .needsYou : fallback
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
        let kinds = facts.work.kindsNeedingYou.map { kind in
            AttentionNotice(
                id: "kind:\(kind.kind):\(kindNoticeStage(kind))",
                title: Words.kindName(kind.kind),
                body: kindWaitingReason(kind),
                opens: kindPlace(kind),
                notifies: kindNoticeStage(kind) != "preparing"
            )
        }
        let samples = facts.samples.waitingOnYou.map { sample in
            var notice = sampleNotice(sample)
            notice.notifies = !facts.work.kindsNeedingYou.contains { $0.sampleSeason == sample.prefix }
            return notice
        }
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
                id: "season:\(season.prefix):\(season.state ?? ""):\(season.action ?? "")",
                title: "\(Words.season(season.showName, season.season)) needs you",
                body: Words.tvSeasonState(season),
                opens: .item(season.prefix)
            )
        }
        let originals = (facts.originalPreviews ?? []).map { preview in
            let waitingOnly = preview.waiting != nil && preview.problem == nil
            let title = waitingOnly ? "Original-file checks are waiting" : preview.problem == nil ? "Original files need your confirmation" : "Original-file checks need attention"
            return AttentionNotice(id: "original:\(preview.id)\(waitingOnly ? ":waiting" : "")", title: title,
                                   body: preview.problem ?? preview.waiting ?? "Review the exact file list and confirm it in Mediaforce.",
                                   opens: .needsYou, notifies: !waitingOnly)
        }
        return kinds + samples + seasons + computers + originals
    }

    private static func kindNoticeStage(_ kind: MainWindowFacts.PendingKind) -> String {
        let stage: String
        if kind.settingsAvailable == true {
            stage = "settings"
        } else if kind.sampleReady ?? true {
            stage = kind.sampleSeason ?? "ready"
        } else if ["failed", "stopped", "needs_attention"].contains(kind.sampleStatus ?? "") {
            stage = "attention:\(kind.sampleSeason ?? "")"
        } else if ["pending_review", "completed", "choose_size", "choose_balance", "sample_blocked"].contains(kind.sampleStatus ?? "") {
            stage = "choice:\(kind.sampleSeason ?? kind.sampleStartPrefix ?? "")"
        } else {
            stage = "preparing"
        }
        return stage
    }

    private static func kindPlace(_ kind: MainWindowFacts.PendingKind) -> Place {
        if let prefix = kind.sampleStartPrefix, ["choose_size", "choose_balance", "sample_blocked"].contains(kind.sampleStatus ?? "") {
            return .sample(prefix)
        }
        return .kind(kind.kind)
    }

    private static func kindWaitingReason(_ kind: MainWindowFacts.PendingKind) -> String {
        if kind.settingsAvailable == true {
            return "Choose an already approved show's settings for this kind. A new sample isn't needed."
        }
        if kind.sampleReady ?? true {
            let name = kind.sampleSeason?.split(separator: "/").dropFirst().joined(separator: " • ") ?? "The sample"
            return kind.oneTime == true
                ? "\(name) is ready to watch. Each show of this kind needs its own choice."
                : "Watch the sample and choose how this kind of content should look."
        }
        switch kind.sampleStatus {
        case "not_started":
            return "This kind needs a sample. Mediaforce tries to make one automatically; open it to choose a size if needed."
        case "starting", "running", "stopping", "queued", "retry_backoff":
            return "The sample is being prepared. Open this kind to see its progress and anything holding it up."
        case "pending_review", "completed":
            return "This sample cannot approve the kind. Open this kind to choose a different show's sample or approved settings."
        case "choose_size":
            return "Choose a size before Mediaforce can make the first sample."
        case "choose_balance":
            return "Choose how much detail to keep before Mediaforce can make the first sample."
        case "sample_blocked":
            return "The first sample cannot start yet. Open it to see what needs choosing."
        default:
            return "The sample needs attention before you can approve this kind. Open it to see what to do next."
        }
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
