import Charts
import SwiftUI

struct SavingsHistory: Decodable, Sendable {
    struct Week: Decodable, Identifiable, Sendable {
        let start: String
        let bytes: Int64
        var id: String { start }
        var label: String {
            guard let date = ActivityFacts.date(start + "T12:00:00Z") else { return start }
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
    let openItem: (String) -> Void
    private var history: SavingsHistory? { facts.spaceSaved.history }

    var body: some View {
        HStack(alignment: .center, spacing: 32) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Saved so far").font(.headline)
                Text(Words.size(facts.spaceSaved.bytes))
                    .font(.system(size: 40, weight: .medium, design: .rounded))
                    .monospacedDigit()
                Text("Across \(Words.count(facts.spaceSaved.itemCount, "file"))")
                    .foregroundStyle(.secondary)
            }
            .fixedSize()
            Divider()
            VStack(alignment: .leading, spacing: 12) {
                Text("Saved each week").font(.headline)
                if let weeks = history?.weeks, weeks.contains(where: { $0.bytes != 0 }) {
                    Chart(weeks) { week in
                        BarMark(x: .value("Week of", week.label), y: .value("GB saved", Double(week.bytes) / 1e9),
                                width: .ratio(0.52))
                            .cornerRadius(3)
                            .foregroundStyle(Color.teal)
                            .accessibilityLabel("Week of \(week.label)")
                            .accessibilityValue(Words.size(week.bytes))
                    }
                    .chartYAxis {
                        AxisMarks(position: .leading, values: .automatic(desiredCount: 3)) { value in
                            AxisGridLine()
                            AxisValueLabel {
                                if let gb = value.as(Double.self) { Text("\(gb.formatted()) GB") }
                            }
                        }
                    }
                    .frame(height: 96)
                } else {
                    Text(history == nil ? "Weekly totals are unavailable." : "No space saved in the past eight weeks.")
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 6)
        Divider()
        if let wins = history?.wins, !wins.isEmpty {
            VStack(alignment: .leading, spacing: 13) {
                Text("Biggest wins").font(.title3.weight(.semibold))
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 20, alignment: .leading), count: wins.count), alignment: .leading) {
                    ForEach(wins) { win in
                        Button { openItem(win.prefix) } label: {
                            HStack(spacing: 12) {
                                MediaPoster(titlePrefix: win.prefix, name: win.title, kind: MediaKind(libraryType: win.mediaType),
                                            posters: posters, width: 96)
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(win.title).font(.headline).lineLimit(3)
                                    Text("\(Words.size(win.before - win.after)) saved")
                                        .font(.system(.title3, design: .rounded, weight: .semibold))
                                    Text("\(Words.size(win.before)) to \(Words.size(win.after))")
                                        .font(.callout).foregroundStyle(.secondary)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .pointerStyle(.link)
                        .help("Open \(win.title)")
                    }
                }
            }
            Divider()
        }
        HStack(alignment: .top, spacing: 32) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Left to save").font(.title3.weight(.semibold))
                ForEach(MediaKind.libraries(in: facts.library)) { kind in
                    let estimate = listing.savingsEstimate(for: [kind])
                    HStack {
                        Label(Words.mediaKind(kind), systemImage: kind.symbol)
                        Spacer()
                        Text(estimate.viewOnly ? Words.libraryState(.cannotCompressYet) : Words.estimatedTotalSavingsValue(estimate.bytes, partial: estimate.partial))
                            .monospacedDigit()
                    }
                }
                ForEach(listing.unavailable) { kind in
                    Text(Words.libraryReadFailure(kind, hasPrevious: listing.hasLoaded(kind))).font(.callout)
                }
                Text(finishLine).font(.callout).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
            VStack(alignment: .leading, spacing: 12) {
                Text("Saved by computer").font(.title3.weight(.semibold))
                if let computers = machines.facts?.computers {
                    let largest = computers.map(\.done.all.bytesSaved).max() ?? 0
                    ForEach(computers.sorted { $0.done.all.bytesSaved > $1.done.all.bytesSaved }) { machine in
                        VStack(alignment: .leading, spacing: 5) {
                            HStack {
                                Label(machine.name ?? machine.key, systemImage: "desktopcomputer")
                                Spacer()
                                Text(Words.size(machine.done.all.bytesSaved)).monospacedDigit()
                            }
                            ProgressView(value: Double(max(0, machine.done.all.bytesSaved)), total: Double(max(1, largest)))
                                .tint(.teal).accessibilityHidden(true)
                        }
                    }
                    if computers.isEmpty { Text("No computers listed.").foregroundStyle(.secondary) }
                } else if !machines.refreshFailed {
                    ProgressView().controlSize(.small)
                }
                if machines.refreshFailed { Text("Computer totals didn’t refresh. Retrying.").foregroundStyle(.secondary) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .labelStyle(.titleAndIcon)
    }

    private var finishLine: String {
        if listing.loading { return "Reading the libraries…" }
        let estimate = listing.savingsEstimate(for: MediaKind.libraries(in: facts.library))
        if estimate.bytes == 0 && !estimate.partial { return "No more savings expected." }
        guard let finish = history?.finish(remaining: estimate.bytes, at: ActivityFacts.date(facts.observedAt)) else {
            return "Too soon to estimate a finish date."
        }
        let month = finish.formatted(.dateTime.month(.wide).year())
        return estimate.partial ? "At this pace, around \(month) for titles with estimates." : "At this pace, around \(month) to finish."
    }
}
