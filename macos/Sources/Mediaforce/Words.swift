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
        case "away": "Away. Picks up work when it returns"
        case "reconnecting": "Reconnecting automatically"
        case "unreachable_long": "Has been off the network for a long time"
        case "recovery_required": "Needs its storage checked"
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

    static let computerNotHeardFrom = "Not checked yet"
    static let details = "Details"
    static let checkAgain = "Check Again"
    static let startMediaforce = "Start Mediaforce"
    static let restartMediaforce = "Restart Mediaforce"
    static let restartQuestion = "Restart Mediaforce?"
    static let restartMessage = "Anything being compressed or sampled right now is interrupted."
    static let settingsDidNotLoad = "Mediaforce is running, but these settings didn't load."

    static func serverSymbol(_ situation: ServerSituation) -> String {
        switch situation {
        case .checking: "ellipsis.circle"
        case .notSetUp: "shippingbox"
        case .notRunning: "moon.zzz"
        case .inTrouble: "exclamationmark.triangle"
        }
    }

    static func serverHeadline(_ situation: ServerSituation) -> String {
        switch situation {
        case .checking: "Looking for Mediaforce on this Mac…"
        case .notSetUp: "Mediaforce isn't set up on this Mac"
        case .notRunning: "Mediaforce isn't running"
        case .inTrouble: "Mediaforce is having trouble"
        }
    }

    static func serverSentence(_ situation: ServerSituation) -> String {
        switch situation {
        case .checking: "This only takes a moment."
        case .notSetUp: "Mediaforce itself runs in the background on the Mac that holds your library. Open this app on that Mac."
        case .notRunning: "It usually starts when you log in. Start it now, and this window fills in as soon as it answers."
        case .inTrouble: "It answered, but not properly, so this window can't show your library. It often clears by itself; if not, restarting it usually helps."
        }
    }

    static func serverLastHeard(_ situation: ServerSituation, _ lastHeard: Date?) -> String {
        if situation == .notSetUp { return "This Mac has never been set up to run Mediaforce." }
        guard let lastHeard else { return "Checking again every 30 seconds." }
        let day = Calendar.current.isDateInToday(lastHeard) ? "today" : lastHeard.formatted(date: .abbreviated, time: .omitted)
        return "Last heard from Mediaforce \(day) at \(lastHeard.formatted(date: .omitted, time: .shortened)). Checking again every 30 seconds."
    }

    /// What failed and when, for the Details disclosure.
    static func serverDetails(problem: ServerProblem?, refusal: String?) -> String {
        var lines: [String] = []
        if let problem {
            let what = switch problem.kind {
            case .noAnswer: "Mediaforce didn't answer"
            case .errorReply: "Mediaforce answered with an error"
            case .unreadableReply: "Mediaforce's answer couldn't be read, which can happen when the app and Mediaforce are different versions"
            }
            lines.append("\(what) at \(problem.at.formatted(date: .omitted, time: .standard)).")
        }
        if let refusal {
            lines.append("macOS refused to start it: \(refusal.isEmpty ? "no reason given" : refusal)")
        }
        return lines.isEmpty ? "Nothing more is known yet." : lines.joined(separator: "\n")
    }
    static let openInSettings = "Open in Settings › Computers"
    static let accountPassword = "Account password"
    static let workWindowsItem = "Work windows"

    /// States where a computer is fine as it is; every other state has something to fix.
    private static let computerStatesFine: Set<String> = ["ready", "busy", "outside_schedule", "not_encoding", "checking", "away", "reconnecting", "unreachable"]

    static func computerNeedsYou(_ state: String?) -> Bool {
        state.map { !computerStatesFine.contains($0) } ?? false
    }

    static func accountPasswordPrompt(_ name: String) -> String {
        "Password for \(name.isEmpty ? "this computer" : name)"
    }

    static func computerAction(_ action: ComputerAction, needsPassword: Bool) -> String {
        switch action {
        case .checkAgain: "Check Again"
        case .start: "Start It"
        case .setUp: needsPassword ? "Set Up" : "Set Up Again"
        case .trustAgain: "Trust It Again"
        }
    }

    /// What to do about a computer's state, in plain words; nil when there is nothing to do.
    static func computerFixHelp(_ state: String) -> String? {
        switch state {
        case "unreachable": "Make sure it is on and awake, then check again. Start It wakes it when Mediaforce knows how."
        case "not_found", "new_name": "Mediaforce checks it again and says what it finds."
        case "storage_unreachable": "Set it up again to reconnect it to the shared storage. Nothing on it is removed."
        case "unreadable_reply", "python_missing": "Set it up again. Mediaforce puts back what it needs there."
        case "password_needed": "Enter its account password once. Mediaforce uses it to set the computer up and doesn't keep it."
        case "identity_changed": "Its identity changed, which happens after a reinstall. If this is the same computer, trust it again."
        case "remote_login_off": "On that Mac, turn on Remote Login in System Settings › General › Sharing, then check again."
        case "no_address": "Give it an address below and save."
        case "cooling_down": "It failed several times in a row. Mediaforce tries it again by itself after a rest."
        default: nil
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
        case ("no_work", _): "Nothing to compress"
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
        case "motion_put_back": "you put the original back"
        case "motion_check_failed": "its compressed copy kept failing its check"
        case "motion_over_size": "its compressed copy came out over the size you allow"
        default: "kept by you"
        }
    }

    static func finishedNotice(_ season: MainWindowFacts.FinishedSeason, keptReasons: [String]) -> String {
        if season.keptEntirelyOriginal {
            var reasons: [String] = []
            for reason in keptReasons.map(keptReason) where !reasons.contains(reason) {
                reasons.append(reason)
            }
            guard !reasons.isEmpty else { return "Every episode was kept original." }
            return "Every episode was kept original: \(reasons.joined(separator: ", "))."
        }
        let kept = season.keptOriginalCount > 0 ? " \(count(season.keptOriginalCount, "episode")) kept original." : ""
        return "\(count(season.episodeCount, "episode")) compressed, checked and replaced.\(kept) \(size(season.bytesSaved)) saved."
    }

    static func finishedSaved(_ season: MainWindowFacts.FinishedSeason) -> String {
        season.keptEntirelyOriginal ? keptOriginal : "\(size(season.bytesSaved)) saved"
    }

    static func finishedSummary(_ season: MainWindowFacts.FinishedSeason) -> String {
        let name = Words.season(season.show, season.season)
        if season.keptEntirelyOriginal { return "\(name): every episode kept original" }
        let kept = season.keptOriginalCount > 0 ? ", \(season.keptOriginalCount) kept original" : ""
        return "\(name): \(count(season.episodeCount, "episode")) replaced\(kept), \(size(season.bytesSaved)) saved"
    }

    static let keptOriginal = "Kept original"

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
    static let openItem = "Open"
    static let cancel = "Cancel"
    static let resume = "Resume"
    static let pausedBar = "Mediaforce is paused. Nothing new starts."
    static let none = "None"
    static let next = "Next"
    static let looksGood = "Looks Good"
    static let appName = "Mediaforce"
    static let lookForNewFilesNow = "Look for New Files"
    static let openMediaforce = "Open Mediaforce"
    static let settingsEllipsis = "Settings…"
    static let resumeHelp = "Resume work on every computer."
    static let menuNotConnected = "Mediaforce isn't answering. Open it to see why."

    /// The line under the menu bar switch: what Mediaforce is doing, or why it isn't.
    static func menuStatus(_ facts: MainWindowFacts) -> String {
        let work = facts.work
        if work.pausedByOwner { return "Paused" }
        if work.stopReasons.contains("storage_unavailable") { return "This Mac can't see the media library" }
        if work.stopReasons.contains("no_computer_ready") { return "Waiting for a computer" }
        if work.runningCount > 0 {
            guard let first = work.runningSeasons.first else { return "Compressing" }
            let name = season(first.show, first.season)
            let more = work.runningSeasons.count - 1
            return more > 0 ? "Compressing \(name) and \(more) more" : "Compressing \(name)"
        }
        if work.waitingCount > 0 { return "Waiting to start" }
        return Attention.waitingOnYou(facts) > 0 ? waitingForYou : "Up to date"
    }
    static let tryAgain = "Try Again"
    static let spotCheckNoEpisode = "None of this show's episodes still has its original backup to compare."
    static let spotCheckUnanswered = "Mediaforce didn't answer, so this show can't play yet."
    static let spaceSaved = "Space saved"
    static let originalBackups = "Original backups"
    static let originalBackupsHelp = "When a season is replaced, its originals wait here until you delete them on Space saved."
    static let spotCheckFirst = "Spot-check before deleting"
    static let spotCheckBackups = "Spot-check what these backups hold"

    static func showOnly(_ group: LibraryStateGroup) -> String {
        "Show only \(libraryState(group).lowercased())"
    }
    static let spotCheckPrompt = "Does it look right? Each show you check gets a tick below."
    static let noBackupsToCheck = "No original backups are waiting, so there is nothing to spot-check."
    static let showChecked = "Checked. Choose it to look again."
    static let checkThisShow = "Play an episode of this show above."
    static let deleteBackups = "Delete Original Backups…"
    static let deleteAllBackups = "Delete all original backups"
    static let deleteBackupsQuestion = "Delete everything in the Cleanup folder?"
    static let deleteBackupsHelp = "Delete everything in the Cleanup folder. Mediaforce shows what is there and asks first."

    static func savedSoFar(_ bytes: Int64) -> String { "\(size(bytes)) saved so far" }

    static func spotCheckFile(_ seasonPrefix: String, _ file: ItemFacts.File) -> String {
        let folders = seasonPrefix.split(separator: "/").dropFirst().joined(separator: ", ")
        return file.episode == nil ? folders : "\(folders), \(fileName(file))"
    }

    static func showBackups(_ seasons: Int, _ bytes: Int64) -> String {
        "\(count(seasons, "season")), \(size(bytes)) of backups"
    }

    static func storageCompressed(_ bytes: Int64) -> String { "Compressed, \(size(bytes))" }
    static func storageStillOriginal(_ bytes: Int64) -> String { "Still original, \(size(bytes))" }
    static func storageSaved(_ bytes: Int64) -> String { "Space saved, \(size(bytes))" }
    static func storageBackups(_ bytes: Int64) -> String { "Backups, \(size(bytes))" }

    static func libraryWasAndIs(_ was: Int64, _ now: Int64) -> String {
        "The library was \(size(was)) and is \(size(now)) now."
    }

    static func deleteBackupsLine(_ count: Int, _ bytes: Int64) -> String {
        "Delete \(Words.count(count, "original backup")) to get back \(size(bytes))."
    }

    static func deleteBackupsDetail(_ folder: CleanupFolder) -> String {
        "\(count(folder.fileCount, "file")), \(size(folder.totalSizeBytes)). Mediaforce keeps any original a file still being finished needs. The replaced files stay in your library. This cannot be undone."
    }
    static let original = "Original"
    static let sample = "Sample"
    static let play = "Play"
    static let pause = "Pause"
    static let hear = "Hear"
    static let momentChoice = "Moment"
    static let sampleCoverageUnknown = "Coverage was not recorded for this sample."

    static func sampleCoverage(_ facts: SampleFacts) -> String? {
        guard facts.draftHash != nil, !SampleFacts.sampleMakingStates.contains(facts.state ?? "") else { return nil }
        return facts.coverageSummary ?? sampleCoverageUnknown
    }

    /// The sample's headline; a kind's page names the season, since its title names the kind.
    static func sampleHeadline(_ facts: SampleFacts, namesSeason: Bool, now: Date) -> String {
        let ready = facts.readyInSeconds.map { " Ready by about \(readyBy(now.addingTimeInterval(TimeInterval($0))))." } ?? ""
        guard namesSeason else { return sampleHeadline(facts) + (facts.state == "making_test" ? ready : "") }
        let name = season(facts.show, facts.season)
        return switch (facts.state, facts.action) {
        case ("making_test", _): "Making a sample of \(name).\(ready)"
        case ("sample_waiting", _): "The \(name) sample waits for a free computer."
        case ("ready_to_compare", _): "The \(name) sample is ready. Watch it next to the original."
        default: "\(name): \(sampleHeadline(facts))"
        }
    }

    // Rounded up to five minutes, since it is an estimate.
    static func readyBy(_ date: Date) -> String {
        let minutes = (date.timeIntervalSinceReferenceDate / 300).rounded(.up) * 300
        return Date(timeIntervalSinceReferenceDate: minutes).formatted(date: .omitted, time: .shortened)
    }

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

    static let compressNeedsChoice = ActionNotice(
        title: "Compressing needs a choice first",
        message: "The sample is approved, but this season needs a choice Mediaforce can't ask for here yet, so it isn't compressing."
    )

    static let chooseBalance = ActionNotice(
        title: "Choose this season's balance first",
        message: "This season needs its balance of size and detail chosen before a new sample, and Mediaforce can't ask for it here yet."
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
        case .readyToCompress: "Ready to compress"
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
        guard season.opensSample else { return openItem }
        return season.action == "queue_encode" ? startCompressing : sampleAction(season.action) ?? sample
    }

    static func looseFiles(_ files: TVShowsFacts.LooseFiles) -> String {
        let one = files.episodeCount == 1
        let verb = one ? "isn't" : "aren't"
        let them = one ? "it" : "them"
        let episodes = "\(count(files.episodeCount, "episode")) (\(size(files.bytes)))"
        return "\(episodes) \(verb) in a season folder. Mediaforce works season by season, so move \(them) into a Season folder to compress \(them)."
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
        case "ready": "Ready to compress"
        default: "No work needed"
        }
    }

    static func movieDetail(_ movie: MoviesFacts.Movie) -> String {
        let place = movie.library.map { " in \($0)" } ?? ""
        let saved = movie.estimatedSavedBytes.map { "About \(size($0)) could be saved." } ?? "No estimate of space saved yet."
        return "\(movieState(movie.state)). \(count(movie.fileCount, "file")), \(size(movie.bytes))\(place). \(saved)"
    }
}

