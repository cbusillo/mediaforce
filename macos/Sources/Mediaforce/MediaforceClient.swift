import AppKit
import Foundation

struct MediaforceClient: Sendable {
    static let factsTimeout: TimeInterval = 30

    let baseURL: URL
    var session: URLSession = .shared

    func facts() async throws -> MainWindowFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/main-window"))
        request.timeoutInterval = Self.factsTimeout
        let (data, response) = try await session.data(for: request)
        try Self.requireSuccess(response)
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: data)
    }

    func poster(showPrefix: String) async -> NSImage? {
        let url = baseURL.appending(path: "api/app/posters").appending(path: showPrefix)
        guard let (data, response) = try? await session.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else {
            return nil
        }
        return NSImage(data: data)
    }

    func pause() async throws {
        try await post("api/encode-queue/pause")
    }

    func resume() async throws {
        try await post("api/encode-queue/resume")
    }

    func deleteAllOriginalBackups() async throws {
        try await post("api/completed/backups/clear", body: Data("{}".utf8))
    }

    private func post(_ path: String, body: Data? = nil) async throws {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (_, response) = try await session.data(for: request)
        try Self.requireSuccess(response)
    }

    private static func requireSuccess(_ response: URLResponse) throws {
        guard let status = (response as? HTTPURLResponse)?.statusCode, (200..<300).contains(status) else {
            throw URLError(.badServerResponse)
        }
    }
}
