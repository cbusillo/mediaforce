import AppKit
import SwiftUI

struct RenderOptions {
    let output: URL
    var server = ServerConnection.localServer
    var pages: [String] = []
    var size = NSSize(width: 1440, height: 1000)

    init(arguments: [String]) throws {
        guard let directory = arguments.first, !directory.hasPrefix("--") else {
            throw RenderFailure("Usage: Mediaforce render OUTPUT_DIR [--server URL] [--page PAGE] [--width POINTS] [--height POINTS]")
        }
        output = URL(fileURLWithPath: directory, isDirectory: true)
        var remaining = arguments.dropFirst()
        while let option = remaining.first {
            remaining = remaining.dropFirst()
            guard let value = remaining.first else { throw RenderFailure("Missing value for \(option)") }
            remaining = remaining.dropFirst()
            switch option {
            case "--server":
                guard let url = URL(string: value), ["http", "https"].contains(url.scheme), url.host != nil else {
                    throw RenderFailure("Invalid server URL")
                }
                server = url
            case "--page": pages.append(value)
            case "--width", "--height":
                let minimum = option == "--width" ? 1100.0 : 700.0
                guard let points = Double(value), points.isFinite, (minimum...4096).contains(points) else {
                    throw RenderFailure("\(option) must be between \(Int(minimum)) and 4096 points")
                }
                if option == "--width" { size.width = points } else { size.height = points }
            default: throw RenderFailure("Unknown option: \(option)")
            }
        }
    }
}

struct RenderFailure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