extension Words {
    static let otherMedia = "Other media"
    static let nameColumn = "Name"
    static let searchOtherMedia = "Search folders and files"
    static let noMatchingOtherMedia = "No folders or files match."
    static let chooseOtherMedia = "Choose a folder or file to see more about it."
    static let otherMediaTruncated = "This list shows only part of this library. The rest is too big to list here."

    // Counts what the list holds, folders and single files, and the files inside them, which is the sidebar's number.
    static func otherMediaCount(_ shown: Int, of total: Int, files: Int) -> String {
        let listed = shown == total ? total.formatted() : "\(shown.formatted()) of \(total.formatted())"
        return "\(listed) folders and files, \(count(files, "file")) in all"
    }

    static func otherMediaState(_ code: String) -> String {
        switch code {
        case "cannot_start": "Can't start"
        case "view_only": "View only"
        case "needs_attention": "Needs attention"
        case "compressing": "Compressing"
        case "ready": "Ready to compress"
        default: "No work needed"
        }
    }

    static func otherMediaDetail(_ item: OtherMediaFacts.Item) -> String {
        let place = item.library.map { " in \($0)" } ?? ""
        let saved = item.estimatedSavedBytes.map { "About \(size($0)) could be saved." } ?? "No estimate of space saved yet."
        return "\(otherMediaState(item.state)). \(count(item.fileCount, "file")), \(size(item.bytes))\(place). \(saved)"
    }
}

