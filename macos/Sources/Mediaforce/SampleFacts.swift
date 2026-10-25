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

    enum Action: Equatable {
        case approve, makeAgain, makeSample, chooseSize, tryAnotherSize, startCompressing, openInBrowser
    }

    static let inProgressStates: Set<String> = ["making_test", "sample_waiting", "making_season"]

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
        case "retry_encode", "review_files", "validate_outputs", "promote_outputs", "decide_motion": return [.openInBrowser]
        default: return []
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
