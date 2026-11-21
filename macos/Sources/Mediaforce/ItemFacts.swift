import Foundation

/// One show season, movie or folder: every file in it, where each has got, and the show's own choices.
struct ItemFacts: Decodable, Sendable {
    let prefix: String
    let domain: String
    let kind: String
    let title: String
    let show: String?
    let showPrefix: String?
    let season: Int?
    let parentPrefix: String?
    private(set) var files: [File]
    let sound: Sound?
    let stillAiring: StillAiring?
    let movieQueue: MovieQueue?

    struct MovieQueue: Decodable, Sendable {
        let waits: [String]
        let refusal: Refusal?

        struct Refusal: Decodable, Sendable {
            let code: String?
            let message: String?
            let measurable: Bool?
        }
    }

    struct File: Decodable, Sendable, Identifiable, Equatable {
        let libraryItemId: Int
        let relPath: String
        let name: String
        let season: Int?
        let episode: Int?
        let stage: String
        let validation: String?
        let waitReason: WaitReason?
        let additionalWaitReasons: [WaitReason]?
        let keptReason: String?
        let heldReason: String?
        var canCompressAsIs: Bool?
        let percentComplete: Int?
        let sizeQuestion: SizeQuestion?
        let bytes: Int64
        let originalBytes: Int64?
        let originalBackup: Bool
        let heldCopy: HeldCopy?
        var id: Int { libraryItemId }
        // A whole show's page names the season too, since episode numbers repeat across seasons.
        var inShowPage = false

        private enum CodingKeys: String, CodingKey {
            case libraryItemId, relPath, name, season, episode, stage, validation, waitReason, additionalWaitReasons, keptReason, heldReason
            case canCompressAsIs, percentComplete, sizeQuestion, bytes, originalBytes, originalBackup, heldCopy
        }

        /// What the player shows: a held copy against the file as it is, a compressed file against its backup, or the
        /// one file there is.
        var playerSides: (shown: PlayerSide, comparedWith: PlayerSide?) {
            if heldCopy?.present == true { return (.compressed, .original) }
            if compressed { return (.compressed, originalBackup ? .original : nil) }
            return (.original, nil)
        }

        var compressed: Bool { stage == "published" }
        /// Held because Mediaforce couldn't confirm its last run ended; only the Director can release it.
        var unconfirmedStop: WaitReason? {
            ([waitReason].compactMap { $0 } + (additionalWaitReasons ?? [])).first { $0.code == "containment_unproven" }
        }
        var heldForUnconfirmedStop: Bool { unconfirmedStop != nil }
        var needsMotionChoice: Bool { heldReason == "motion_pattern" && keptReason == nil && canCompressAsIs == true }
        var needsYou: Bool { stage == "needs_you" || !(additionalWaitReasons ?? []).isEmpty }
    }

    /// Why a file waits on the Director, as a code with any values its words need.
    struct WaitReason: Decodable, Sendable, Equatable {
        let code: String
        /// The computer a restart stopped it on, or the one to check before a held file is tried again.
        let computer: String?
        /// Why a failed copy could not be made again.
        let blockers: [String]?
    }

    /// A copy held for the Director because it came out far smaller than expected: its size, and whether it is still
    /// there to watch.
    struct HeldCopy: Decodable, Sendable, Equatable {
        let bytes: Int64?
        let present: Bool
        var waitingReason: String?
    }

    /// The file needs more than its size goal to keep the picture right.
    struct SizeQuestion: Decodable, Sendable, Equatable {
        let jobId: String
        let goalBytes: Int64
        let smallestQualitySafeBytes: Int64
    }

    struct Sound: Decodable, Sendable {
        let showPrefix: String?
        let current: String?
        let options: [String]
    }

    struct StillAiring: Decodable, Sendable {
        let seriesPrefix: String
        let mode: String
        let airing: Bool?
    }

    /// A whole show's page names each episode's season, since episode numbers repeat.
    mutating func markShowPage() {
        guard kind == "tv_series" else { return }
        files = files.map { file in
            var file = file
            file.inShowPage = true
            return file
        }
    }

    func workActions(_ actions: [SampleFacts.Action]) -> [SampleFacts.Action] {
        actions.filter { $0 != .reviewFiles || !waiting.isEmpty }
    }

    func selectedFile(_ id: File.ID?) -> File? {
        files.first { $0.id == id }
            ?? files.first { $0.needsYou }
            ?? files.first { $0.originalBackup }
            ?? files.first { $0.compressed }
            ?? files.first
    }

    var motionFiles: [File] { files.filter { $0.heldReason == "motion_pattern" && $0.keptReason == nil } }
    var motionQuestion: String {
        let eligible = motionFiles.filter(\.needsMotionChoice).count
        let held = motionFiles.count - eligible
        var line = "\(Words.count(eligible, "file")) can be compressed without changing the frames."
        if held > 0 {
            line += held == 1 ? " One stays held because its motion can’t be compressed safely." :
                " \(held) stay held because their motion can’t be compressed safely."
        }
        return line
    }
    var waiting: [File] { files.filter(\.needsYou) }
    var compressedCount: Int { files.filter(\.compressed).count }
    var keptCount: Int { files.filter { $0.stage == "kept_original" || ($0.stage == "held" && $0.heldReason != "motion_pattern") }.count }
    var bytes: Int64 { files.reduce(0) { $0 + $1.bytes } }
    var originalBytes: Int64 { files.reduce(0) { $0 + ($1.originalBytes ?? $1.bytes) } }
}

enum PlayerSide: String, Sendable {
    case compressed, original
}