extension Words {
    static let librariesTab = "Libraries"
    static let librariesSection = "Libraries"
    static let workingSpaceSection = "Working space"
    static let libraryName = "Name"
    static let libraryFolder = "Folder"
    static let libraryType = "Kind"
    static let plexPath = "Path reported by Plex"
    static let plexPathHelp = "Leave blank when Plex reports the same path Mediaforce uses."

    static func plexPathSummary(_ path: String) -> String {
        path.trimmingCharacters(in: .whitespaces).isEmpty ? "Plex: uses this folder" : "Plex: its own path"
    }

    static let addLibrary = "Add Library"
    static let removeLibrary = "Remove"
    static let chooseFolder = "Choose…"
    static let workingFolder = "Working folder"
    static let workingFolderHelp = "Compressed files are made here before they replace originals. Original backups wait in its Cleanup folder."
    static let lookForNewFiles = "Look for new files every"
    static let hours = "hours"
    static let lookForNewFilesHelp = "0 turns automatic checks off."
    static let saveSettings = "Save"
    static let revertSettings = "Revert"
    static let settingsSaved = "Saved."
    static let noLibraries = "No libraries yet."
    static let keepCurrentKind = "Keep current kind"
    static let changeKind = "Change kind"
    static let keepLibrary = "Keep library"
    static let removeFromMediaforce = "Remove from Mediaforce"

    static func libraryKind(_ code: String) -> String {
        switch code {
        case "tv": "TV shows"
        case "movie": "Movies"
        case "spatial": "3D and VR"
        case "other": "Other"
        default: "Unknown"
        }
    }

    static func changeKindTitle(_ preview: LibraryTypePreview) -> String {
        "Change this library from \(libraryKind(preview.fromType)) to \(libraryKind(preview.toType))?"
    }

    static func changeKindMessage(_ preview: LibraryTypePreview) -> String {
        "This changes how \(count(preview.itemCount, "scanned item")) are grouped. After you save, the library is view only until you review it, and a fresh scan applies the new grouping. Saved compression choices are kept but no longer apply. No media files move or are deleted."
    }

    static func removeLibraryTitle(_ name: String) -> String {
        "Remove \(name.isEmpty ? "this library" : name)?"
    }

    static let removeLibraryMessage = "Mediaforce stops scanning this folder after you save. Files stay where they are and are never deleted."

    static func librarySaveRefused(_ code: String?) -> String {
        switch code {
        case "working_folder_missing": "Choose a working folder."
        case "working_folder_not_absolute": "The working folder must be a full path, starting with /."
        case "refresh_interval_invalid": "How often to look for new files must be between 0 and 8760 hours."
        case "library_incomplete": "Each library needs a folder."
        case "library_name_too_long": "Library names must be 80 characters or fewer."
        case "library_folder_not_absolute": "Each library folder must be a full path, starting with /."
        case "library_folder_duplicate": "Two libraries use the same folder. Each library needs its own."
        case "library_folders_overlap": "One library's folder is inside another's. Library folders can't contain each other."
        case "library_contains_working_folder": "The working folder can't be inside a library folder or contain one."
        case "library_type_unsupported": "Choose a kind for each library."
        case "library_type_unconfirmed": "A library's kind changed without its preview being confirmed. Choose the kind again."
        case "no_libraries": "Keep at least one library."
        case "plex_path_relative": "A path reported by Plex must be a full path, starting with /."
        case "plex_path_duplicate": "Two libraries use the same path reported by Plex. Each library needs its own."
        default: "Mediaforce couldn't save these settings. Check the libraries and folders."
        }
    }
}

