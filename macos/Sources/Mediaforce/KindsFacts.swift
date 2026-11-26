import Foundation

/// Each kind of content, what it covers and how it is decided, from `GET /api/app/kinds`. The server sends facts
/// and codes; the app words them.
struct KindsFacts: Decodable, Sendable, Equatable {
    let kinds: [Kind]
    let leftOut: [LeftOutSeason]
    // The library's tallest picture; 0 when every original keeps its height, so there is no 4K choice to make.
    var tallestPicture: Int? = nil

    struct Kind: Decodable, Sendable, Identifiable, Equatable {
        let kind: String
        let oneTime: Bool
        let seasons: Int
        let waitingBytes: Int64
        // The seasons this kind covers, by show; a show with its own approval follows it and is never here.
        let shows: [Show]
        // Shows already approved on a season of this kind, whose setting the kind could use.
        let settings: [Setting]
        let approval: Approval?
        // The season whose sample is being made or waits to be watched; nil when none is.
        let sampleSeason: String?
        var seasonApprovedShows: [String]? = nil
        var selectedSizeMb: Double? = nil
        var sizeOptionsMb: [Double]? = nil
        var animationTitles: [AnimationTitle]? = nil
        var sampleReady: Bool? = nil
        var sampleStatus: String? = nil
        var id: String { kind }

        var firstSampleShow: Show? { shows.first { $0.sampleSeason != nil } }
        var sampleInProgress: Bool? = nil

        var reviewSeason: String? { sampleStatus == "choose_another_show" ? nil : sampleSeason }
        var sampleShow: String? { reviewSeason?.split(separator: "/").prefix(2).joined(separator: "/") }
        var showsSampleFirst: Bool { selectedSizeMb != nil || oneTime || settings.isEmpty || sampleSetting != nil }

        var sampleButtonTitle: String? {
            if let approval { return approval.sampleMoments?.isEmpty == false ? Words.watchApprovedSample : nil }
            guard reviewSeason != nil else { return nil }
            if Words.sampleNeedsRemaking(sampleStatus) { return Words.makeSample }
            if sampleStatus == "choose_size" { return Words.chooseSize }
            if ["pending_review", "completed"].contains(sampleStatus) { return Words.watchSample }
            return Words.openSample
        }

        var offersSample: Bool {
            reviewSeason == nil || (sampleReady == false && ["pending_review", "completed"].contains(sampleStatus ?? ""))
        }

        var sampleSetting: Setting? {
            guard let reviewSeason else { return nil }
            return settings.first { reviewSeason == $0.fromPrefix || reviewSeason.hasPrefix("\($0.fromPrefix)/") }
        }

        /// The setting the page plays first: the decided one, else the first with an episode to watch.
        var firstSetting: Setting? {
            if let approval {
                return settings.first { $0.fromPrefix == approval.fromPrefix } ?? approval.setting
            }
            guard selectedSizeMb == nil else { return nil }
            return sampleSetting ?? settings.first { $0.libraryItemId != nil } ?? settings.first
        }
    }

    struct AnimationTitle: Decodable, Identifiable, Equatable, Sendable {
        let prefix: String
        let source: String
        let pictureLook: String?
        let kbps: Double?
        let look: String
        let error: String?
        let originalMissing: Bool?
        let chosen: Bool
        let moment: SampleFacts.Moment?
        var id: String { prefix }
        var name: String { prefix.split(separator: "/").dropFirst().joined(separator: " · ") }
        var explanation: String {
            if error != nil { return "The short test needs another try" }
            if originalMissing == true { return "Waiting for the original file" }
            if kbps == nil { return "Waiting for a short test" }
            if chosen { return look == "cartoon" ? "You chose the cartoon look" : "You chose the live action look" }
            switch look {
            case "cartoon": return "Easy to compress · Uses the cartoon look"
            case "live_action": return "Hard to compress · Uses the live action look"
            default:
                if moment != nil { return "Close call · Choose a look" }
                if pictureLook == "cartoon" { return "Picture passed · Automatic sorting is on hold" }
                if pictureLook == "live_action" { return "Picture lost detail · Automatic sorting is on hold" }
                return "Automatic sorting is on hold"
            }
        }
    }

