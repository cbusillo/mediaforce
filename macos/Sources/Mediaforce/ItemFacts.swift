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
        let percentComplete: Int?
        let sizeQuestion: SizeQuestion?
        let bytes: Int64
        let originalBytes: Int64?
        let originalBackup: Bool
        var id: Int { libraryItemId }
        // A whole show's page names the season too, since episode numbers repeat across seasons.
        var inShowPage = false

        private enum CodingKeys: String, CodingKey {
            case libraryItemId, relPath, name, season, episode, stage, validation, waitReason, additionalWaitReasons, keptReason, heldReason
            case percentComplete, sizeQuestion, bytes, originalBytes, originalBackup
        }

        var compressed: Bool { stage == "published" }
        var needsYou: Bool { stage == "needs_you" || !(additionalWaitReasons ?? []).isEmpty }
    }

    /// Why a file waits on the Director, as a code with any values its words need.
    struct WaitReason: Decodable, Sendable, Equatable {
        let code: String
        /// The computer a restart stopped it on.
        let computer: String?
        /// Why a failed copy could not be made again.
        let blockers: [String]?
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

    var waiting: [File] { files.filter(\.needsYou) }
    var compressedCount: Int { files.filter(\.compressed).count }
    var keptCount: Int { files.filter { $0.stage == "kept_original" || $0.stage == "held" }.count }
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
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        var facts = try JSONDecoder.mediaforce.decode(ItemFacts.self, from: data)
        facts.markShowPage()
        return facts
    }

    func player(_ libraryItemId: Int, _ side: PlayerSide) -> URL {
        baseURL.appending(path: "api/app/player/\(libraryItemId)/\(side.rawValue)")
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

    func retryUnfinished(_ prefix: String) async throws -> ItemActionResult {
        try await folderAction(prefix, "queue-encode", body: ["notes": "Retry unfinished episodes.", "bypass_schedule": false])
    }

    func keepMotionOriginals(_ prefix: String, _ libraryItemIds: [Int]) async throws -> ItemActionResult {
        try await folderAction(prefix, "keep-motion-original", body: ["library_item_ids": libraryItemIds])
    }

    func compressAsIs(_ prefix: String) async throws -> ItemActionResult {
        try await folderAction(prefix, "accept-ambiguous-motion", body: [:])
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
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 400, 409].contains((response as? HTTPURLResponse)?.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder.mediaforce.decode(ItemActionResult.self, from: data)
    }
}