extension ActionNotice {
    static let previewUnavailable = ActionNotice(
        title: "Kind not changed",
        message: "Mediaforce couldn't check what changing this library's kind would do, so the kind stays as it was."
    )

    static let settingsChangedElsewhere = ActionNotice(
        title: "Settings not saved",
        message: "These settings changed somewhere else while this window was open. It now shows them as saved; make your changes again."
    )

    static func settingsNotSaved(_ code: String?) -> ActionNotice {
        ActionNotice(title: "Settings not saved", message: Words.librarySaveRefused(code))
    }
}

extension Words {
    static let computersTab = "Computers"
    static let computersSection = "Computers"
    static let noComputers = "No computers yet. Mediaforce works on this Mac only."
    static let addComputer = "Add Computer"
    static let removeComputer = "Remove"
    static let unnamedComputer = "New computer"
    static let computerName = "Name"
    static let computerAddress = "Address"
    static let computerAddressHelp = "How Mediaforce reaches this computer, such as studio.local or name@studio.local."
    static let computerCanDo = "Can"
    static let computerLibraries = "Works on"
    static let computerLibrariesHelp = "Turn every library on to include libraries you add later."
    static let computerPriority = "Priority"
    static let computerPriorityHelp = "When more than one computer is free, the higher number gets work first."
    static let wakeAddress = "Wake address"
    static let wakeAddressPrompt = "Optional, such as 00:11:22:33:44:55"
    static let wakeAddressHelp = "This computer's network hardware address. With it, Mediaforce wakes the computer when work is waiting."
    static let connectionDetails = "Connection details"
    static let mediaAccess = "Gets files by"
    static let mediaforceFolder = "Mediaforce folder on that computer"
    static let computerWorkingFolder = "Working folder"
    static let computerWorkingFolderPrompt = "Same as the main working folder"
    static let scratchFolder = "Scratch folder on that computer"
    static let scratchFolderPrompt = "None"
    static let scratchFolderHelp = "For a computer without the shared media folders. Mediaforce copies each file here, compresses it there, then deletes the copy."
    static let removedLibraryFolder = "Folder for a library Mediaforce no longer has"
    static let clearFolder = "Clear"
    static let startCommand = "Start command"
    static let stopCommand = "Stop command"
    static let optionalField = "Optional"
    static let startWait = "Wait for it to start"
    static let seconds = "seconds"
    static let workWindow = "Work window"
    static let workWindowsSection = "Work windows"
    static let noWorkWindows = "No work windows of your own yet. Computers can still work any time or never."
    static let addWorkWindow = "Add Work Window"
    static let removeWorkWindow = "Remove"
    static let unnamedWorkWindow = "New work window"
    static let workWindowName = "Name"
    static let worksOn = "Works"
    static let allDayOn = "All day"
    static let startsAt = "From"
    static let endsAt = "Until"
    static let workWindowHelp = "When Until is earlier than From, the window runs overnight. Times are each computer's own."
    static let keepComputer = "Keep computer"
    static let keepWorkWindow = "Keep work window"

    static func computerAbility(_ code: String) -> String {
        switch code {
        case "encode_queue": "Compress files"
        case "sample_calibration": "Make samples"
        default: "Other work"
        }
    }

    static func builtInWorkWindow(_ key: String) -> String {
        switch key {
        case "always": "Any time"
        case "never": "Never"
        default: "Unknown"
        }
    }

    static func mediaAccessChoice(_ code: String) -> String {
        switch code {
        case "mounted": "Opening the shared media folders"
        case "stream": "Having files sent to it"
        default: "Unknown"
        }
    }

    static func atOnce(_ count: Int) -> String {
        "Jobs at once: \(count.formatted())"
    }

    static func day(_ code: String) -> String {
        switch code {
        case "mon": "Mon"
        case "tue": "Tue"
        case "wed": "Wed"
        case "thu": "Thu"
        case "fri": "Fri"
        case "sat": "Sat"
        case "sun": "Sun"
        default: code
        }
    }

    static func hour(_ hour: Int) -> String {
        let shown = hour % 12 == 0 ? 12 : hour % 12
        return "\(shown) \(hour < 12 ? "AM" : "PM")"
    }

    static func libraryFolderOnComputer(_ name: String) -> String {
        "\(name) folder on that computer"
    }

    static func removeComputerTitle(_ name: String) -> String {
        "Remove \(name.isEmpty ? "this computer" : name)?"
    }

    static let removeComputerMessage = "Mediaforce stops giving this computer work after you save. Nothing on the computer is deleted."

    static func removeWorkWindowTitle(_ name: String) -> String {
        "Remove \(name.isEmpty ? "this work window" : name)?"
    }

    static func removeWorkWindowMessage(usedBy computers: Int) -> String {
        computers == 0
            ? "No computer uses it."
            : "\(count(computers, "computer")) use\(computers == 1 ? "s" : "") it. Choose another work window for \(computers == 1 ? "it" : "them") before you save."
    }

    static func computerSaveRefused(_ code: String?) -> String {
        switch code {
        case "computer_address_missing": "Each computer needs an address."
        case "computer_address_duplicate": "Two computers have the same address. Each computer needs its own."
        case "computer_abilities_missing": "Each computer needs something it can do: compress files, make samples, or both."
        case "computer_priority_invalid": "Priority must be a whole number."
        case "computer_at_once_invalid": "How many files a computer works on at once must be a whole number."
        case "computer_start_wait_invalid": "How long to wait for a computer to start must be a whole number of seconds."
        case "computer_scratch_folder_not_absolute": "A scratch folder must be a full path on that computer, starting with /."
        case "computer_library_folders_invalid": "A computer's library folders couldn't be read. Check them under Connection details."
        case "computer_library_folder_unknown": "A computer has a folder for a library Mediaforce no longer has. Clear it under Connection details."
        case "schedule_key_missing", "schedule_key_reserved", "schedule_duplicate": "Two work windows got mixed up. Revert and make your changes again."
        case "schedule_days_missing": "Each work window needs at least one day."
        case "schedule_hours_invalid": "Each work window needs a start and end hour."
        case "schedule_unknown": "A computer uses a work window that was removed. Choose another work window for it."
        default: "Mediaforce couldn't save these settings. Check the computers and work windows."
        }
    }
}

