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

    let baseURL: URL
    var session: URLSession = .shared

    func facts() async throws -> MainWindowFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/main-window"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(MainWindowFacts.self, from: request)
    }

    func poster(showPrefix: String) async -> NSImage? {
        let url = baseURL.appending(path: "api/app/posters").appending(path: showPrefix)
        guard let (data, response) = try? await session.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else {
            return nil
        }
        return NSImage(data: data)
    }

    func webPage(_ path: String) -> URL {
        baseURL.appending(path: path)
    }

    func seasonPage(_ prefix: String) -> URL {
        baseURL.appending(path: "folders").appending(path: prefix)
    }

    func pause() async throws {
        _ = try await post("api/encode-queue/pause")
    }

    func resume() async throws {
        _ = try await post("api/encode-queue/resume")
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

    private func post(_ path: String, body: Data? = nil) async throws -> Data {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
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