struct ItemActionResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
    let message: String?

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        ok = try values.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        code = try values.decodeIfPresent(String.self, forKey: .code)
        // A refusal raised as a request error carries its sentence as `detail`.
        message = try values.decodeIfPresent(String.self, forKey: .message) ?? values.decodeIfPresent(String.self, forKey: .detail)
    }

    private enum CodingKeys: String, CodingKey {
        case ok, code, message, detail
    }

    private init(ok: Bool, message: String?) {
        self.ok = ok
        code = nil
        self.message = message
    }

    static let done = ItemActionResult(ok: true, message: nil)

    static func refused(_ message: String?) -> ItemActionResult {
        ItemActionResult(ok: false, message: message)
    }
}

extension MediaforceClient {
    func item(_ prefix: String) async throws -> ItemFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/items").appending(path: prefix))
        request.timeoutInterval = Self.factsTimeout
        var facts = try await decode(ItemFacts.self, from: request)
        facts.markShowPage()
        return facts
    }

    /// One file's answer, from its season's page or from Needs you.
    func answer(_ action: ItemAction, in prefix: String, for file: ItemFacts.File) async throws -> ItemActionResult {
        switch action {
        case .compressAsIs: try await compressAsIs(prefix, [file.id], reconsiderKept: true)
        case .makeAgain: try await makeAgain(prefix, file.libraryItemId)
        case .keepThisCopy: try await keepThisCopy(prefix, file.libraryItemId)
        case .putOriginalBack: try await putOriginalBack(prefix, file.libraryItemId)
        case .allowLarger: try await decideSize(file.sizeQuestion?.jobId ?? "", allow: true)
        case .keepOriginal: try await decideSize(file.sizeQuestion?.jobId ?? "", allow: false)
        case .releaseHold: try await releaseHold(file)
        }
    }

    func player(_ libraryItemId: Int, _ side: PlayerSide) -> URL {
        baseURL.appending(path: "api/app/player/\(libraryItemId)/\(side.rawValue)/index.m3u8")
    }

    func putOriginalBack(_ prefix: String, _ libraryItemId: Int) async throws -> ItemActionResult {
        try await folderAction(prefix, "put-original-back", body: ["library_item_ids": [libraryItemId]])
    }

    func makeAgain(_ prefix: String, _ libraryItemId: Int) async throws -> ItemActionResult {
        try await folderAction(prefix, "size-held-decision", body: ["library_item_ids": [libraryItemId], "keep": false])
    }

    func keepThisCopy(_ prefix: String, _ libraryItemId: Int) async throws -> ItemActionResult {
        try await folderAction(prefix, "size-held-decision", body: ["library_item_id": libraryItemId, "keep": true])
    }

    func decideSize(_ jobId: String, allow: Bool) async throws -> ItemActionResult {
        var request = URLRequest(url: baseURL.appending(path: "api/encode-queue/size-decision"))
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: ["job_id": jobId, "allow": allow])
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 409].contains((response as? HTTPURLResponse)?.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder.mediaforce.decode(ItemActionResult.self, from: data)
    }

    func checkFiles(_ prefix: String) async throws -> ItemActionResult {
        try await folderAction(prefix, "validate-outputs", body: [:])
    }

    func replaceOriginals(_ prefix: String) async throws -> ItemActionResult {
        try await folderAction(prefix, "promote-outputs", body: [:])
    }

    /// The Director's confirmed release of one held file: it is cleaned up and tried again like any retry.
    func releaseHold(_ file: ItemFacts.File) async throws -> ItemActionResult {
        try await folderAction(file.relPath, "queue-encode", body: ["release_hold": true, "bypass_schedule": false])
    }

    func retryUnfinished(_ prefix: String) async throws -> ItemActionResult {
        try await folderAction(prefix, "queue-encode", body: ["notes": "Retry unfinished episodes.", "bypass_schedule": false])
    }

    func answerMotion(_ facts: ItemFacts, compress: Bool) async throws -> ItemActionResult {
        if compress {
            return try await compressAsIs(facts.prefix, facts.motionFiles.filter(\.needsMotionChoice).map(\.id))
        }
        return try await keepMotionOriginals(facts.prefix, facts.motionFiles.map(\.id))
    }

    func compressAsIs(_ prefix: String, _ ids: [Int], reconsiderKept: Bool = false) async throws -> ItemActionResult {
        try await folderAction(prefix, "compress-as-is", body: ["library_item_ids": ids, "reconsider_kept": reconsiderKept])
    }

    func keepMotionOriginals(_ prefix: String, _ libraryItemIds: [Int]) async throws -> ItemActionResult {
        try await folderAction(prefix, "keep-motion-original", body: ["library_item_ids": libraryItemIds])
    }

    func setSound(_ showPrefix: String, _ preset: String) async throws -> ItemActionResult {
        try await folderAction(showPrefix, "audio-preset", body: ["preset": preset])
    }

    func setStillAiring(_ seriesPrefix: String, _ mode: String) async throws -> ItemActionResult {
        try await folderAction(seriesPrefix, "series-lifecycle", body: ["mode": mode])
    }

    private func folderAction(_ prefix: String, _ action: String, body: [String: Any]) async throws -> ItemActionResult {
        var request = URLRequest(url: baseURL.appending(path: "api/folders").appending(path: prefix).appending(path: action))
        request.httpMethod = "POST"
        if action == "put-original-back" { request.timeoutInterval = 3600 }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 400, 409].contains((response as? HTTPURLResponse)?.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder.mediaforce.decode(ItemActionResult.self, from: data)
    }
}