extension ActionNotice {
    static func computerSettingsNotSaved(_ code: String?) -> ActionNotice {
        ActionNotice(title: "Settings not saved", message: Words.computerSaveRefused(code))
    }
}

extension Words {
    static let metadataTab = "Plex and TMDB"
    static let plexSection = "Plex"
    static let tmdbSection = "TMDB"
    static let usePlex = "Use Plex"
    static let usePlexHelp = "Plex tells Mediaforce when each show first arrived, so a compressed copy keeps that date."
    static let plexAddress = "Plex server address"
    static let plexAddressPrompt = "http://plex.local:32400"
    static let useTMDB = "Use TMDB"
    static let useTMDBHelp = "TMDB tells Mediaforce whether a show is still airing."
    static let accessKey = "Access key"
    static let accessKeyFound = "Found"
    static let accessKeyDetails = "Key details"
    static let accessKeyReadFrom = "Read from"
    static let accessKeyHelp = "Mediaforce reads the key from this setting on the Mac that runs it, and sees a change after it restarts. Settings never show or store the key itself."
    static let metadataHelp = "A full library scan refreshes what Plex and TMDB know. What Mediaforce already learned stays available while either can't be reached."

    static func accessKeyNeeded(_ name: String) -> String {
        "Needed. Set \(name) on the Mac that runs Mediaforce."
    }

    static func metadataSaveRefused(_ code: String?) -> String {
        switch code {
        case "plex_address_invalid": "The Plex server address must start with http:// or https://, such as http://plex.local:32400, with no name, password or ? part."
        case "tmdb_address_invalid": "The saved TMDB address isn't a valid https:// address. Fix it in the configuration file, then try again."
        default: "Mediaforce couldn't save these settings. Check the Plex server address."
        }
    }
}

extension ActionNotice {
    static func metadataSettingsNotSaved(_ code: String?) -> ActionNotice {
        ActionNotice(title: "Settings not saved", message: Words.metadataSaveRefused(code))
    }
}

extension Words {
    static let compressionTab = "Compression"
    static let aimSection = "What to aim for"
    static let aimPicker = "Mediaforce aims for"
    static let sizeSection = "Size"
    static let sizeGoal = "Size goal"
    static let megabytes = "MB"
    static let forAnEpisodeOf = "for an episode of"
    static let minutes = "minutes"
    static let sizeLimit = "Allowed over the goal"
    static let sizeLimitHelp = "A finished file may be this much over its size goal. Samples are held to the same limit. Smaller is never a failure by itself."
    static let largestSize = "Largest allowed size"
    static let percentOfOriginal = "% of the original"
    static let largestSizeHelp = "A compressed file bigger than this share of its original is not used."
    static let percent = "%"
    static let pictureSection = "Picture"
    static let tallestPicture = "Tallest picture"
    static let pixelsTall = "pixels tall"
    static let tallestPictureHelp = "Taller pictures are scaled down to this height. 0 keeps each picture as tall as the original."
    static let grain = "Film grain"
    static let grainHelp = "How much film grain Mediaforce adds back after compressing, from 0 to 50. 0 adds none."
    static let advancedCompression = "Advanced: how picture quality is measured"
    static let measurementPicker = "Measure picture quality by"
    static let appearanceTarget = "Picture appearance score to aim for"
    static let appearanceMinimum = "Lowest picture appearance score allowed"
    static let detailTarget = "Picture detail score to aim for"
    static let detailMinimum = "Lowest picture detail score allowed"
    static let scoresHelp = "Scores run from 1 to 100; higher means closer to the original."
    static let samplePicker = "How samples are made"

    static func compressionAim(_ code: String) -> String {
        switch code {
        case "size_first_review": "The size goal"
        case "quality_first_sample": "The picture quality"
        default: "Unknown"
        }
    }

    static func qualityMeasurement(_ code: String) -> String {
        switch code {
        case "auto": "Automatic"
        case "vmaf": "Picture appearance"
        case "xpsnr": "Picture detail"
        default: "Unknown"
        }
    }

    static func sampleMethod(_ code: String) -> String {
        switch code {
        case "ab_av1_fast_sample": "Fast sample (the only way for now)"
        default: "Unknown"
        }
    }

    static func sizeGoalScaling(_ code: String) -> String {
        switch code {
        case "normalized": "Scaled to each episode's length, so a longer episode gets more room."
        default: "Unknown"
        }
    }

    static func tallestPictureSummary(_ height: Int) -> String {
        height > 0 ? "Up to \(height.formatted()) pixels tall" : "As tall as the original"
    }

    static func compressionSaveRefused(_ code: String?) -> String {
        switch code {
        case "size_goal_invalid": "The size goal must be between 1 and 100,000 MB."
        case "size_goal_minutes_invalid": "The episode length for the size goal must be between 1 and 1,440 minutes."
        case "size_limit_invalid": "How far over the goal a file may be must be between 0.1% and 100%."
        case "sample_size_limit_invalid": "The saved size limit for samples isn't between 0.1% and 100%. Change how far over the goal a file may be, then save."
        case "largest_size_invalid": "The largest allowed size must be between 1% and 100% of the original."
        case "tallest_picture_invalid": "The tallest picture must be a whole number of pixels from 0 to 4,320."
        case "grain_invalid": "Film grain must be a whole number from 0 to 50."
        case "appearance_target_invalid": "The picture appearance score to aim for must be between 1 and 100."
        case "appearance_minimum_invalid": "The lowest picture appearance score must be between 1 and 100."
        case "detail_target_invalid": "The picture detail score to aim for must be between 1 and 100."
        case "detail_minimum_invalid": "The lowest picture detail score must be between 1 and 100."
        case "appearance_minimum_above_target": "The lowest picture appearance score can't be above the score to aim for."
        case "detail_minimum_above_target": "The lowest picture detail score can't be above the score to aim for."
        case "unknown_aim": "Choose what Mediaforce aims for."
        case "unknown_measurement": "Choose how picture quality is measured."
        case "unknown_sample_method": "Choose how samples are made."
        default: "Mediaforce couldn't save these settings. Check the compression settings."
        }
    }
}

