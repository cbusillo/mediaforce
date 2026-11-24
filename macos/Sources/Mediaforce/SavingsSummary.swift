import SwiftUI

struct SavingsHistory: Decodable, Sendable {
    struct Week: Decodable, Identifiable, Sendable {
        let start: String
        let bytes: Int64
        var id: String { start }
        var date: Date? { ActivityFacts.date(start + "T12:00:00Z") }
        var label: String {
            guard let date else { return start }
            return date.formatted(Date.FormatStyle(timeZone: .gmt).month(.abbreviated).day())
        }
    }

    struct Win: Decodable, Identifiable, Sendable {
        let prefix: String
        let title: String
        let mediaType: String
        let before: Int64
        let after: Int64
        var id: String { prefix }
    }

    struct Published: Decodable, Sendable {
        let saved: Int64
        let current: Int64
        let complete: Bool
    }

    var publishedSizes: [String: Published]? = nil
    let weeks: [Week]
    let wins: [Win]

    func finish(remaining: Int64?, at date: Date?) -> Date? {
        let recent = weeks.dropLast().suffix(4)
        let saved = recent.reduce(0) { $0 + $1.bytes }
        guard recent.count == 4, saved > 0, let remaining, remaining > 0, let date else { return nil }
        let days = ceil(28 * Double(remaining) / Double(saved))
        guard days < 365 * 100 else { return nil }
        return Calendar.current.date(byAdding: .day, value: Int(days), to: date)
    }
}

struct SavingsSummary: View {
    let facts: MainWindowFacts
    let listing: MediaListing
    let machines: PageFacts<ComputersFacts>
    let posters: PosterStore

    var body: some View {
        let saved = facts.spaceSaved.bytes
        let totals = facts.library
        let after = totals.tv.totalBytes - totals.tv.originalBytes + totals.movies.totalBytes - totals.movies.originalBytes
            + totals.other.totalBytes - totals.other.originalBytes
        VStack(alignment: .leading, spacing: 30) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Saved all time").foregroundStyle(.secondary)
                Text(Words.size(saved)).font(.system(size: 30, weight: .bold)).monospacedDigit()
                Text("\(Words.count(facts.spaceSaved.itemCount, "file")) · \(Words.size(after + saved)) before, \(Words.size(after)) after")
                    .foregroundStyle(.secondary)
                if let history = facts.spaceSaved.history {
                    SavingsCurve(history: history, observed: ActivityFacts.date(facts.observedAt) ?? .now).padding(.top, 12)
                }
            }
            if let history = facts.spaceSaved.history, let sizes = history.publishedSizes {
                let library = SavingsLibrary(rows: listing.rows, sizes: sizes)
                if !library.groups.isEmpty {
                    SavingsRing(library: library, today: totals.tv.totalBytes + totals.movies.totalBytes,
                                posters: posters, leader: history.wins.first?.prefix)
                }
                SavingsDetails(history: history, saved: saved, movies: totals.movies, machines: machines.facts?.computers ?? [])
                if machines.refreshFailed { Text("Computer totals didn’t refresh. Retrying.").foregroundStyle(.secondary) }
                SavingsMedals(history: history, library: library, saved: saved, remaining: remaining.bytes)
            } else {
                Text("Savings history is unavailable.").foregroundStyle(.secondary)
            }
            storage
        }
        .font(.system(size: 13))
    }

    private var remaining: MediaListing.SavingsEstimate { listing.savingsEstimate(for: [.tv, .movies]) }

    private struct Segment: Identifiable {
        let label: String
        let bytes: Int64?
        let color: Color
        let caption: String
        let tip: String
        var id: String { label }
    }

    private var storage: some View {
        let saved = facts.spaceSaved.bytes
        let tv = listing.savingsEstimate(for: [.tv]).bytes
        let movies = listing.savingsEstimate(for: [.movies]).bytes
        let total = facts.library.tv.totalBytes + facts.library.movies.totalBytes + saved
        let rest = max(0, total - saved - (tv ?? 0) - (movies ?? 0))
        let segments = [
            Segment(label: "Saved", bytes: saved, color: .green, caption: "Saved \(Words.size(saved))",
                    tip: "\(Words.size(saved)) already back across \(Words.count(facts.spaceSaved.itemCount, "file"))"),
            Segment(label: "TV shows to go", bytes: tv, color: .blue, caption: "TV shows \(tv.map(Words.size) ?? "—") to go",
                    tip: "At least \(tv.map(Words.size) ?? "—") still to get back from \(Words.count(facts.library.tv.showCount, "show"))"),
            Segment(label: "Movies to go", bytes: movies, color: .purple, caption: "Movies \(movies.map(Words.size) ?? "—") to go",
                    tip: "At least \(movies.map(Words.size) ?? "—") still to get back from \(Words.count(facts.library.movies.titleCount, "movie"))"),
            Segment(label: "Compressed size and the rest", bytes: rest, color: .gray.opacity(0.26), caption: "Compressed size and the rest",
                    tip: "What the library still takes up after compressing")
        ]
        return VStack(alignment: .leading, spacing: 8) {
            Text("Left to save").fontWeight(.semibold)
            Text(finish).foregroundStyle(.secondary)
            GeometryReader { geo in
                let sum = segments.reduce(Int64.zero) { $0 + ($1.bytes ?? 0) }
                HStack(spacing: 0) {
                    ForEach(segments) { segment in
                        segment.color.frame(width: geo.size.width * Double(segment.bytes ?? 0) / Double(max(1, sum)))
                            .help("\(segment.label)\n\(segment.tip)")
                    }
                }.clipShape(RoundedRectangle(cornerRadius: 6))
            }.frame(height: 22)
            HStack(spacing: 16) {
                ForEach(segments) { segment in
                    HStack(spacing: 5) {
                        Circle().fill(segment.color).frame(width: 9, height: 9)
                        Text(segment.caption)
                    }
                }
            }.font(.system(size: 12)).foregroundStyle(.secondary)
            ForEach(listing.unavailable) { kind in Text(Words.libraryReadFailure(kind, hasPrevious: listing.hasLoaded(kind))) }
        }
    }

    private var finish: String {
        guard let bytes = remaining.bytes else { return listing.loading ? "Reading the libraries…" : "Waiting for library estimates." }
        let size = (remaining.partial ? "At least " : "") + Words.size(bytes)
        guard let date = facts.spaceSaved.history?.finish(remaining: bytes, at: ActivityFacts.date(facts.observedAt)) else { return size }
        return "\(size) · Estimated finish \(date.formatted(.dateTime.month(.wide).year()))"
    }
}
