import SwiftUI

@MainActor
@Observable
final class PosterStore {
    private let client: MediaforceClient
    private(set) var images: [String: NSImage] = [:]
    private var requested: Set<String> = []

    init(client: MediaforceClient) {
        self.client = client
    }

    /// A show's or movie's poster by its title prefix ("tv/Show" or "movies/Title (Year)"), fetched once.
    func image(for titlePrefix: String) -> NSImage? {
        if !requested.contains(titlePrefix) {
            requested.insert(titlePrefix)
            Task { images[titlePrefix] = await client.poster(titlePrefix: titlePrefix) }
        }
        return images[titlePrefix]
    }
}

struct PosterView: View {
    static let nameSizes: [CGFloat] = [12, 10.5, 9]

    static func nameSize(_ name: String, fitting width: CGFloat) -> CGFloat {
        let longestWord = name.split(separator: " ").map(String.init).max { $0.count < $1.count } ?? name
        return nameSizes.first { size in
            let font = NSFont.systemFont(ofSize: size, weight: .semibold)
            return (longestWord as NSString).size(withAttributes: [.font: font]).width <= width
        } ?? nameSizes[nameSizes.count - 1]
    }

    let show: String
    let image: NSImage?
    let headline: String
    let detail: String
    var width: CGFloat = 84

    var body: some View {
        let height = (width / 210 * 295).rounded()
        ZStack(alignment: .bottom) {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: width, height: height)
                    .clipped()
            } else {
                Text(show)
                    .font(.system(size: Self.nameSize(show, fitting: width - 12), weight: .semibold))
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                    .truncationMode(.tail)
                    .frame(width: width - 12)
                    .padding(.bottom, 30)
                    .padding(8)
                    .frame(width: width, height: height)
                    .background(Color.primary.opacity(0.06))
            }
            VStack(alignment: .leading, spacing: 0) {
                Text(headline)
                    .font(.system(size: 12.5, weight: .semibold))
                    .monospacedDigit()
                    .minimumScaleFactor(0.75)
                Text(detail)
                    .font(.system(size: 10))
                    .opacity(0.82)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .lineLimit(1)
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 6)
            .padding(.vertical, 5)
            .background(.ultraThinMaterial)
            .environment(\.colorScheme, .dark)
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.14), lineWidth: 0.5)
        )
    }
}

struct FinishedShelf: View {
    let items: [MainWindowFacts.FinishedSeason]
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Finished lately")
                .font(.headline)
            if items.isEmpty {
                Text("Nothing finished yet.")
                    .foregroundStyle(.secondary)
            }
            HStack(alignment: .top, spacing: 10) {
                ForEach(items) { item in
                    Button { open(.item(item.prefix)) } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            PosterView(
                                show: item.show,
                                image: posters.image(for: item.showPrefix),
                                headline: Words.finishedSaved(item),
                                detail: item.season.map { "Season \($0)" } ?? item.show
                            )
                            Text(Words.day(item.finishedAt))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                    .pointerStyle(.link)
                    .help(Words.finishedSummary(item))
                }
            }
        }
    }
}

struct SampleShelf: View {
    let facts: MainWindowFacts
    private var samples: MainWindowFacts.Samples { facts.samples }
    let posters: PosterStore
    let openSample: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Approved samples")
                .font(.headline)
            ScrollView(.horizontal) {
                LazyHStack(alignment: .top, spacing: 10) {
                    ForEach(Array(samples.approvedNotStarted.enumerated()), id: \.element.id) { index, sample in
                        VStack(alignment: .leading, spacing: 7) {
                            Button { openSample(sample.prefix) } label: {
                                PosterView(
                                    show: sample.show,
                                    image: posters.image(for: sample.showPrefix),
                                    headline: sample.season.map { "Season \($0)" } ?? sample.show,
                                    detail: Words.sampleState(sample)
                                )
                            }
                            .buttonStyle(.plain)
                            .pointerStyle(.link)
                            .help([Words.season(sample.show, sample.season), Words.sampleFailure(sample.failure)].compactMap { $0 }.joined(separator: ": "))
                            if let action = Words.sampleAction(sample.action) {
                                let open = { openSample(sample.prefix) }
                                if index == 0 {
                                    Button(action, action: open)
                                        .buttonStyle(.borderedProminent)
                                        .controlSize(.small)
                                } else {
                                    Button(action, action: open)
                                        .controlSize(.small)
                                }
                            } else if let reason = Words.waitingReason(sample, facts) {
                                Text(reason)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .frame(width: 84, alignment: .leading)
                            }
                        }
                    }
                }
            }
        }
    }
}