extension ActionNotice {
    static func compressionSettingsNotSaved(_ code: String?) -> ActionNotice {
        ActionNotice(title: "Settings not saved", message: Words.compressionSaveRefused(code))
    }
}

extension Words {
    static let waitingForYou = "Waiting for you"
    static let keepThisCopy = "Keep this copy"
    static let makeItAgain = "Make it again"
    static let putOriginalBack = "Put original back…"
    static let compressed = "Compressed"
    static let soundLabel = "Sound"
    static let stillAiringLabel = "Still airing"
    static let stillAiringModes = ["auto", "on", "off"]
    static let putOriginalBackDetail =
        "The compressed copy is removed and the original goes back in the library. Mediaforce keeps it original until you choose otherwise."

    static func itemTitle(_ facts: ItemFacts) -> String {
        facts.season.map { "Season \($0)" } ?? facts.title
    }

    static func beforeAfter(_ before: Int64, _ after: Int64) -> String {
        before == after ? size(after) : "\(size(before)) → \(size(after))"
    }

    static func itemSummary(_ facts: ItemFacts) -> String {
        let noun = facts.domain == "tv" ? "episode" : "file"
        var parts: [String] = []
        if facts.compressedCount > 0 { parts.append("\(count(facts.compressedCount, noun)) compressed") }
        if facts.keptCount > 0 { parts.append("\(facts.keptCount) kept original") }
        if !facts.waiting.isEmpty { parts.append("\(facts.waiting.count) waiting for you") }
        if parts.isEmpty { return "Nothing compressed yet." }
        return parts.joined(separator: ", ") + "."
    }

    static func fileName(_ file: ItemFacts.File) -> String {
        guard let episode = file.episode else { return file.name }
        if file.inShowPage, let season = file.season { return "Season \(season), Episode \(episode)" }
        return "Episode \(episode)"
    }

    static func fileState(_ file: ItemFacts.File) -> String {
        switch file.stage {
        case "needs_you": file.sizeQuestion != nil ? "Needs more than its size goal" : waitLabel(file)
        case "kept_original": "\(keptOriginal): \(keptReason("motion_\(file.keptReason ?? "")"))"
        case "held": file.heldReason == "motion_pattern" ? "Kept original: Mediaforce can't convert its motion safely" : "Waiting to start"
        case "compressing": file.percentComplete.map { "Compressing, \($0)%" } ?? "Compressing"
        case "measuring": "Finding the right size"
        case "getting_ready": "Getting ready"
        case "checking": "Checking the compressed copy"
        case "waiting": file.validation == "remaking" ? "Didn't pass its check, being made again" : "Waiting its turn"
        default: "Not compressed yet"
        }
    }

    static func waitingReason(_ file: ItemFacts.File) -> String {
        if let question = file.sizeQuestion {
            return "To look right it needs \(size(question.smallestQualitySafeBytes)), more than its \(size(question.goalBytes)) goal."
        }
        let computer = file.waitReason?.computer ?? "the computer"
        return switch file.waitReason?.code ?? file.validation {
        case "check_failed", "failed": "The compressed copy didn't pass its check, so the original stays in the library."
        case "publish_failed": "Mediaforce couldn't check or publish this copy after several tries. Check this file."
        case "publish_unsafe": "Publishing stopped, and Mediaforce couldn't put the files back. Check and restore this file before publishing it."
        case "not_made_again": notMadeAgain(file.waitReason?.blockers ?? [])
        case "size_held": "It came out much smaller than its sample. Keep this copy, or make it again."
        case "stopped": "It was stopped before it finished."
        case "restart_stopped":
            "Mediaforce restarted while \(computer) was compressing it and can't tell whether that work ended. Check \(computer)."
        case "restart_stopped_last_attempt": "Mediaforce restarted during its last try."
        case "quality_floor_size_conflict": "To look right it needs more space than its size goal."
        case "final_size_target_miss": "The compressed copy didn't come out at its size goal."
        case "controller_database_busy": "Mediaforce was too busy to keep track of it, so it stopped."
        case "storage_io": "It had trouble reading or writing the media files."
        case "host_unavailable": "The computer compressing it became unavailable."
        case "ssh_transport": "Mediaforce couldn't connect to the computer compressing it."
        case "remote_quality_timeout": "Checking its picture took too long on a computer."
        case "needs_review": "It ran into a problem Mediaforce couldn't sort out by itself."
        case "schedule_too_short": "It takes longer than every computer's work window. A longer work window lets it run."
        case "plan_rebuild": "Its work needs to be set up again before it can run."
        case "storage_repair": "The storage it's on needs to be mounted or repaired."
        case "storage_reconnect": "The storage it's on needs to be reconnected."
        case "copy_missing": "Its compressed copy is missing, so there's nothing to check."
        case "settings_block": "Mediaforce can't start it with its library's current settings."
        default: "It needs a decision from you."
        }
    }

    private static func waitLabel(_ file: ItemFacts.File) -> String {
        switch file.waitReason?.code ?? file.validation {
        case "check_failed", "failed": "Didn't pass its check"
        case "publish_failed": "Couldn't publish"
        case "publish_unsafe": "Files need restoring"
        case "not_made_again": "Couldn't be made again"
        case "size_held": "Much smaller than its sample"
        case "stopped": "Stopped"
        case "restart_stopped", "restart_stopped_last_attempt": "Stopped by a restart"
        case "quality_floor_size_conflict": "Needs more than its size goal"
        case "final_size_target_miss": "Missed its size goal"
        case "controller_database_busy", "storage_io", "host_unavailable", "ssh_transport", "remote_quality_timeout",
             "needs_review": "Couldn't finish"
        case "schedule_too_short": "Longer than every work window"
        case "plan_rebuild": "Needs to be set up again"
        case "storage_repair": "Storage needs a repair"
        case "storage_reconnect": "Storage disconnected"
        case "copy_missing": "Compressed copy missing"
        case "settings_block": "Can't start"
        default: "Needs a decision"
        }
    }

