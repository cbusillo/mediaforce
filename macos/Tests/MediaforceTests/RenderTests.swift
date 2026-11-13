import AppKit
import os
import XCTest
@testable import Mediaforce

@MainActor
final class RenderTests: XCTestCase {
    func testOriginalReviewAndItsWaitingNoticeRender() async throws {
        let retained = ProcessInfo.processInfo.environment["MEDIAFORCE_ORIGINAL_RENDER_OUTPUT"]
        let directory = retained ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path
        defer { if retained == nil { try? FileManager.default.removeItem(atPath: directory) } }
        let options = try RenderOptions(arguments: [directory, "--server", "http://mediaforce.invalid", "--page", "needs-you", "--page", "originals"])
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [OriginalReviewRenderServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let rendered = try await RenderCommand.run(options, session: session)
        XCTAssertFalse(try XCTUnwrap(rendered["originals"]).isEmpty)
        XCTAssertFalse(try XCTUnwrap(rendered["needs-you"]).isEmpty)
    }

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
        try await keepNothingWaitingPage(in: directory)
    }

    // Copied beside the main pages after they are checked, because a render needs its own empty directory.
    private func keepNothingWaitingPage(in directory: String) async throws {
        let scratch = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: scratch) }
        let options = try RenderOptions(arguments: [scratch.path, "--server", "http://mediaforce.invalid", "--page", "needs-you"])
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NothingWaitingRenderServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let pages = try await RenderCommand.run(options, session: session)
        let rendered = try XCTUnwrap(pages["needs-you"])
        XCTAssertEqual(rendered.count, 2)
        for file in rendered {
            let name = "nothing-waiting-" + file.lastPathComponent
            try FileManager.default.copyItem(at: file, to: URL(fileURLWithPath: directory).appendingPathComponent(name))
        }
    }

    func testTheMenuShowsThePosterOfWhatItIsCompressing() async throws {
        let retained = ProcessInfo.processInfo.environment["MEDIAFORCE_MENU_RENDER_OUTPUT"]
        let directory = retained ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path
        defer { if retained == nil { try? FileManager.default.removeItem(atPath: directory) } }
        let options = try RenderOptions(arguments: [directory, "--server", "http://mediaforce.invalid", "--page", "menu"])
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CompressingRenderServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        CompressingRenderServer.requested.withLock { $0 = [] }
        let rendered = try await RenderCommand.run(options, session: session)
        XCTAssertFalse(try XCTUnwrap(rendered["menu"]).isEmpty)
        XCTAssertTrue(CompressingRenderServer.requested.withLock { $0.contains("/api/app/posters/\(CompressingRenderServer.show)") })
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
        let body = path.hasPrefix("/api/app/posters/")
            ? StandInPoster.png(String(path.dropFirst("/api/app/posters/".count)))
            : payload(path).map { Data($0.utf8) }
        let response = HTTPURLResponse(url: request.url!, statusCode: body == nil ? 404 : 200,
                                       httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body ?? Data())
        client?.urlProtocolDidFinishLoading(self)
    }

    func payload(_ path: String) -> String? {
        switch path {
        case "/api/app/main-window": Self.busierDay
        case "/api/app/kinds": """
            {"kinds": [{"kind": "bright_live_action", "one_time": false, "seasons": 1, "waiting_bytes": 1000,
                        "shows": [], "settings": [], "approval": null, "sample_season": null},
                       {"kind": "darker_live_action", "one_time": false, "seasons": 41, "waiting_bytes": 684000000000,
                        "shows": [{"prefix": "tv/Futurama", "seasons": 7, "waiting_bytes": 90000000000, "sample_season": "tv/Futurama/Season 5"},
                                  {"prefix": "tv/Lucifer", "seasons": 6, "waiting_bytes": 200000000000, "sample_season": "tv/Lucifer/Season 2"},
                                  {"prefix": "tv/ER", "seasons": 15, "waiting_bytes": 324000000000, "sample_season": "tv/ER/Season 1"},
                                  {"prefix": "tv/Dollhouse", "seasons": 2, "waiting_bytes": 30000000000, "sample_season": "tv/Dollhouse/Season 1"},
                                  {"prefix": "tv/Fringe", "seasons": 5, "waiting_bytes": 20000000000, "sample_season": "tv/Fringe/Season 1"},
                                  {"prefix": "tv/Angel", "seasons": 5, "waiting_bytes": 20000000000, "sample_season": "tv/Angel/Season 1"}],
                        "settings": [], "approval": null, "sample_season": "tv/Futurama/Season 5"}], "left_out": []}
            """
        case "/api/app/tv-shows": """
            {"shows": [{"show_prefix": "tv/Lucifer", "show": "Lucifer", "season_count": 1, "episode_count": 16,
                        "bytes": 60000000000, "estimated_saved_bytes": 50000000000, "seasons": [{"prefix": "tv/Lucifer/Season 2", "season": 2,
                        "episode_count": 16, "bytes": 60000000000, "needs_you": true, "state": "ready_to_compare"}]}]}
            """
        case "/api/app/movies": """
            {"movies": [{"prefix": "movies/Example", "title": "Example", "file_count": 1, "bytes": 1000, "estimated_saved_bytes": 600, "state": "ready"}]}
            """
        case "/api/app/other": """
            {"items": [{"prefix": "other/Example", "title": "Example", "file_count": 1, "bytes": 1000, "estimated_saved_bytes": 400, "state": "ready"}], "truncated": false}
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

    // The shared sample facts on a busier day, using only reasons the app already has.
    static let busierDay: String = {
        var facts = try! JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as! [String: Any]
        var work = facts["work"] as! [String: Any]
        work["kinds_needing_you"] = [["kind": "darker_live_action", "sample_season": "tv/Futurama/Season 5",
                                      "sample_status": "completed", "sample_ready": true, "one_time": false,
                                      "settings_available": false, "sample_start_prefix": NSNull()]]
        facts["work"] = work
        var samples = facts["samples"] as! [String: Any]
        var waiting = samples["waiting_on_you"] as! [[String: Any]]
        waiting.append(["prefix": "tv/ER/Season 1", "show_prefix": "tv/ER", "show": "ER", "season_folder": "Season 1",
                        "season": 1, "state": "needs_help", "action": "choose_size", "failure": "bound_exhausted"])
        waiting.append(["prefix": "tv/Futurama/Season 4", "show_prefix": "tv/Futurama", "show": "Futurama",
                        "season_folder": "Season 4", "season": 4, "state": "ready_to_compare", "action": "review_sample",
                        "failure": NSNull()])
        samples["waiting_on_you"] = waiting
        facts["samples"] = samples
        var biggest = facts["biggest_not_started"] as! [[String: Any]]
        biggest[0]["waiting_sample_prefix"] = "tv/ER/Season 1"
        facts["biggest_not_started"] = biggest
        return String(decoding: try! JSONSerialization.data(withJSONObject: facts), as: UTF8.self)
    }()

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

private final class NothingWaitingRenderServer: RenderFixtureServer, @unchecked Sendable {
    override func payload(_ path: String) -> String? {
        guard path == "/api/app/main-window", let original = super.payload(path),
              var facts = try? JSONSerialization.jsonObject(with: Data(original.utf8)) as? [String: Any],
              var work = facts["work"] as? [String: Any],
              var samples = facts["samples"] as? [String: Any],
              let computers = facts["computers"] as? [[String: Any]] else { return super.payload(path) }
        work["kinds_needing_you"] = []
        facts["work"] = work
        samples["waiting_on_you"] = []
        facts["samples"] = samples
        facts["computers"] = computers.map { $0.merging(["needs_you": false]) { _, new in new } }
        facts["original_backups"] = ["count": 0, "bytes": 0]
        return (try? JSONSerialization.data(withJSONObject: facts)).map { String(decoding: $0, as: UTF8.self) }
    }
}

private final class CompressingRenderServer: RenderFixtureServer, @unchecked Sendable {
    static let show = "tv/Futurama"
    static let requested = OSAllocatedUnfairLock<[String]>(initialState: [])

    override func startLoading() {
        Self.requested.withLock { $0.append(request.url!.path) }
        super.startLoading()
    }

    override func payload(_ path: String) -> String? {
        guard path == "/api/app/main-window", let original = super.payload(path),
              var facts = try? JSONSerialization.jsonObject(with: Data(original.utf8)) as? [String: Any],
              var work = facts["work"] as? [String: Any] else { return super.payload(path) }
        work["stop_reasons"] = []
        work["running_count"] = 1
        work["running_seasons"] = [["prefix": "\(Self.show)/Season 3", "show_prefix": Self.show, "show": "Futurama",
                                    "season_folder": "Season 3", "season": 3]]
        facts["work"] = work
        return (try? JSONSerialization.data(withJSONObject: facts)).map { String(decoding: $0, as: UTF8.self) }
    }
}

private final class OriginalReviewRenderServer: RenderFixtureServer, @unchecked Sendable {
    override func payload(_ path: String) -> String? {
        guard path == "/api/app/main-window", let original = super.payload(path),
              var facts = try? JSONSerialization.jsonObject(with: Data(original.utf8)) as? [String: Any],
              let preview = try? originalApprovalFixture() else { return super.payload(path) }
        facts["original_previews"] = [["preview_id": preview.previewId, "manifest_hash": preview.manifestHash,
                                       "canonical_json": preview.canonicalJson, "challenge": preview.challenge]]
        return (try? JSONSerialization.data(withJSONObject: facts)).map { String(decoding: $0, as: UTF8.self) }
    }
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
            ["library_item_id": 3, "name": "Lucifer.S02E03.mkv", "rel_path": "tv/Lucifer/Season 2/Lucifer.S02E03.mkv",
             "episode": 3, "stage": "needs_you", "wait_reason": ["code": "containment_unproven", "computer": "Attic Mini"],
             "bytes": 900000000, "original_backup": false],
        ]
        return (try? JSONSerialization.data(withJSONObject: item)).map { String(decoding: $0, as: UTF8.self) }
    }
}

// Drawn stand-in artwork so rendered pages show posters instead of grey boxes. Not real show art.
private enum StandInPoster {
    private struct Look {
        let top: NSColor
        let bottom: NSColor
        let glow: NSColor
        let font: String
        let title: NSColor
    }

    private static func look(_ show: String) -> Look {
        switch show {
        case "Lucifer":
            Look(top: NSColor(srgbRed: 0.07, green: 0.03, blue: 0.05, alpha: 1), bottom: NSColor(srgbRed: 0.45, green: 0.05, blue: 0.09, alpha: 1),
                 glow: NSColor(srgbRed: 0.98, green: 0.45, blue: 0.22, alpha: 0.85), font: "Didot-Bold", title: .white)
        case "Futurama":
            Look(top: NSColor(srgbRed: 0.05, green: 0.16, blue: 0.33, alpha: 1), bottom: NSColor(srgbRed: 0.10, green: 0.55, blue: 0.60, alpha: 1),
                 glow: NSColor(srgbRed: 0.98, green: 0.72, blue: 0.25, alpha: 1), font: "ArialRoundedMTBold",
                 title: NSColor(srgbRed: 1, green: 0.86, blue: 0.30, alpha: 1))
        case "ER":
            Look(top: NSColor(srgbRed: 0.04, green: 0.13, blue: 0.14, alpha: 1), bottom: NSColor(srgbRed: 0.16, green: 0.45, blue: 0.42, alpha: 1),
                 glow: NSColor(srgbRed: 0.85, green: 0.96, blue: 0.95, alpha: 0.7), font: "HelveticaNeue-CondensedBlack", title: .white)
        default:
            Look(top: NSColor(srgbRed: 0.12, green: 0.12, blue: 0.16, alpha: 1), bottom: NSColor(srgbRed: 0.35, green: 0.33, blue: 0.45, alpha: 1),
                 glow: NSColor(srgbRed: 0.9, green: 0.85, blue: 0.75, alpha: 0.7), font: "AvenirNext-Heavy", title: .white)
        }
    }

    static func png(_ titlePrefix: String) -> Data? {
        let show = titlePrefix.removingPercentEncoding?.split(separator: "/").last.map(String.init) ?? titlePrefix
        let look = look(show)
        let size = NSSize(width: 420, height: 590)
        guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height),
                                            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
              let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        let bounds = NSRect(origin: .zero, size: size)
        NSGradient(starting: look.bottom, ending: look.top)?.draw(in: bounds, angle: 90)
        let glow = NSGradient(colors: [look.glow, look.glow.withAlphaComponent(0)])
        switch show {
        case "Futurama":
            glow?.draw(in: NSBezierPath(ovalIn: NSRect(x: 40, y: 150, width: 340, height: 340)), relativeCenterPosition: NSPoint(x: -0.3, y: 0.3))
            look.glow.withAlphaComponent(0.55).setStroke()
            let ring = NSBezierPath(ovalIn: NSRect(x: -10, y: 270, width: 440, height: 90))
            ring.lineWidth = 7
            ring.stroke()
        case "ER":
            for (index, y) in stride(from: 120.0, to: 460, by: 58).enumerated() {
                look.glow.withAlphaComponent(0.10 + 0.06 * Double(index)).setFill()
                NSBezierPath(rect: NSRect(x: 0, y: y, width: size.width, height: 10)).fill()
            }
            NSColor(srgbRed: 0.85, green: 0.12, blue: 0.14, alpha: 0.9).setFill()
            NSBezierPath(rect: NSRect(x: 180, y: 250, width: 60, height: 170)).fill()
            NSBezierPath(rect: NSRect(x: 125, y: 305, width: 170, height: 60)).fill()
        default:
            glow?.draw(in: NSBezierPath(ovalIn: NSRect(x: -40, y: 90, width: 500, height: 380)), relativeCenterPosition: NSPoint(x: 0, y: -0.35))
            NSColor.black.withAlphaComponent(0.9).setFill()
            let skyline: [(Double, Double)] = [(0, 150), (34, 210), (70, 170), (96, 280), (128, 190), (160, 240), (196, 330),
                                               (226, 200), (262, 260), (300, 180), (330, 230), (366, 160), (396, 200)]
            for (x, height) in skyline {
                NSBezierPath(rect: NSRect(x: x, y: 0, width: 36, height: height)).fill()
            }
            look.glow.withAlphaComponent(0.6).setFill()
            for (x, height) in skyline where height > 200 {
                for row in stride(from: 40.0, to: height - 20, by: 26) {
                    NSBezierPath(rect: NSRect(x: x + 10, y: row, width: 5, height: 8)).fill()
                    NSBezierPath(rect: NSRect(x: x + 22, y: row + 10, width: 5, height: 8)).fill()
                }
            }
        }
        let font = NSFont(name: look.font, size: show.count <= 3 ? 120 : 58) ?? .systemFont(ofSize: 58, weight: .heavy)
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.5)
        shadow.shadowBlurRadius = 8
        let title = NSAttributedString(string: show, attributes: [.font: font, .foregroundColor: look.title,
                                                                  .paragraphStyle: style, .shadow: shadow, .kern: 1.5])
        let height = title.boundingRect(with: NSSize(width: size.width - 40, height: 400), options: .usesLineFragmentOrigin).height
        title.draw(with: NSRect(x: 20, y: size.height - 50 - height, width: size.width - 40, height: height), options: .usesLineFragmentOrigin)
        NSGraphicsContext.restoreGraphicsState()
        return bitmap.representation(using: .png, properties: [:])
    }
}