struct SizeBar: View {
    let fraction: Double
    private let track: CGFloat = 30

    var body: some View {
        ZStack(alignment: .leading) {
            Capsule().fill(Color.primary.opacity(0.08))
            Capsule().fill(Color.primary.opacity(0.38))
                .frame(width: max(2, track * fraction))
        }
        .frame(width: track, height: 4)
        .accessibilityHidden(true)
    }
}

struct BiggestTable: View {
    static let headerHeight: CGFloat = 28
    // Tall enough for the poster beside each name.
    static let rowHeight: CGFloat = 29
    static let noteHeight: CGFloat = 16
    // A poster that fits inside a row.
    static let posterWidth: CGFloat = 14

    let titles: [MainWindowFacts.NotStartedTitle]
    let samples: [MainWindowFacts.Sample]
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
        let largest = Double(titles.map(\.bytes).max() ?? 1)
        VStack(alignment: .leading, spacing: 10) {
            Text("Biggest not started")
                .font(.headline)
            Table(titles) {
                TableColumn("Title") { (title: MainWindowFacts.NotStartedTitle) in
                    HStack(spacing: 8) {
                        MediaPoster(titlePrefix: title.showPrefix, name: title.show, kind: MediaKind(libraryType: title.mediaType), posters: posters, width: Self.posterWidth)
                        VStack(alignment: .leading, spacing: 1) {
                            Button(title.show) { open(.item(title.showPrefix)) }
                                .buttonStyle(QuietLinkStyle(primary: true))
                            if let note = waitingNote(title), let prefix = title.waitingSamplePrefix {
                                HStack(spacing: 4) {
                                    // The note beside it already says it waits.
                                    WaitingMark().accessibilityHidden(true)
                                    Button(note) { open(.sample(prefix)) }
                                        .buttonStyle(QuietLinkStyle())
                                }
                                .font(.caption)
                            }
                        }
                    }
                    .padding(.vertical, waitingNote(title) == nil ? 0 : 2)
                }
                TableColumn("Size") { (title: MainWindowFacts.NotStartedTitle) in
                    HStack(spacing: 8) {
                        SizeBar(fraction: Double(title.bytes) / max(1, largest))
                        Text(Words.size(title.bytes))
                            .monospacedDigit()
                            .frame(width: 58, alignment: .trailing)
                    }
                    .accessibilityElement(children: .combine)
                }
                .width(94)
                TableColumn("Details") { (title: MainWindowFacts.NotStartedTitle) in
                    if MediaKind(libraryType: title.mediaType) == .tv {
                        Text([Words.count(title.episodeCount, "episode"), Words.count(title.seasonCount, "season")].joined(separator: " · "))
                    } else {
                        Text(title.mediaType == "movie" ? "Movie" : Words.mediaKind(.other))
                    }
                }
                .width(160)
            }
            .tableStyle(.inset(alternatesRowBackgrounds: true))
            .scrollDisabled(true)
            .frame(height: Self.headerHeight + Self.rowHeight * CGFloat(titles.count) + Self.noteHeight * CGFloat(titles.filter { waitingNote($0) != nil }.count))
        }
    }

    private func waitingNote(_ title: MainWindowFacts.NotStartedTitle) -> String? {
        guard let prefix = title.waitingSamplePrefix,
              let sample = samples.first(where: { $0.prefix == prefix }) else { return nil }
        let season = sample.season.map { "Season \($0)" } ?? "A season"
        return "\(season): \(Words.sampleState(sample).lowercased())"
    }
}

struct Latest: View {
    let events: [MainWindowFacts.Event]
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Latest")
                .font(.headline)
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 8) {
                ForEach(events) { event in
                    GridRow {
                        Text(Words.moment(event.at))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                            .fixedSize()
                        // The poster sits centred beside the sentence; the row keeps the time on its first line.
                        HStack(spacing: 8) {
                            MediaPoster(titlePrefix: MediaPoster.titlePrefix(event.relPath), name: Words.eventSentence(event),
                                        kind: MediaKind(libraryType: event.mediaType ?? (event.season == nil ? "other" : "tv")),
                                        posters: posters, width: 16)
                            Button(Words.eventSentence(event)) { open(.item(event.itemPrefix)) }
                                .buttonStyle(QuietLinkStyle(primary: true))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .font(.callout)
        }
    }
}
