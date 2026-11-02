import AppKit
import XCTest
@testable import Mediaforce

@MainActor
final class RenderTests: XCTestCase {
    func testMainPagesRenderWithFixtureDataInBothAppearances() async throws {
        guard let directory = ProcessInfo.processInfo.environment["MEDIAFORCE_RENDER_OUTPUT"] else {
            throw XCTSkip("The render CI step supplies MEDIAFORCE_RENDER_OUTPUT")
        }
        let options = try RenderOptions(arguments: [directory])
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RenderFixtureServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        try await RenderCommand.run(options, session: session)
        let files = try FileManager.default.contentsOfDirectory(at: options.output, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "png" }
        XCTAssertEqual(files.count, (RenderCommand.mainPages.count + 6) * 2)
        for file in files {
            let bitmap = try XCTUnwrap(NSBitmapImageRep(data: Data(contentsOf: file)), file.path)
            if !file.lastPathComponent.contains("settings_") {
                XCTAssertGreaterThanOrEqual(bitmap.pixelsWide, Int(options.size.width))
            }
            XCTAssertGreaterThanOrEqual(bitmap.pixelsHigh, Int(options.size.height))
            var colors: Set<String> = []
            for y in stride(from: 0, to: bitmap.pixelsHigh, by: 7) {
                for x in stride(from: 0, to: bitmap.pixelsWide, by: 7) {
                    colors.insert(try XCTUnwrap(bitmap.colorAt(x: x, y: y)).description)
                }
            }
            XCTAssertGreaterThan(colors.count, 20, "Blank page: \(file.lastPathComponent)")
            if file.lastPathComponent.hasSuffix("light.png") {
                let dark = file.deletingLastPathComponent().appendingPathComponent(file.lastPathComponent.replacingOccurrences(of: "light.png", with: "dark.png"))
                XCTAssertNotEqual(try Data(contentsOf: file), try Data(contentsOf: dark))
            }
        }
    }

    func testInvalidRenderArgumentsFailBeforeLoading() {
        for arguments in [[], ["output", "--page"], ["output", "--width", "nan"], ["output", "--server", "file:///tmp"]] {
            XCTAssertThrowsError(try RenderOptions(arguments: arguments))
        }
    }

    func testAnUnreadablePageCannotBeReportedAsRendered() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let options = try RenderOptions(arguments: [directory.path, "--page", "activity"])
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [BrokenRenderServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        do {
            try await RenderCommand.run(options, session: session)
            XCTFail("Unreadable page succeeded")
        } catch {
            XCTAssertTrue(String(describing: error).contains("/api/app/activity"))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.appendingPathComponent("1-activity-light.png").path))
    }
}

private class RenderFixtureServer: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard request.httpMethod == "GET" else {
            client?.urlProtocol(self, didFailWithError: RenderFailure("Rendering attempted a write"))
            return
        }
        let path = request.url!.path
        let payload = payload(path)
        let response = HTTPURLResponse(url: request.url!, statusCode: payload == nil ? 404 : 200,
                                       httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data((payload ?? "").utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    func payload(_ path: String) -> String? {
        switch path {
        case "/api/app/main-window": MainWindowTests.facts
        case "/api/app/kinds": """
            {"kinds": [{"kind": "bright_live_action", "one_time": false, "seasons": 1, "waiting_bytes": 1000,
                        "shows": [], "settings": [], "approval": null, "sample_season": null}], "left_out": []}
            """
        case "/api/app/tv-shows": """
            {"shows": [{"show_prefix": "tv/Lucifer", "show": "Lucifer", "season_count": 1, "episode_count": 16,
                        "bytes": 60000000000, "seasons": [{"prefix": "tv/Lucifer/Season 2", "season": 2,
                        "episode_count": 16, "bytes": 60000000000, "needs_you": true, "state": "ready_to_compare"}]}]}
            """
        case "/api/app/movies": """
            {"movies": [{"prefix": "movies/Example", "title": "Example", "file_count": 1, "bytes": 1000, "state": "ready"}]}
            """
        case "/api/app/other": """
            {"items": [{"prefix": "other/Example", "title": "Example", "file_count": 1, "bytes": 1000, "state": "ready"}], "truncated": false}
            """
        case "/api/hosts": "{\"hosts\": []}"
        case "/api/app/activity": ActivityTests.payload
        case "/api/completed": "{\"folders\": []}"
        case "/api/archive-cleanup": "{\"file_count\": 0, \"total_size_bytes\": 0}"
        case "/api/app/settings/library": SettingsTests.serverFacts
        case "/api/app/settings/computers": SettingsTests.computerFacts
        case "/api/app/settings/metadata": SettingsTests.metadataFacts
        case "/api/app/settings/compression": SettingsTests.compressionFacts
        case let path where path.hasPrefix("/api/app/samples/"): SampleTests.facts()
        case let path where path.hasPrefix("/api/app/items/"):
            """
            {"prefix": "\(path.dropFirst("/api/app/items/".count))", "domain": "tv", "kind": "tv_season",
             "title": "Example", "show": "Lucifer", "show_prefix": "tv/Lucifer", "season": 2,
             "season_folder": "Season 2", "parent_prefix": "tv/Lucifer", "files": []}
            """
        default: nil
        }
    }

    override func stopLoading() {}
}

private final class BrokenRenderServer: RenderFixtureServer, @unchecked Sendable {
    override func payload(_ path: String) -> String? {
        path == "/api/app/activity" ? "unreadable" : super.payload(path)
    }
}