    struct Show: Decodable, Sendable, Identifiable, Equatable {
        let prefix: String
        let seasons: Int
        let waitingBytes: Int64
        // Its biggest waiting season of the kind, where a sample for the show is made.
        let sampleSeason: String?
        var id: String { prefix }
        var name: String { String(prefix.split(separator: "/").last ?? "") }
        // A movie title is listed beside the shows its look covers.
        var mediaType: String? = nil
        var isMovie: Bool { mediaType.map { $0 == "movie" } ?? prefix.hasPrefix("movies/") }
    }

    struct Setting: Decodable, Sendable, Identifiable, Equatable {
        let show: String
        let fromPrefix: String
        let sizeGoalMode: String?
        let sizeGoalMb: Double?
        // A published episode made under it whose original backup is kept, to compare; nil when none is.
        let libraryItemId: Int?
        var id: String { show }
        var name: String { String(show.split(separator: "/").last ?? "") }
    }

    struct Approval: Decodable, Sendable, Equatable {
        let fromPrefix: String
        let approvedAt: String?
        let sampleMoments: [SampleFacts.Moment]?
        let sizeGoalMode: String?
        let sizeGoalMb: Double?
        let libraryItemId: Int?
        // Whether 4K stays 4K under this approval; otherwise it comes down to the library's tallest picture.
        var keepFullHeight: Bool? = nil

        var showPrefix: String { fromPrefix.split(separator: "/").prefix(2).joined(separator: "/") }
        var setting: Setting {
            Setting(show: showPrefix, fromPrefix: fromPrefix, sizeGoalMode: sizeGoalMode, sizeGoalMb: sizeGoalMb, libraryItemId: libraryItemId)
        }
    }

    struct LeftOutSeason: Decodable, Sendable, Equatable {
        let season: String
        // "unmeasured" until its originals are measured.
        let kind: String
        let waitingBytes: Int64
    }
}

struct KindActionResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
    var message: String? = nil
}

extension MediaforceClient {
    func kinds() async throws -> KindsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/kinds"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(KindsFacts.self, from: request)
    }

    /// Uses a show's own approval for every season of the kind; a refusal answers 409 with a code the app words.
    func approveKind(_ kind: String, from prefix: String) async throws -> KindActionResult {
        try await kindAction(kind, "approve", body: ["from_prefix": prefix])
    }

    func chooseAnimationLook(_ title: KindsFacts.AnimationTitle, look: String) async throws -> KindActionResult {
        try await kindAction("cartoon", "title-look", body: ["prefix": title.prefix, "source": title.source, "look": look])
    }

    func chooseKindSize(_ kind: String, megabytes: Double) async throws -> KindActionResult {
        try await kindAction(kind, "size", body: ["size_mb": megabytes])
    }

    func makeKindSample(_ kind: String, prefix: String) async throws -> KindActionResult {
        try await kindAction(kind, "sample", body: ["prefix": prefix], timeout: Self.samplePreparationTimeout)
    }

    func stopKind(_ kind: String) async throws -> KindActionResult {
        try await kindAction(kind, "stop", body: [String: String]())
    }

    /// Saves whether 4K stays 4K under this kind's approval.
    func chooseKindPicture(_ kind: String, keepFullHeight: Bool) async throws -> KindActionResult {
        try await kindAction(kind, "picture", body: ["keep_full_height": keepFullHeight])
    }

    private func kindAction(_ kind: String, _ action: String, body: some Encodable, timeout: TimeInterval? = nil) async throws -> KindActionResult {
        var request = URLRequest(url: baseURL.appending(path: "api/app/kinds").appending(path: kind).appending(path: action))
        if let timeout { request.timeoutInterval = timeout }
        request.httpMethod = "POST"
        request.httpBody = try JSONEncoder().encode(body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 409].contains((response as? HTTPURLResponse)?.statusCode) else { throw URLError(.badServerResponse) }
        return try JSONDecoder.mediaforce.decode(KindActionResult.self, from: data)
    }
}