    private static func notMadeAgain(_ blockers: [String]) -> String {
        let reasons = blockers.compactMap(remakeBlocker)
        let why = switch reasons.count {
        case 0: " after several tries"
        case 1: ": " + reasons[0]
        default: ": " + reasons.dropLast().joined(separator: ", ") + " and " + reasons[reasons.count - 1]
        }
        return "The compressed copy didn't pass its check, and Mediaforce couldn't make it again\(why). The original stays in the library."
    }

    private static func remakeBlocker(_ code: String) -> String? {
        switch code {
        case "approval_changed": "its approved settings changed"
        case "run_settings_missing": "its saved settings are missing"
        case "approval_missing": "it needs a newly approved sample"
        case "run_details_missing": "its saved size details are missing"
        case "size_settings_unchanged": "it needs a sample approved with different settings"
        case "size_checks_block": "its saved size checks still hold it back"
        case "original_unreachable": "the original file can't be reached"
        case "copy_points_at_original": "its compressed copy points at the original"
        case "sampling": "a sample is still being made"
        case "compressing": "other files here are still compressing"
        case "queued_run": "a waiting run may still make it"
        case "copy_removed_not_queued": "its old copy was removed, but the new one didn't start"
        case "not_waiting": "it was no longer waiting to be made again"
        case "copy_in_use": "its compressed copy was still in use"
        case "copy_path_missing": "the place its compressed copy was saved can't be found"
        case "remote_settings_missing": "the computer that made its compressed copy is no longer set up"
        case "request_changed": "its saved request changed partway"
        case "copy_not_removed": "its old copy couldn't be removed"
        case "recovery_not_saved": "its new start couldn't be saved"
        case "recovery_failed": "something went wrong while setting it up again"
        case "queue_failed": "adding it to the work list failed"
        case "queue_refused": "the work list didn't take it"
        case "queue_excluded": "the work list left it out"
        default: nil
        }
    }

    static let keepTheOriginal = "Keep the original"

    static func allowLarger(_ question: ItemFacts.SizeQuestion) -> String {
        "Allow \(size(question.smallestQualitySafeBytes))"
    }

    static func couldNotPlay(_ file: ItemFacts.File) -> String {
        "\(fileName(file)) couldn't be made ready to play."
    }

    static func actionRefused(_ action: ItemAction) -> String {
        switch action {
        case .makeAgain: "Not made again"
        case .keepThisCopy: "Not kept"
        case .putOriginalBack: "The original was not put back"
        case .allowLarger: "The larger size was not allowed"
        case .keepOriginal: "Not kept original"
        }
    }

    static func actionRefusal(_ code: String?, _ message: String?) -> String {
        switch code {
        case "backup_gone": "Its original backup was deleted, so there is nothing to put back."
        case "backup_changed": "Its original backup changed since it was made, so Mediaforce left everything as it was."
        case "busy": "This file is being worked on right now. Try again when that finishes."
        case "occupied": "Another file is already in the way. Nothing was moved."
        case "unsafe": "It stopped partway and Mediaforce could not put the files back. Check the library and the original backups folder."
        default: message ?? "Mediaforce left everything as it was."
        }
    }

    static func gettingReady(_ file: ItemFacts.File) -> String {
        "Getting \(fileName(file)) ready to play"
    }

    static func playTime(_ position: Double, _ duration: Double) -> String {
        "\(clock(position)) / \(clock(duration))"
    }

    private static func clock(_ seconds: Double) -> String {
        let total = Int(seconds.isFinite ? max(seconds, 0) : 0)
        return total >= 3600
            ? String(format: "%d:%02d:%02d", total / 3600, total / 60 % 60, total % 60)
            : String(format: "%d:%02d", total / 60, total % 60)
    }

    static func playerCaption(_ file: ItemFacts.File, comparing: Bool) -> String {
        if comparing, let before = file.originalBytes {
            return "\(fileName(file)): \(beforeAfter(before, file.bytes))."
        }
        if file.compressed { return "\(fileName(file)): \(size(file.bytes)). Its original backup is deleted." }
        return "\(fileName(file)): \(size(file.bytes)), as it is now."
    }

    static func putOriginalBackQuestion(_ file: ItemFacts.File) -> String {
        "Put the original of \(fileName(file)) back?"
    }

    static func forEverySeason(_ show: String) -> String {
        "For every season of \(show)"
    }

    static func soundName(_ key: String) -> String {
        switch key {
        case "keep_surround": "Film"
        case "speech_stereo": "Speech-heavy"
        default: key
        }
    }

    static func soundExplanation(_ key: String?) -> String {
        switch key {
        case "speech_stereo": "Stereo sound, which saves more space. Suits shows that are mostly people talking."
        default: "Keeps surround sound at its usual quality."
        }
    }

    static func stillAiringName(_ mode: String) -> String {
        switch mode {
        case "on": "Yes"
        case "off": "No"
        default: "Follow TMDB"
        }
    }

    static func stillAiringExplanation(_ airing: ItemFacts.StillAiring) -> String {
        let tmdb = switch airing.airing {
        case true?: " TMDB says it is still airing."
        case false?: " TMDB says it has ended."
        case nil: ""
        }
        return "While a show is still airing, Mediaforce leaves its newest season alone until the season ends.\(tmdb)"
    }
}

extension Words {
    static func workAction(_ action: SampleFacts.Action) -> String {
        switch action {
        case .approve, .makeAgain, .makeSample, .chooseSize, .tryAnotherSize, .openSample: "Watch the Sample"
        case .startCompressing: startCompressing
        case .checkFiles: "Check New Files"
        case .replaceOriginals: "Replace Originals…"
        case .retryUnfinished: "Retry Unfinished Episodes"
        case .decideMotion: "Keep These Originals…"
        }
    }

    static func workQuestion(_ action: WorkAction) -> String {
        switch action {
        case .replaceOriginals: "Replace the originals with the checked copies?"
        case .keepMotionOriginals: "Keep these files original?"
        default: ""
        }
    }

