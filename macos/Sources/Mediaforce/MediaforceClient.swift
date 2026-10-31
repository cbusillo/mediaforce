import AppKit
import Foundation

struct CleanupFolder: Decodable, Sendable {
    let fileCount: Int
    let totalSizeBytes: Int64
}

struct CleanupResult: Decodable, Sendable {
    let ok: Bool
    let removedCount: Int
    let removedSizeBytes: Int64
    let preservedCount: Int?
}

struct MediaforceClient: Sendable {
    static let factsTimeout: TimeInterval = 30
    static let computerActionTimeout: TimeInterval = 600

    let baseURL: URL
    var session: URLSession = .shared

    func facts() async throws -> MainWindowFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/main-window"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(MainWindowFacts.self, from: request)
    }

    func activity() async throws -> ActivityFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/activity"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(ActivityFacts.self, from: request)
    }

    func tvShows() async throws -> TVShowsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/tv-shows"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(TVShowsFacts.self, from: request)
    }

    func movies() async throws -> MoviesFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/movies"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(MoviesFacts.self, from: request)
    }

    func otherMedia() async throws -> OtherMediaFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/other"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(OtherMediaFacts.self, from: request)
    }

    func poster(titlePrefix: String) async -> NSImage? {
        let url = baseURL.appending(path: "api/app/posters").appending(path: titlePrefix)
        guard let (data, response) = try? await session.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else {
            return nil
        }
        return NSImage(data: data)
    }

    func sample(_ prefix: String) async throws -> SampleFacts {
        var request = URLRequest(url: samplePath(prefix))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(SampleFacts.self, from: request)
    }

    func approveSample(_ prefix: String, confirm: [String], draftHash: String, scope: String = "season") async throws -> SampleActionResult {
        try await sampleAction(prefix, "approve", body: ["confirm": confirm, "reviewed_draft_hash": draftHash, "approval_scope": scope])
    }

    func makeSample(_ prefix: String, size: String?) async throws -> SampleActionResult {
        try await sampleAction(prefix, "make", body: ["size": size ?? NSNull()])
    }

    func compressSeason(_ prefix: String) async throws -> SampleActionResult {
        try await sampleAction(prefix, "compress", body: [:])
    }

    func reviewMedia(_ path: String) -> URL {
        baseURL.appending(path: path)
    }

    func hosts() async throws -> HostsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/hosts").appending(queryItems: [URLQueryItem(name: "compact", value: "1")]))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(HostsFacts.self, from: request)
    }

    /// Runs one of a computer's fixes. Setting a computer up or starting it can take minutes, so these wait longer.
    func computerAction(_ action: ComputerAction, _ hostKey: String, password: String? = nil) async throws -> HostActionResult {
        let path = switch action {
        case .checkAgain: "api/hosts/retry-connection"
        case .start: "api/hosts/start"
        case .setUp: "api/hosts/prepare"
        case .trustAgain: "api/hosts/reset-trust"
        }
        var body = ["host_key": hostKey]
        if let password { body["remote_password"] = password }
        let data = try await post(path, body: JSONEncoder().encode(body), timeout: Self.computerActionTimeout)
        return try JSONDecoder.mediaforce.decode(HostActionResult.self, from: data)
    }

    func lookForNewFiles() async throws {
        _ = try await post("api/catalog/refresh")
    }

    func pause() async throws {
        _ = try await post("api/encode-queue/pause")
    }

    func resume() async throws {
        _ = try await post("api/encode-queue/resume")
    }

    func completed() async throws -> SpaceSavedFacts.Completed {
        var request = URLRequest(url: baseURL.appending(path: "api/completed"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(SpaceSavedFacts.Completed.self, from: request)
    }

    func cleanupFolder() async throws -> CleanupFolder {
        var request = URLRequest(url: baseURL.appending(path: "api/archive-cleanup"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(CleanupFolder.self, from: request)
    }

    func deleteCleanupFolder() async throws -> CleanupResult {
        let data = try await post("api/archive-cleanup/clear", body: Data("{}".utf8))
        return try JSONDecoder.mediaforce.decode(CleanupResult.self, from: data)
    }

    func librarySettings() async throws -> LibrarySettingsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/settings/library"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(LibrarySettingsFacts.self, from: request)
    }

    func saveLibrarySettings(_ save: LibrarySettingsSave) async throws -> LibrarySettingsSaveResult {
        try await saveSettings("api/app/settings/library", save, as: LibrarySettingsSaveResult.self)
    }

    func computerSettings() async throws -> ComputerSettingsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/settings/computers"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(ComputerSettingsFacts.self, from: request)
    }

    func saveComputerSettings(_ save: ComputerSettingsSave) async throws -> ComputerSettingsSaveResult {
        try await saveSettings("api/app/settings/computers", save, as: ComputerSettingsSaveResult.self)
    }

    func metadataSettings() async throws -> MetadataSettingsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/settings/metadata"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(MetadataSettingsFacts.self, from: request)
    }

    func saveMetadataSettings(_ save: MetadataSettingsSave) async throws -> MetadataSettingsSaveResult {
        try await saveSettings("api/app/settings/metadata", save, as: MetadataSettingsSaveResult.self)
    }

    func compressionSettings() async throws -> CompressionSettingsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/settings/compression"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(CompressionSettingsFacts.self, from: request)
    }

    func saveCompressionSettings(_ save: CompressionSettingsSave) async throws -> CompressionSettingsSaveResult {
        try await saveSettings("api/app/settings/compression", save, as: CompressionSettingsSaveResult.self)
    }

    func libraryTypePreview(key: String, type: String) async throws -> LibraryTypePreview {
        let body = try JSONSerialization.data(withJSONObject: ["key": key, "library_type": type])
        let data = try await post("api/settings/library-type-preview", body: body)
        return try JSONDecoder.mediaforce.decode(LibraryTypePreviewResult.self, from: data).preview
    }

    /// A Settings section save answers 400 with a refusal code the app words, so both 200 and 400 decode.
    private func saveSettings<Body: Encodable, Result: Decodable>(
        _ path: String, _ save: Body, as type: Result.Type
    ) async throws -> Result {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
        request.httpBody = try JSONEncoder.mediaforce.encode(save)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 400].contains((response as? HTTPURLResponse)?.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder.mediaforce.decode(type, from: data)
    }

    private func samplePath(_ prefix: String) -> URL {
        baseURL.appending(path: "api/app/samples").appending(path: prefix)
    }

    private func sampleAction(_ prefix: String, _ action: String, body: [String: Any]) async throws -> SampleActionResult {
        var request = URLRequest(url: samplePath(prefix).appending(path: action))
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 409].contains((response as? HTTPURLResponse)?.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder.mediaforce.decode(SampleActionResult.self, from: data)
    }

    private func post(_ path: String, body: Data? = nil, timeout: TimeInterval? = nil) async throws -> Data {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
        if let timeout { request.timeoutInterval = timeout }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        try Self.requireSuccess(response)
        return data
    }

    private func decode<Value: Decodable>(_ type: Value.Type, from request: URLRequest) async throws -> Value {
        let (data, response) = try await session.data(for: request)
        try Self.requireSuccess(response)
        return try JSONDecoder.mediaforce.decode(type, from: data)
    }

    private static func requireSuccess(_ response: URLResponse) throws {
        guard let status = (response as? HTTPURLResponse)?.statusCode, (200..<300).contains(status) else {
            throw URLError(.badServerResponse)
        }
    }
}
