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
        seasonState(sample.state, sample.action)
    }

    static func seasonState(_ state: String?, _ action: String?) -> String {
        switch (state, action) {
        case ("ready_to_compare", _): "Sample ready"
        case ("needs_help", "choose_size"): "Needs a size decision"
        case ("needs_help", "retry_encode"): "Compressing stopped"
        case ("needs_help", "review_files"): "Some files need you"
        case ("needs_help", _): "Sample needs making again"
        case ("ready_to_make", _): "Approved"
        case ("needs_test", _): "Needs sample"
        case ("sample_waiting", _): "Sample waiting"
        case ("making_test", _): "Making sample"
        case ("making_season", _): "Compressing"
        case ("motion_held", "decide_motion"): "Needs a motion decision"
        case ("motion_held", _): "Stays original"
        case ("ready_to_check", _): "Ready to check"
        case ("finish_blocked", _): "Not ready to replace"
        case ("ready_to_finish", _): "Ready to replace"
        case ("finished", _): "Finished"
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

extension Words {
    static let approveSample = "Approve and compress the season"
    static let makeSampleAgain = "Make sample again"
    static let makeSample = "Make sample"
    static let startCompressing = "Start compressing"
    static let tryAnotherSize = "Try another size"
    static let chooseSizeHeadline = "Choose a size"
    static let openInBrowser = "Open in browser"
    static let cancel = "Cancel"
    static let original = "Original"
    static let sample = "Sample"
    static let play = "Play"
    static let pause = "Pause"
    static let startOver = "Start over"
    static let hear = "Hear"
    static let momentChoice = "Moment"

    static func sampleHeadline(_ facts: SampleFacts) -> String {
        switch (facts.state, facts.action) {
        case ("making_test", _): "Making the sample now."
        case ("sample_waiting", _): "The sample waits for a free computer."
        case ("ready_to_compare", _): "The sample is ready. Watch it next to the original."
        case ("needs_help", "choose_size"): "The sample can't reach the size goal at good quality. Choose another size."
        case ("needs_help", "retry_encode"): "Compressing stopped. Finished episodes are safe; the rest need trying again."
        case ("needs_help", "review_files"): "Some files need you before the season can go on."
        case ("needs_help", _): "The sample needs making again."
        case ("needs_test", _): "This season has no sample yet."
        case ("ready_to_make", _): "Approved. The season waits to be compressed."
        case ("making_season", _): "Compressing the season."
        case ("motion_held", "decide_motion"): "Some episodes need a decision about their motion pattern."
        case ("motion_held", _): "The remaining episodes stay original because their motion can't be converted safely."
        case ("ready_to_check", _): "The compressed episodes are ready for their safety check."
        case ("finish_blocked", _): "Some compressed episodes aren't ready to replace their originals yet."
        case ("ready_to_finish", _): "The compressed episodes passed their checks and can replace the originals."
        case ("finished", _): "This season is finished."
        default: "Nothing needs you here right now."
        }
    }

    static func sizeChoice(_ choice: SampleFacts.SizeChoice) -> String {
        let name = switch choice.key {
        case "recommended": "Recommended"
        case "smaller": "Save more space"
        case "roomier": "Keep more detail"
        case "confirm_normalized": "Scale with runtime"
        case "confirm_absolute": "Keep this size"
        default: "Size goal"
        }
        return "\(name), \(size(choice.targetBytes)) an episode"
    }

    static func clipSize(_ clip: SampleFacts.Clip) -> String {
        "\(size(clip.sizeBytes)) clip"
    }

    static func momentTime(_ seconds: Double) -> String {
        Duration.seconds(seconds).formatted(.time(pattern: seconds >= 3600 ? .hourMinuteSecond : .minuteSecond))
    }

    static func sampleFacts(_ facts: SampleFacts) -> [(label: String, value: String)] {
        [
            ("This episode now", facts.episodeBytes > 0 ? size(facts.episodeBytes) : "Not known"),
            ("Estimated after", facts.estimatedEpisodeBytes > 0 ? "About \(size(facts.estimatedEpisodeBytes))" : "No estimate yet"),
            ("Season space saved", facts.estimatedSavedBytes.map { "About \(size($0)) from \(count(facts.seasonEpisodeCount, "episode"))" } ?? "No estimate yet"),
            ("Size goal", facts.goalBytes > 0 ? "\(size(facts.goalBytes)) an episode" : "Not set"),
        ]
    }

    static func sampleConfirmation(_ code: String) -> (title: String, message: String)? {
        switch code {
        case "confirm_high_impact": (
            "This sample changed important picture settings.",
            "The sample you watched shows those changes. Approve it only if the picture and sound still feel right."
        )
        case "confirm_size_tradeoff": (
            "This sample missed your size goal.",
            "The season may come out larger or smaller than the goal. The estimate on this page is the size you would accept."
        )
        default: nil
        }
    }
}

extension ActionNotice {
    static let sampleRefused = ActionNotice(
        title: "Mediaforce didn't do that",
        message: "It refused the request. The page shows what Mediaforce reports now."
    )

    static let approvedNotCompressing = ActionNotice(
        title: "Compressing didn't start",
        message: "The sample is approved, but the season isn't compressing. Press Start compressing to try again."
    )

    static let sampleChanged = ActionNotice(
        title: "The sample changed",
        message: "A new sample replaced the one you watched, so nothing was approved. Watch the new sample first."
    )

    static let finishInBrowser = ActionNotice(
        title: "Compressing needs a choice first",
        message: "The sample is approved, but this season needs a choice made on its page in the browser before it can compress."
    )

    static let chooseBalance = ActionNotice(
        title: "Choose this season's balance first",
        message: "This season needs its balance of size and detail chosen before a new sample. Choose it in the browser."
    )

    static let chooseSize = ActionNotice(
        title: "Choose a size first",
        message: "This season's sample needs a size chosen before it can be made again."
    )
}

extension Words {
    static let tvShows = "TV shows"
    static let showColumn = "Show"
    static let seasonsColumn = "Seasons"
    static let sizeColumn = "Size"
    static let estimatedSavedColumn = "Estimated space saved"
    static let stateColumn = "State"
    static let searchShows = "Search shows or seasons"
    static let allStates = "All"
    static let noMatchingShows = "No shows match."
    static let chooseShow = "Choose a show to see its seasons."
    static let notKnownYet = "Not known yet"

    static func libraryState(_ group: LibraryStateGroup) -> String {
        switch group {
        case .needsYou: "Needs you"
        case .inProgress: "In progress"
        case .readyForYou: "Ready for you"
        case .noActiveWork: "No active work"
        }
    }

    static func seasonName(_ season: TVShowsFacts.Season) -> String {
        season.season.map { "Season \($0)" } ?? season.folder
    }

    static func tvSeasonState(_ season: TVShowsFacts.Season) -> String {
        if season.filesNeedYou { return "Compressing. Some files need you." }
        if season.checksOnly { return "Approved. Motion-pattern checks run first." }
        return seasonState(season.state, season.action)
    }

    static func seasonButton(_ season: TVShowsFacts.Season) -> String {
        guard season.opensSample else { return openInBrowser }
        return season.action == "queue_encode" ? startCompressing : sampleAction(season.action) ?? sample
    }

    static func shown(_ shown: Int, of total: Int, _ noun: String) -> String {
        shown == total ? count(total, noun) : "\(shown.formatted()) of \(count(total, noun))"
    }
}

extension Words {
    static let movies = "Movies"
    static let titleColumn = "Title"
    static let filesColumn = "Files"
    static let searchMovies = "Search movies"
    static let noMatchingMovies = "No movies match."
    static let chooseMovie = "Choose a movie to see more about it."

    static func movieState(_ code: String) -> String {
        switch code {
        case "view_only": "View only"
        case "finished": "Finished"
        case "cannot_start": "Can't start"
        case "needs_file_choice": "Needs a file choice"
        case "motion_held": "Stays original"
        case "needs_attention": "Needs attention"
        case "compressing": "Compressing"
        case "ready": "Ready for you"
        default: "No work needed"
        }
    }

    static func movieDetail(_ movie: MoviesFacts.Movie) -> String {
        let place = movie.library.map { " in \($0)" } ?? ""
        let saved = movie.estimatedSavedBytes.map { "About \(size($0)) could be saved." } ?? "No estimate of space saved yet."
        return "\(movieState(movie.state)). \(count(movie.fileCount, "file")), \(size(movie.bytes))\(place). \(saved)"
    }
}
