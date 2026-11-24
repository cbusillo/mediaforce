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
    @Environment(\.savingsDesign) private var design
    private var history: SavingsHistory? { facts.spaceSaved.history }

    var body: some View {
        switch design {
        case .footprint:
            SavingsFootprint(facts: facts)
            if let weeks = history?.weeks { SavingsWeeks(weeks: weeks) }
            biggestWins
        case .cinema:
            HStack(alignment: .center, spacing: 24) {
                SavingsTicket(facts: facts)
                if history?.wins.isEmpty == false { biggestWins }
            }
            if let weeks = history?.weeks { SavingsWeeks(weeks: weeks) }
        }
        if history == nil {
            Text("Weekly savings and biggest wins are unavailable.").foregroundStyle(.secondary)
        }
        Divider()
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
                    ForEach(computers.sorted { $0.done.all.bytesSaved > $1.done.all.bytesSaved }) { machine in
                        HStack {
                            Label(machine.name ?? machine.key, systemImage: "desktopcomputer")
                            Spacer()
                            Text(Words.size(machine.done.all.bytesSaved)).monospacedDigit()
                        }
                    }
                    if computers.isEmpty {
                        Text("No computers listed.").foregroundStyle(.secondary)
                    } else if computers.reduce(0, { $0 + $1.done.all.bytesSaved }) != facts.spaceSaved.bytes {
                        Text("These totals cover work recorded for the listed computers.")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                } else if !machines.refreshFailed {
                    ProgressView().controlSize(.small)
                }
                if machines.refreshFailed { Text("Computer totals didn’t refresh. Retrying.").foregroundStyle(.secondary) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .labelStyle(.titleAndIcon)
    }

    @ViewBuilder
    private var biggestWins: some View {
        if let wins = history?.wins, !wins.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("Biggest wins").font(.title3.weight(.semibold))
                HStack(alignment: .top, spacing: 14) {
                    ForEach(wins) { win in
                        Button { openItem(win.prefix) } label: {
                            let layout = design == .cinema ? AnyLayout(VStackLayout(alignment: .leading, spacing: 9))
                                : AnyLayout(HStackLayout(spacing: 12))
                            layout {
                                MediaPoster(titlePrefix: win.prefix, name: win.title, kind: MediaKind(libraryType: win.mediaType),
                                            posters: posters, width: design == .cinema ? 100 : 54)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(win.title).font(.headline).lineLimit(2)
                                    Text("\(Words.size(win.before - win.after)) saved")
                                        .font(.system(.title3, design: design == .cinema ? .serif : .rounded, weight: .semibold))
                                    Text("\(Words.size(win.before)) to \(Words.size(win.after))")
                                        .font(.callout).foregroundStyle(.secondary)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(design == .cinema ? 0 : 10)
                            .background(.quaternary.opacity(design == .cinema ? 0 : 0.42), in: RoundedRectangle(cornerRadius: 9))
                        }
                        .buttonStyle(.plain)
                        .pointerStyle(.link)
                        .help("Open \(win.title)")
                    }
                }
            }
        }
    }

    private var finishLine: String {
        if listing.loading { return "Reading the libraries…" }
        let estimate = listing.savingsEstimate(for: MediaKind.libraries(in: facts.library))
        if estimate.bytes == 0 && !estimate.partial { return "No more savings expected." }
        guard estimate.bytes != nil else { return "Waiting for library estimates." }
        if let weeks = history?.weeks, weeks.count >= 5, weeks.dropLast().suffix(4).allSatisfy({ $0.bytes == 0 }) {
            return "No space was saved in the last four full weeks."
        }
        guard let finish = history?.finish(remaining: estimate.bytes, at: ActivityFacts.date(facts.observedAt)) else {
            return "Too soon to estimate a finish date."
        }
        let month = finish.formatted(.dateTime.month(.wide).year())
        return estimate.partial ? "At this pace, around \(month) for titles with estimates." : "At this pace, around \(month) to finish."
    }
}
