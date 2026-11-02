import AppKit
import XCTest
@testable import Mediaforce

@MainActor
final class RenderTests: XCTestCase {
    func testMainPagesRenderWithFixtureDataInBothAppearances() async throws {
        let retained = ProcessInfo.processInfo.environment["MEDIAFORCE_RENDER_OUTPUT"]
        let directory = retained ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path
        defer { if retained == nil { try? FileManager.default.removeItem(atPath: directory) } }
        let options = try RenderOptions(arguments: [directory, "--server", "http://mediaforce.invalid"])
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RenderFixtureServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let rendered = try await RenderCommand.run(options, session: session)
        let files = try FileManager.default.contentsOfDirectory(at: options.output, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "png" }
        XCTAssertEqual(Set(files.map(\.lastPathComponent)), Set(rendered.values.flatMap { $0 }.map(\.lastPathComponent)))
        XCTAssertTrue(Set(RenderCommand.mainPages).isSubset(of: Set(rendered.keys)))
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
                let darkBitmap = try XCTUnwrap(NSBitmapImageRep(data: Data(contentsOf: dark)))
                XCTAssertGreaterThan(try backgroundBrightness(bitmap), try backgroundBrightness(darkBitmap))
            }
        }
    }

    func testBlockedFilesAndKeptOriginalsRenderOnNativePages() async throws {
        let retained = ProcessInfo.processInfo.environment["MEDIAFORCE_BLOCKED_RENDER_OUTPUT"]
        let directory = retained ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path
        defer { if retained == nil { try? FileManager.default.removeItem(atPath: directory) } }
        let pages = ["sample:tv/Lucifer/Season 2", "item:tv/Lucifer/Season 2"]
        let options = try RenderOptions(arguments: [directory, "--server", "http://mediaforce.invalid"] + pages.flatMap { ["--page", $0] })
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [BlockedFilesRenderServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let rendered = try await RenderCommand.run(options, session: session)
        for page in pages { XCTAssertFalse(try XCTUnwrap(rendered[page]).isEmpty) }
    }

    private func backgroundBrightness(_ bitmap: NSBitmapImageRep) throws -> CGFloat {
        let color = try XCTUnwrap(bitmap.colorAt(x: bitmap.pixelsWide - 8, y: bitmap.pixelsHigh / 2)?.usingColorSpace(.sRGB))
        return color.redComponent + color.greenComponent + color.blueComponent
    }

    func testOptionalMissingFactsStillRenderTheirPages() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let pages = ["item:tv/Lucifer/Season 2", "item:tv/Lucifer", "settings:computers"]
        let options = try RenderOptions(arguments: [directory.path, "--server", "http://mediaforce.invalid"] + pages.flatMap { ["--page", $0] })
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MissingOptionalRenderServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let rendered = try await RenderCommand.run(options, session: session)
        for page in pages { XCTAssertFalse(try XCTUnwrap(rendered[page]).isEmpty) }
    }

    func testExistingOutputIsPreservedInsteadOfMixedWithNewCaptures() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let old = directory.appendingPathComponent("old-light.png")
        let previous = Data("previous capture".utf8)
        try previous.write(to: old)
        do {
            try await RenderCommand.run(RenderOptions(arguments: [directory.path]))
            XCTFail("Existing evidence was mixed with a new run")
        } catch is RenderFailure {}
        XCTAssertEqual(try Data(contentsOf: old), previous)
    }

    func testInvalidRenderArgumentsFailBeforeLoading() {
        for arguments in [[], ["output", "--page"], ["output", "--width", "nan"], ["output", "--server", "file:///tmp"]] {
            XCTAssertThrowsError(try RenderOptions(arguments: arguments))
        }
    }

    func testAnUnreadablePageCannotBeReportedAsRendered() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let options = try RenderOptions(arguments: [directory.path, "--server", "http://mediaforce.invalid", "--page", "activity"])
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [BrokenRenderServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        do {
            try await RenderCommand.run(options, session: session)
            XCTFail("Unreadable page succeeded")
        } catch is RenderFailure {}
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
            item(String(path.dropFirst("/api/app/items/".count)))
        default: nil
        }
    }

    private func item(_ prefix: String) -> String {
        let kind = prefix == "tv/Lucifer" ? "tv_series" : prefix.hasPrefix("movies/") ? "movie" : prefix.hasPrefix("other/") ? "other" : "tv_season"
        return """
        {"prefix": "\(prefix)", "domain": "\(prefix.split(separator: "/").first!)", "kind": "\(kind)",
         "title": "Example", "show": "Lucifer", "show_prefix": "tv/Lucifer", "season": 2,
         "season_folder": "Season 2", "parent_prefix": "tv/Lucifer",
         "files": [{"library_item_id": 1, "rel_path": "\(prefix)/Example.mkv", "name": "Example.mkv", "episode": 1,
                    "stage": "published", "bytes": 1000, "original_bytes": 2000, "original_backup": true}]}
        """
    }

    override func stopLoading() {}
}

private final class BrokenRenderServer: RenderFixtureServer, @unchecked Sendable {
    override func payload(_ path: String) -> String? {
        path == "/api/app/activity" ? "unreadable" : super.payload(path)
    }
}

private final class MissingOptionalRenderServer: RenderFixtureServer, @unchecked Sendable {
    override func payload(_ path: String) -> String? {
        path.hasPrefix("/api/app/samples/") || ["/api/app/kinds", "/api/app/tv-shows", "/api/hosts"].contains(path)
            ? nil : super.payload(path)
    }
}

private final class BlockedFilesRenderServer: RenderFixtureServer, @unchecked Sendable {
    override func payload(_ path: String) -> String? {
        if path.hasPrefix("/api/app/samples/") {
            return SampleTests.facts(state: "needs_help", action: "review_files", choices: "[]")
        }
        guard let original = super.payload(path), path.hasPrefix("/api/app/items/"),
              var item = try? JSONSerialization.jsonObject(with: Data(original.utf8)) as? [String: Any] else {
            return super.payload(path)
        }
        item["files"] = [
            ["library_item_id": 1, "name": "Lucifer.S02E01.mkv", "rel_path": "tv/Lucifer/Season 2/Lucifer.S02E01.mkv",
             "episode": 1, "stage": "kept_original", "kept_reason": "check_failed", "bytes": 900000000, "original_backup": false],
            ["library_item_id": 2, "name": "Lucifer.S02E02.mkv", "rel_path": "tv/Lucifer/Season 2/Lucifer.S02E02.mkv",
             "episode": 2, "stage": "needs_you", "validation": "size_held", "bytes": 900000000, "original_backup": false],
        ]
        return (try? JSONSerialization.data(withJSONObject: item)).map { String(decoding: $0, as: UTF8.self) }
    }
}
