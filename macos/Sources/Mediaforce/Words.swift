import Foundation

enum Words {
    static func size(_ bytes: Int64) -> String {
        let value = Double(bytes)
        if value >= 1e12 { return (value / 1e12).formatted(.number.precision(.fractionLength(2))) + " TB" }
        if value >= 1e9 { return (value / 1e9).formatted(.number.precision(.fractionLength(0))) + " GB" }
        if value >= 1e6 { return (value / 1e6).formatted(.number.precision(.fractionLength(0))) + " MB" }
        return (value / 1e3).formatted(.number.precision(.fractionLength(0))) + " KB"
    }

    static func season(_ show: String, _ season: Int?) -> String {
        guard let season else { return show }
        return "\(show), Season \(season)"
    }

    static func computerState(_ code: String) -> String {
        switch code {
        case "ready": "Ready"
        case "busy": "Compressing"
        case "checking": "Checking this computer"
        case "outside_schedule": "Off schedule"
        case "cooling_down": "Resting after repeated failures"
        case "not_encoding": "Makes samples only"
        case "storage_unreachable": "Can't reach shared storage"
        case "password_needed": "Needs its password once"
        case "unreachable": "Can't reach it. Is it on and awake?"
        case "remote_login_off": "Needs Remote Login turned on"
        case "identity_changed": "Its identity changed"
        case "new_name": "Needs to be confirmed"
        case "not_found": "Can't find it on the network"
        case "no_address": "Has no address"
        case "python_missing": "Needs Python 3"
        case "unreadable_reply": "Its reply couldn't be read"
        default: "Not ready"
        }
    }

    static func sampleState(_ sample: MainWindowFacts.Sample) -> String {
        switch (sample.state, sample.action) {
        case ("ready_to_compare", _): "Sample ready"
        case ("needs_help", "choose_size"): "Needs a size decision"
        case ("needs_help", _): "Sample needs making again"
        case ("ready_to_make", _): "Approved"
        default: "Waiting"
        }
    }

    static func sampleAction(_ action: String?) -> String? {
        switch action {
        case "review_sample": "Watch sample"
        case "make_sample", "retry_sample": "Make sample"
        case "choose_size": "Choose size"
        default: nil
        }
    }

    static func eventKind(_ kind: String) -> String {
        switch kind {
        case "promotion_completed": "replaced"
        case "validation_completed": "checked"
        case "encoding_completed": "compressed"
        case "encoding_failed": "couldn't be compressed"
        case "encoding_stopped": "stopped"
        default: "changed"
        }
    }
}

extension Words {
    static func workHeadline(_ facts: MainWindowFacts) -> String {
        let work = facts.work
        if work.pausedByOwner { return "Paused." }
        if work.stopReasons.contains("storage_unavailable") { return "Paused. This Mac can't see the media library." }
        if work.stopReasons.contains("no_computer_ready") { return "Waiting for a computer." }
        if work.runningCount > 0 { return "Compressing." }
        if work.waitingCount > 0 { return "Waiting to start." }
        return facts.samples.waitingOnYou.isEmpty ? "All caught up." : "Waiting for you."
    }

    static func workExplanation(_ facts: MainWindowFacts) -> String? {
        let work = facts.work
        var sentences: [String] = []
        if work.pausedByOwner {
            sentences.append("Nothing new starts until you resume. Work already running finishes first.")
        }
        if work.stopReasons.contains("storage_unavailable") {
            sentences.append(work.pausedByOwner
                ? "Shared storage isn't connected either; Mediaforce keeps checking by itself."
                : "Shared storage isn't connected, so nothing is being compressed. Mediaforce keeps checking by itself and carries on when it's back.")
        }
        if work.stopReasons.contains("no_computer_ready") {
            sentences.append("Work is waiting and no computer can take it right now. See Computers for why.")
        }
        if sentences.isEmpty, work.runningCount == 0, work.waitingCount == 0, !facts.samples.waitingOnYou.isEmpty {
            sentences.append("Nothing is being compressed. \(count(facts.samples.waitingOnYou.count, "sample")) under Samples need you first.")
        }
        return sentences.isEmpty ? nil : sentences.joined(separator: " ")
    }

    static func staleNotice(_ connection: ServerState, lastHeard: Date?) -> String {
        let since = lastHeard.map { " Showing what it reported at \($0.formatted(date: .omitted, time: .shortened))." } ?? ""
        return connection == .failing
            ? "Mediaforce isn't answering properly.\(since)"
            : "Can't reach Mediaforce on this Mac.\(since)"
    }

