import Foundation

struct SampleFacts: Decodable, Sendable {
    let prefix: String
    let showPrefix: String
    let show: String
    let season: Int?
    let state: String?
    let action: String?
    let failure: String?
    let moments: [Moment]
    let episodeBytes: Int64
    let estimatedEpisodeBytes: Int64
    let seasonBytes: Int64
    let seasonEpisodeCount: Int
    let goalBytes: Int64
    let sizeStatus: String?
    let draftHash: String?
    let sizeChoices: [SizeChoice]

    struct Moment: Decodable, Sendable, Hashable {
        let atSeconds: Double
        let original: Clip
        let sample: Clip
    }

    struct Clip: Decodable, Sendable, Hashable {
        let url: String
        let sizeBytes: Int64
    }

    struct SizeChoice: Decodable, Sendable, Identifiable {
        let key: String
        let targetBytes: Int64
        var id: String { key }
    }

    enum Action: Hashable {
        case approve, makeAgain, makeSample, chooseSize, tryAnotherSize, startCompressing
        case checkFiles, replaceOriginals, retryUnfinished, decideMotion
        case openInBrowser
    }

    static let seasonPageActions: Set<Action> = [.checkFiles, .replaceOriginals, .retryUnfinished, .decideMotion]
    static let inProgressStates: Set<String> = ["making_test", "sample_waiting", "making_season"]
    static let browserStates: Set<String> = ["finish_blocked", "ready_to_check", "ready_to_finish", "motion_held"]

    var isInProgress: Bool { state.map(Self.inProgressStates.contains) ?? false }

    var estimatedSavedBytes: Int64? {
        guard estimatedEpisodeBytes > 0, seasonEpisodeCount > 0 else { return nil }
        return max(0, seasonBytes - estimatedEpisodeBytes * Int64(seasonEpisodeCount))
    }

    var actions: [Action] {
        let others: [Action] = sizeChoices.isEmpty ? [] : [.tryAnotherSize]
        switch action {
        case "review_sample": return [.approve, .makeAgain] + others
        case "make_sample", "retry_sample": return [.makeSample] + others
        case "choose_size": return sizeChoices.isEmpty ? [] : [.chooseSize]
        case "queue_encode": return [.startCompressing]
        case "retry_encode": return [.retryUnfinished]
        case "validate_outputs": return [.checkFiles]
        // A blocked file (a failed check or a missing copy) has no step that settles it yet (#1083).
        case "review_files": return [.openInBrowser]
        case "promote_outputs": return [.replaceOriginals]
        case "decide_motion": return [.decideMotion]
        default: return state.map(Self.browserStates.contains) == true ? [.openInBrowser] : []
        }
    }
}

struct SampleApproval: Equatable {
    let reviewedDraftHash: String
    var confirmed: [String] = []

    func stillReviewed(_ facts: SampleFacts?) -> Bool {
        (facts?.draftHash ?? "") == reviewedDraftHash
    }
}

struct SampleActionResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
    let compressing: Bool?
    let reason: String?

    static let browserReasons: Set<String> = ["confirm_legacy_size", "confirm_compression_goal", "approval_needs_fresh_sample"]

    var needsBrowser: Bool { reason.map(Self.browserReasons.contains) ?? false }
}
