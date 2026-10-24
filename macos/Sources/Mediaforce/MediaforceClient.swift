import Foundation

struct MediaforceClient: Sendable {
    static let factsTimeout: TimeInterval = 30

    let baseURL: URL
    var session: URLSession = .shared

    func facts() async throws -> MainWindowFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/main-window"))
        request.timeoutInterval = Self.factsTimeout
        let (data, response) = try await session.data(for: request)
        guard let status = (response as? HTTPURLResponse)?.statusCode, (200..<300).contains(status) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: data)
    }
}
