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
        case approve, makeAgain, makeSample, chooseSize, tryAnotherSize
    }

    var isBeingMade: Bool { state == "making_test" || state == "sample_waiting" }

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
        default: return []
        }
    }
}

struct SampleActionResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
    let compressing: Bool?
}