    static func sampleFailure(_ code: String?) -> String? {
        switch code {
        case "arithmetic_infeasible", "bound_exhausted", "quality_conflict":
            "It can't reach the size goal at good quality."
        case "goal_too_small": "The size goal is too small for this season."
        case "size_under_target", "size_over_target", "size_unverified": "The sample came out the wrong size."
        case "no_access": "Mediaforce couldn't open the source files."
        case "missing_clips": "Its comparison clips are gone."
        case "restarted": "It was interrupted by a restart."
        case "stopped": "It was stopped."
        default: nil
        }
    }

    static func waitingReason(_ sample: MainWindowFacts.Sample, _ facts: MainWindowFacts) -> String? {
        var reasons = facts.keptOriginal.held.first(where: { $0.prefix == sample.prefix })?.reasons.map(holdReason) ?? []
        let work = facts.work
        if work.pausedByOwner { reasons.append("until you resume") }
        if work.stopReasons.contains("storage_unavailable") { reasons.append("until storage is back") }
        if work.stopReasons.contains("no_computer_ready") { reasons.append("for a free computer") }
        return reasons.isEmpty ? nil : "Waits: " + reasons.joined(separator: ", ")
    }

    static func event(_ event: MainWindowFacts.Event) -> String {
        guard let season = event.season else { return "\(fileName(event.relPath)) \(eventKind(event.kind))" }
        return "\(Words.season(season.show, season.season)) \(eventKind(event.kind))"
    }

    static func eventSentence(_ event: MainWindowFacts.Event) -> String {
        let saved = event.bytesSaved.map { " Saved \(size($0))." } ?? ""
        if let show = event.season?.show, let episode = episodeCode(event.relPath) {
            return "\(show) \(episode) \(eventKind(event.kind)).\(saved)"
        }
        let subject = event.season.map { Words.season($0.show, $0.season) } ?? fileName(event.relPath)
        return "\(subject) \(eventKind(event.kind)).\(saved)"
    }

    static func moment(_ iso: String) -> String {
        guard let date = parse(iso) else { return "" }
        return date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    static func day(_ iso: String?) -> String {
        guard let iso, let date = parse(iso) else { return "" }
        return date.formatted(.dateTime.month(.abbreviated).day())
    }

    static func computerSymbol(_ computer: MainWindowFacts.Computer) -> String {
        let name = (computer.name ?? "").lowercased()
        if computer.platform == "linux" || name.contains("linux") { return "server.rack" }
        if name.contains("mbp") || name.contains("macbook") { return "laptopcomputer" }
        if name.contains("mini") { return "macmini" }
        if name.contains("studio") { return "macstudio" }
        return "desktopcomputer"
    }

    private static func parse(_ iso: String) -> Date? {
        try? Date(iso, strategy: .iso8601)
    }

    private static func fileName(_ relPath: String) -> String {
        URL(fileURLWithPath: relPath).deletingPathExtension().lastPathComponent
    }

    private static func episodeCode(_ relPath: String) -> String? {
        guard let match = relPath.firstMatch(of: /[Ss]\d{1,2}[Ee]\d{1,3}/) else { return nil }
        return String(match.output).uppercased()
    }
}

extension Words {
    static func pauseHelp(_ work: MainWindowFacts.Work) -> String {
        if work.stopReasons.contains("storage_unavailable") {
            return "Work is stopped until shared storage is back, then it carries on by itself. Pause to keep it stopped even then."
        }
        return "Stop starting new work on every computer. Work already running finishes first."
    }
}

extension Words {
    static func count(_ count: Int, _ noun: String) -> String {
        "\(count.formatted()) \(noun)\(count == 1 ? "" : "s")"
    }

    static func keptReason(_ code: String) -> String {
        switch code {
        case "motion_mixed": "mixed film and video motion"
        case "motion_partly_interlaced": "partly interlaced"
        case "motion_unclear": "motion pattern unclear"
        default: "kept by you"
        }
    }

    static func holdReason(_ code: String) -> String {
        switch code {
        case "current_season": "still airing"
        case "recent_acquisition": "added recently"
        case "content_age_unknown": "age not known yet"
        case "season_identity_conflict": "season number doesn't match"
        case "motion_pattern": "waiting for a motion decision"
        default: "waiting"
        }
    }
}