    static func workButton(_ action: WorkAction) -> String {
        switch action {
        case .replaceOriginals: "Replace Originals"
        case .keepMotionOriginals: "Keep Original"
        default: ""
        }
    }

    static func workDetail(_ action: WorkAction) -> String {
        switch action {
        case .replaceOriginals:
            "Each checked copy takes its original's place in the library. The originals become backups you can put back until you delete them."
        case .keepMotionOriginals:
            "Mediaforce can't convert their motion pattern safely, so they stay as they are. You can undo this later."
        default: ""
        }
    }

    static func workRefused(_ action: WorkAction) -> String {
        switch action {
        case .startCompressing: "Compressing didn't start"
        case .checkFiles: "Nothing was checked"
        case .replaceOriginals: "No originals were replaced"
        case .retryUnfinished: "Nothing was retried"
        case .keepMotionOriginals: "Nothing was kept original"
        }
    }
}

extension Words {
    static let media = "Media"
    static let seasonsHeading = "Seasons"
    static let kindLabel = "Kind"
    static let viewLabel = "View"
    static let tableView = "Show as a table"
    static let posterView = "Show as posters"
    static let searchMedia = "Search"
    static let noMatchingMedia = "Nothing matches. Try another kind, state or search."

    static func mediaKind(_ kind: MediaKind) -> String {
        switch kind {
        case .everything: "Everything"
        case .tv: "TV shows"
        case .movies: "Movies"
        case .other: "Other media"
        }
    }
}

// The page for how each kind of content should look.
extension Words {
    static let howItShouldLook = "How it should look"
    static let showsOfThisKind = "Shows of this kind"
    static let approvedShow = "Approved"
    // Stopping removes the shared setting, so each show of the kind goes back to its own sample.
    static let stopUsingKind = "Decide Show by Show"
    static let kindRefusedTitle = "Mediaforce didn't use that setting"
    static let ok = "OK"
    static let backOneFrame = "Back one frame"
    static let backOneFrameHelp = "Back one frame (,)"
    static let forwardOneFrame = "Forward one frame"
    static let forwardOneFrameHelp = "Forward one frame (.)"
    static let fullScreen = "Full screen"
    static let fullScreenHelp = "Full screen (F). Press Escape to come back."
    static let exitFullScreen = "Exit full screen"
    static let actualSize = "Actual size"
    static let actualSizeHelp = "Show every pixel. Both sides pan together."
    static func oneAtATimeHelp(_ other: String) -> String {
        "Click the picture to switch between the original and the \(other.lowercased())"
    }
    static let couldNotPlayEpisode = "Couldn't play this episode."
    static let couldNotPlaySample = "Couldn't play this moment of the sample."
    static let gettingSampleReady = "Getting the sample ready to play"
    static let gettingEpisodeReady = "Getting the episode ready to play"
    static let seasonsWithoutKindTitle = "Seasons that don't fit a kind"
    static let showEverything = "Show Everything"

    static func kindName(_ code: String) -> String {
        switch code {
        case "darker_live_action": "Darker live action"
        case "bright_live_action": "Bright live action"
        case "cartoon": "Cartoons"
        case "4k": "4K"
        default: code.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func kindSubtitle(_ kind: KindsFacts.Kind) -> String {
        "\(count(kind.shows.count, "show")) · \(size(kind.waitingBytes))"
    }

    static func useForKind(_ code: String) -> String {
        let name = switch code {
        case "darker_live_action": "Darker Live Action"
        case "bright_live_action": "Bright Live Action"
        default: kindName(code)
        }
        return "Use for All \(name)"
    }

    static func sizeSetting(_ setting: KindsFacts.Setting) -> String {
        guard let megabytes = setting.sizeGoalMb else { return "its approved size" }
        let amount = "\(Int(megabytes.rounded())) MB"
        return setting.sizeGoalMode == "normalized" ? "\(amount) per 45-minute episode" : "\(amount) per episode"
    }

    static func showSetting(_ show: String, _ setting: KindsFacts.Setting) -> String {
        "\(show)'s setting: \(sizeSetting(setting))"
    }

    static func kindDecided(_ approval: KindsFacts.Approval) -> String {
        let show = String(approval.showPrefix.split(separator: "/").last ?? "")
        let decided = day(approval.approvedAt)
        return "Decided \(decided.isEmpty ? "" : "\(decided). ")\(showSetting(show, approval.setting))"
    }

    static func decidedShowByShow(_ code: String) -> String {
        code == "cartoon" ? "Each cartoon is decided on its own." : "Each \(kindName(code)) show is decided on its own."
    }

    static func kindEmpty(_ code: String) -> String {
        "No seasons measure as \(kindName(code).lowercased()) yet."
    }

    static let kindNothingToWatch = "No episode made with this setting is kept to compare yet."
    static let makeASample = "Make a Sample"

    static func makeSampleOf(_ show: String) -> String {
        "Make a Sample of \(show)"
    }

    static func kindRefusal(_ code: String?) -> String {
        switch code {
        case "approval_not_current": "That show's sample approval has changed. Approve its sample again first."
        case "size_goal_not_scalable": "That setting is a size per episode, so it can't carry to other shows."
        case "source_kind_differs": "That show's sample was made on a season of another kind."
        case "no_folder_approval": "That show has no approved sample of its own."
        case "kind_is_one_time": "Each of these shows is decided on its own sample."
        default: "Mediaforce didn't say why."
        }
    }

    static func watchShow(_ show: String) -> String { "Play \(show)" }
    static func openShow(_ show: String) -> String { "Open \(show)" }

    static func seasonsWithoutKind(_ seasons: Int) -> String {
        seasons == 1 ? "1 season doesn't fit a kind" : "\(seasons.formatted()) seasons don't fit a kind"
    }

    static func leftOutReason(_ code: String) -> String {
        code == "mixed" ? "mixed kinds" : "not measured yet"
    }

    static func playerLayout(_ layout: CompareLayout) -> String {
        switch layout {
        case .sideBySide: "Side by side"
        case .wipe: "Wipe"
        case .oneAtATime: "One at a time"
        }
    }
}
