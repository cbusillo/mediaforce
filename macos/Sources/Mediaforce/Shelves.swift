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
    let seasons: [MainWindowFacts.FinishedSeason]
    let posters: PosterStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Finished lately")
                .font(.headline)
            if seasons.isEmpty {
                Text("Finished seasons appear here.")
                    .foregroundStyle(.secondary)
            }
            HStack(alignment: .top, spacing: 10) {
                ForEach(seasons) { season in
                    VStack(alignment: .leading, spacing: 5) {
                        PosterView(
                            show: season.show,
                            image: posters.image(for: season.showPrefix),
                            headline: Words.finishedSaved(season),
                            detail: season.season.map { "Season \($0)" } ?? season.show
                        )
                        Text(Words.day(season.finishedAt))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .help(Words.finishedSummary(season))
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
            Text("Samples")
                .font(.headline)
            HStack(alignment: .top, spacing: 10) {
                ForEach(Array((samples.waitingOnYou + samples.approvedNotStarted).prefix(6).enumerated()), id: \.element.id) { index, sample in
                    VStack(alignment: .leading, spacing: 7) {
                        PosterView(
                            show: sample.show,
                            image: posters.image(for: sample.showPrefix),
                            headline: sample.season.map { "Season \($0)" } ?? sample.show,
                            detail: Words.sampleState(sample)
                        )
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
    static let rowLimit = 9
    static let headerHeight: CGFloat = 28
    static let rowHeight: CGFloat = 24
    static let noteHeight: CGFloat = 16

    let shows: [MainWindowFacts.NotStartedShow]
    let samples: [MainWindowFacts.Sample]
    let openSample: (String) -> Void

    var body: some View {
        let largest = Double(shows.map(\.bytes).max() ?? 1)
        VStack(alignment: .leading, spacing: 10) {
            Text("Biggest shows not started")
                .font(.headline)
            Table(shows) {
                TableColumn("Show") { (show: MainWindowFacts.NotStartedShow) in
                    VStack(alignment: .leading, spacing: 1) {
                        Text(show.show)
                        if let note = waitingNote(show), let prefix = show.waitingSamplePrefix {
                            Button(note) { openSample(prefix) }
                                .buttonStyle(.plain)
                                .font(.caption)
                                .foregroundStyle(Color.accentColor)
                        }
                    }
                    .padding(.vertical, waitingNote(show) == nil ? 0 : 2)
                }
                TableColumn("Size") { (show: MainWindowFacts.NotStartedShow) in
                    HStack(spacing: 8) {
                        SizeBar(fraction: Double(show.bytes) / largest)
                        Text(Words.size(show.bytes))
                            .monospacedDigit()
                            .frame(width: 58, alignment: .trailing)
                    }
                    .accessibilityElement(children: .combine)
                }
                .width(94)
                TableColumn("Episodes") { (show: MainWindowFacts.NotStartedShow) in
                    Text("\(show.episodeCount)")
                        .monospacedDigit()
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                .width(58)
                TableColumn("Seasons") { (show: MainWindowFacts.NotStartedShow) in
                    Text("\(show.seasonCount)")
                        .monospacedDigit()
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                .width(52)
            }
            .tableStyle(.inset(alternatesRowBackgrounds: true))
            .scrollDisabled(true)
            .frame(height: Self.headerHeight + Self.rowHeight * CGFloat(shows.count) + Self.noteHeight * CGFloat(shows.filter { waitingNote($0) != nil }.count))
        }
    }

    private func waitingNote(_ show: MainWindowFacts.NotStartedShow) -> String? {
        guard let prefix = show.waitingSamplePrefix,
              let sample = samples.first(where: { $0.prefix == prefix }) else { return nil }
        let season = sample.season.map { "Season \($0)" } ?? "A season"
        return "\(season): \(Words.sampleState(sample).lowercased())"
    }
}

struct Computers: View {
    let computers: [MainWindowFacts.Computer]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Computers")
                .font(.headline)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 7) {
                ForEach(computers) { computer in
                    GridRow {
                        Image(systemName: Words.computerSymbol(computer))
                            .foregroundStyle(.secondary)
                            .frame(width: 22)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(computer.name ?? computer.key)
                            Text(Words.computerState(computer.state))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .padding(.leading, 6)
        }
    }
}

struct Latest: View {
    let events: [MainWindowFacts.Event]

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
                        Text(Words.eventSentence(event))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .font(.callout)
        }
    }
}