actor RenderLoads {
    private var pending = 0
    private var completed: Set<String> = []
    private var failures: [String: String] = [:]
    private var changed = ContinuousClock.now

    func startPage() {
        completed.removeAll()
        failures.removeAll()
        changed = .now
    }

    func begin(_ path: String) {
        pending += 1
        changed = .now
    }

    func finish(_ path: String, failure: String? = nil) {
        pending -= 1
        completed.insert(path)
        failures[path] = failure
        changed = .now
    }

    func wait(for paths: Set<String>) async throws {
        let deadline = ContinuousClock.now + .seconds(45)
        while ContinuousClock.now < deadline {
            let requiredFailures = failures.filter { paths.contains($0.key) }
            if !requiredFailures.isEmpty {
                throw RenderFailure(requiredFailures.map { "\($0.key): \($0.value)" }.sorted().joined(separator: "\n"))
            }
            if pending == 0, paths.isSubset(of: completed), changed.duration(to: .now) >= .milliseconds(500) { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        throw RenderFailure("Page did not finish loading: \(paths.subtracting(completed).sorted()); \(pending) requests pending")
    }
}

@MainActor
enum RenderCommand {
    static let mainPages = ["library", "tv", "movies", "other", "needs-you", "activity", "space-saved", "media",
                            "settings:libraries", "settings:compression", "settings:computers", "settings:metadata"]

    @discardableResult
    static func run(_ options: RenderOptions, session: URLSession = .shared) async throws -> [String: [URL]] {
        if FileManager.default.fileExists(atPath: options.output.path),
           !(try FileManager.default.contentsOfDirectory(atPath: options.output.path)).isEmpty {
            throw RenderFailure("Output directory is not empty. Use a new empty directory for this run: \(options.output.path)")
        }
        let loads = RenderLoads()
        let client = MediaforceClient(baseURL: options.server, session: session, renderLoads: loads)
        let model = MainWindowModel(client: client)
        await model.refresh()
        try await loads.wait(for: ["/api/app/main-window"])
        guard model.facts != nil else { throw RenderFailure("Main window did not load") }
        let pages = options.pages.isEmpty ? try await defaultPages(model) : options.pages
        try FileManager.default.createDirectory(at: options.output, withIntermediateDirectories: true)
        var rendered: [String: [URL]] = [:]
        for (index, page) in pages.enumerated() {
            for dark in [false, true] {
                let name = page.map { $0.isLetter || $0.isNumber || $0 == "-" ? $0 : "_" }
                let file = options.output.appendingPathComponent("\(index + 1)-\(String(name))-\(dark ? "dark" : "light").png")
                try await capture(page, model: model, loads: loads, size: options.size, dark: dark, file: file)
                rendered[page, default: []].append(file)
                print(file.path)
            }
        }
        return rendered
    }

    private static func defaultPages(_ model: MainWindowModel) async throws -> [String] {
        guard let kinds = model.kinds else { throw RenderFailure("Kinds did not load for the default page set") }
        var pages = mainPages
        pages += kinds.kinds.map { "kind:\($0.kind)" }
        let shows = try await model.client.tvShows().shows
        if let show = shows.first {
            pages.append("item:\(show.showPrefix)")
            if let season = show.seasons.first { pages.append("item:\(season.prefix)") }
        }
        if let sample = (model.facts?.samples.waitingOnYou ?? []).first ?? model.facts?.samples.approvedNotStarted.first {
            pages.append("sample:\(sample.prefix)")
        }
        if let movie = try await model.client.movies().movies.first { pages.append("item:\(movie.prefix)") }
        if let other = try await model.client.otherMedia().items.first { pages.append("item:\(other.prefix)") }
        return pages
    }

    private static func capture(_ page: String, model: MainWindowModel, loads: RenderLoads, size: NSSize, dark: Bool, file: URL) async throws {
        await loads.startPage()
        let (view, paths) = try content(page, model: model)
        let previousAppearance = NSApp.appearance
        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        defer { NSApp.appearance = previousAppearance }
        let size = NSSize(width: page.hasPrefix("settings:") ? 760 : size.width, height: size.height)
        let host = NSHostingView(rootView: view.environment(\.colorScheme, dark ? .dark : .light)
            .frame(width: size.width, height: size.height))
        host.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        host.sceneBridgingOptions = [.title, .toolbars]
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: host.frame, styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        window.contentView = host
        defer {
            window.contentView = nil
            window.close()
        }
        host.layoutSubtreeIfNeeded()
        try await loads.wait(for: paths)
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        guard !window.isVisible else { throw RenderFailure("Render window became visible") }
        let frame = window.contentView!.superview!
        guard let bitmap = frame.bitmapImageRepForCachingDisplay(in: frame.bounds) else {
            throw RenderFailure("Cannot allocate page bitmap")
        }
        frame.effectiveAppearance.performAsCurrentDrawingAppearance {
            frame.cacheDisplay(in: frame.bounds, to: bitmap)
        }
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw RenderFailure("Cannot encode page PNG")
        }
        try png.write(to: file, options: .atomic)
    }

    private static func content(_ page: String, model: MainWindowModel) throws -> (AnyView, Set<String>) {
        let parts = page.split(separator: ":", maxSplits: 1).map(String.init)
        guard let name = parts.first else { throw RenderFailure("Page name is empty") }
        let value = parts.count > 1 ? parts[1] : ""
        if name == "originals" {
            guard let preview = model.facts?.originalPreviews?.first else { throw RenderFailure("No original-file request is waiting") }
            return (AnyView(OriginalApprovalView(preview: preview, model: model)), [])
        }
        if name == "settings" {
            let view: AnyView
            switch value {
            case "libraries": view = AnyView(LibrarySettingsTab(main: model).navigationTitle(Words.librariesTab))
            case "compression": view = AnyView(CompressionSettingsTab(main: model).navigationTitle(Words.compressionTab))
            case "computers": view = AnyView(ComputerSettingsTab(main: model).navigationTitle(Words.computersTab))
            case "metadata": view = AnyView(MetadataSettingsTab(main: model).navigationTitle(Words.metadataTab))
            default: throw RenderFailure("Unknown Settings page: \(value)")
            }
            let endpoint = value == "libraries" ? "library" : value
            return (view, ["/api/app/settings/\(endpoint)"])
        }
        let place: Place
        let paths: Set<String>
        switch page {
        case "library": place = .library; paths = libraryPaths
        case "tv": place = .libraryKind(.tv); paths = ["/api/app/tv-shows"]
        case "movies": place = .libraryKind(.movies); paths = ["/api/app/movies"]
        case "other": place = .libraryKind(.other); paths = ["/api/app/other"]
        case "needs-you": place = .needsYou; paths = []
        case "activity": place = .activity; paths = ["/api/app/activity"]
        case "space-saved": place = .spaceSaved; paths = ["/api/completed", "/api/archive-cleanup"]
        case "media": place = .media; paths = libraryPaths
        default:
            guard !value.isEmpty else { throw RenderFailure("Unknown page: \(page)") }
            switch name {
            case "item": place = .item(value); paths = ["/api/app/items/\(value)"]
            case "sample": place = .sample(value); paths = ["/api/app/samples/\(value)"]
            case "kind":
                guard let kinds = model.kinds else { throw RenderFailure("Kinds did not load") }
                guard kinds.kinds.contains(where: { $0.kind == value }) else {
                    throw RenderFailure("No live kind named \(value)")
                }
                place = .kind(value); paths = ["/api/app/kinds"]
            default: throw RenderFailure("Unknown page: \(page)")
            }
        }
        let view = MainPage(place: place, facts: model.facts!, model: model, open: { _ in })
            .toolbar { ToolbarItem(placement: .navigation) { PauseButton(facts: model.facts!, model: model) } }
            .frame(minWidth: 1100, minHeight: 700)
        return (AnyView(view), paths)
    }

    private static let libraryPaths: Set<String> = ["/api/app/tv-shows", "/api/app/movies", "/api/app/other"]

}
