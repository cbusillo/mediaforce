import Foundation

struct SampleFacts: Decodable, Sendable {
    let prefix: String
    let showPrefix: String
    let show: String
    let season: Int?
    let state: String?
    let waitDetail: String?
    let action: String?
    let failure: String?
    let moments: [Moment]
    let coverageSummary: String?
    let episodeBytes: Int64
    let estimatedEpisodeBytes: Int64
    let seasonBytes: Int64
    let seasonEpisodeCount: Int
    let goalBytes: Int64
    let sizeStatus: String?
    let draftHash: String?
    let approvalKind: String?
    // The latest a running sample should be ready by, from comparable finished samples; nil when they can't tell.
    let readyInSeconds: Int?
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
        case approve, makeAgain, makeSample, chooseSize, tryAnotherSize, openSample, startCompressing
        case checkFiles, replaceOriginals, retryUnfinished, decideMotion
    }

    static let seasonPageActions: Set<Action> = [.checkFiles, .replaceOriginals, .retryUnfinished, .decideMotion]
    static let inProgressStates: Set<String> = ["making_test", "sample_waiting", "making_season"]
    // A sample being made, or waiting for a computer, already has its page to follow it on.
    static let sampleMakingStates: Set<String> = ["making_test", "sample_waiting"]

    var isInProgress: Bool { state.map(Self.inProgressStates.contains) ?? false }

    var estimatedSavedBytes: Int64? {
        guard estimatedEpisodeBytes > 0, seasonEpisodeCount > 0 else { return nil }
        return max(0, seasonBytes - estimatedEpisodeBytes * Int64(seasonEpisodeCount))
    }

    var actions: [Action] {
        if action == nil, state.map(Self.sampleMakingStates.contains) == true { return [.openSample] }
        let others: [Action] = sizeChoices.isEmpty ? [] : [.tryAnotherSize]
        switch action {
        case "review_sample": return [.approve, .makeAgain] + others
        case "make_sample", "retry_sample": return [.makeSample] + others
        case "choose_size": return sizeChoices.isEmpty ? [] : [.chooseSize]
        case "queue_encode": return [.startCompressing]
        case "retry_encode": return [.retryUnfinished]
        case "validate_outputs": return [.checkFiles]
        case "promote_outputs": return [.replaceOriginals]
        case "decide_motion": return [.decideMotion]
        // A blocked file (a failed check or a missing copy) and a state with no native step show no action yet (#1088, #1140).
        default: return []
        }
    }
}

struct SampleApproval: Equatable {
    let reviewedDraftHash: String
    var scope = "season"
    var confirmed: [String] = []

    func changeNotice(_ facts: SampleFacts?) -> ActionNotice {
        facts?.draftHash == reviewedDraftHash ? .approvalScopeChanged : .sampleChanged
    }

    func stillReviewed(_ facts: SampleFacts?) -> Bool {
        (facts?.draftHash ?? "") == reviewedDraftHash && (scope == "season" || facts?.approvalKind == scope)
    }
}

struct SampleActionResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
    let compressing: Bool?
    let kindCode: String?
    let reason: String?

    static let choiceReasons: Set<String> = ["confirm_legacy_size", "confirm_compression_goal", "approval_needs_fresh_sample"]

    var needsChoice: Bool { reason.map(Self.choiceReasons.contains) ?? false }
}
