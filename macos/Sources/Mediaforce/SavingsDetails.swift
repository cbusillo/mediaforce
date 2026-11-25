import SwiftUI

struct SavingsDetails: View {
    let history: SavingsHistory
    let saved: Int64
    let movies: MainWindowFacts.Library.Movies
    let machines: [ComputersFacts.Machine]
    private var best: SavingsHistory.Week? { history.weeks.max { $0.bytes < $1.bytes } }
    private var average: Double { Double(movies.totalBytes) / Double(max(1, movies.titleCount)) }
    private var movieCount: Int { average > 0 ? Int((Double(saved) / average).rounded()) : 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Details").fontWeight(.semibold)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 14), GridItem(.flexible())], spacing: 14) {
                card("Best week", .orange, best.map { Words.size($0.bytes) } ?? "—", best.map { "Week of " + $0.label } ?? "") {
                    HStack(spacing: 0) {
                        ForEach(history.weeks.suffix(8)) { week in
                            let d = 6 + 19 * sqrt(Double(max(0, week.bytes)) / Double(max(1, best?.bytes ?? 0)))
                            Circle().fill(.orange.opacity(week.id == best?.id ? 1 : 0.32))
                                .frame(width: d, height: d).frame(maxWidth: .infinity)
                                .help("Week of \(week.label)\n\(Words.size(week.bytes)) saved" + (week.id == best?.id ? " · best week" : ""))
                        }
                    }
                }
                let win = history.wins.first
                card("Most saved", .green, win.map { Words.size($0.before - $0.after) } ?? "—",
                     win.map { "\($0.title), \(Words.size($0.before)) to \(Words.size($0.after))" } ?? "") {
                    if let win, win.before > 0 {
                        HStack(alignment: .bottom, spacing: 5) {
                            RoundedRectangle(cornerRadius: 6).fill(.gray.opacity(0.35)).frame(width: 66, height: 66)
                            Image(systemName: "arrow.right").font(.caption).foregroundStyle(.tertiary).frame(height: 66)
                            let side = 66 * sqrt(max(0, Double(win.after) / Double(win.before)))
                            RoundedRectangle(cornerRadius: 3).fill(.green).frame(width: side, height: side)
                        }
                    }
                }
                let avg = (average / 1e9).formatted(.number.precision(.fractionLength(1))) + " GB"
                card("Equal to", .purple, average > 0 ? Words.count(movieCount, "movie") : "—",
                     average > 0 ? "At your average movie size, \(avg)" : "") {
                    Canvas { context, size in
                        let count = min(885, movieCount)
                        for index in 0..<count {
                            let tile = CGRect(x: CGFloat(index % 59) * size.width / 59, y: CGFloat(index / 59) * 4,
                                              width: size.width / 59 * 0.72, height: 3.2)
                            context.fill(Path(roundedRect: tile, cornerRadius: 0.4), with: .color(.purple.opacity(0.85)))
                        }
                    }
                    .frame(height: 60)
                    .help("\(Words.count(movieCount, "movie"))\n\(Words.size(saved)) saved ÷ \(avg), the average size of your \(Words.count(movies.titleCount, "movie"))")
                }
                let sorted = machines.filter { $0.done.all.bytesSaved > 0 }.sorted { $0.done.all.bytesSaved > $1.done.all.bytesSaved }
                let top = sorted.first
                card("By computer", .blue, top.map { Words.size($0.done.all.bytesSaved) } ?? "—",
                     top.map(computerDetail) ?? "") {
                    GeometryReader { geo in
                        let sum = sorted.reduce(Int64.zero) { $0 + $1.done.all.bytesSaved }
                        HStack(spacing: 0) {
                            ForEach(sorted) { machine in
                                let bytes = machine.done.all.bytesSaved
                                Capsule().fill(.blue.opacity(machine.id == top?.id ? 1 : 0.35))
                                    .padding(.horizontal, 1.5)
                                    .frame(width: geo.size.width * Double(bytes) / Double(max(1, sum)), height: 14)
                                    .help(computerHelp(machine))
                            }
                        }
                    }.frame(height: 14)
                }
            }
        }
    }

    func computerDetail(_ machine: ComputersFacts.Machine) -> String {
        "\(machine.name ?? machine.key), \(percent(machine.done.all.bytesSaved))% of the total"
    }

    func computerHelp(_ machine: ComputersFacts.Machine) -> String {
        let bytes = machine.done.all.bytesSaved
        return "\(machine.name ?? machine.key)\n\(Words.size(bytes)) saved · \(percent(bytes))% of the total"
    }

    private func percent(_ bytes: Int64) -> Int { Int((100 * Double(bytes) / Double(max(saved, 1))).rounded()) }

    private func card<Graphic: View>(_ title: String, _ tint: Color, _ value: String, _ detail: String,
                                     @ViewBuilder graphic: () -> Graphic) -> some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Circle().fill(tint).frame(width: 8, height: 8)
                    Text(title).foregroundStyle(tint).font(.system(size: 12, weight: .semibold))
                }
                Text(value).font(.system(size: 17, weight: .semibold))
                Text(detail).foregroundStyle(.secondary)
            }.frame(maxWidth: .infinity, alignment: .leading)
            graphic().frame(width: 150)
        }
        .padding(.horizontal, 14).frame(minHeight: 90)
        .background(Color.gray.opacity(0.09), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct SavingsMedals: View {
    let history: SavingsHistory
    let library: SavingsLibrary
    let saved: Int64
    let remaining: Int64?
    let partial: Bool

    private func passed(_ threshold: Int64) -> SavingsHistory.Week? {
        var sum: Int64 = 0
        return history.weeks.first { sum += $0.bytes; return sum >= threshold }
    }

    var body: some View {
        let best = history.weeks.max { $0.bytes < $1.bytes }
        let full = library.shows.filter { $0.finished == $0.slices.count && $0.saved > 0 }.max { $0.saved < $1.saved }
        let leader = library.shows.max { $0.saved < $1.saved }
        let half = remaining.flatMap { bytes -> Int64? in
            if partial && bytes <= saved { return nil }
            return max(0, (bytes - saved) / 2)
        }
        VStack(alignment: .leading, spacing: 12) {
            Text("Milestones").fontWeight(.semibold)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 104, maximum: 104), spacing: 18, alignment: .top)], alignment: .leading, spacing: 12) {
                threshold(2_000_000_000_000)
                let weekEarned = (best?.bytes ?? 0) >= 500_000_000_000
                medal("500 GB in a week", weekEarned ? best?.label ?? "" : "Not yet", "bolt", .orange, weekEarned,
                      best.map { "\(Words.size($0.bytes)) in the week of \($0.label), the most in a single week." } ?? "Not yet.")
                medal(full?.name ?? "All seasons", full.map { "All \(Words.count($0.slices.count, "season"))" } ?? "Not yet",
                      "tv", .blue, full != nil,
                      full.map { "All \($0.slices.count) seasons compressed: \(Words.size($0.before)) to \(Words.size($0.current))." } ?? "Not yet.")
                let earned = (leader?.saved ?? 0) >= 500_000_000_000
                medal("Half a terabyte", earned ? (leader?.name ?? "") + " alone" : "Not yet", "star", .purple, earned,
                      leader.map { "\($0.name) has given back \(Words.size($0.saved)) so far, with \($0.slices.count - $0.finished) seasons still to go." } ?? "Not yet.")
                threshold(3_000_000_000_000)
                let left = half.map { $0 > 0 ? "About \(SavingsSummary.approximateSize($0)) to go" : "Half saved" } ?? "—"
                medal("Halfway", left, "circle.lefthalf.filled", .green, half == 0, "Halfway through what can be saved. \(left)")
            }
        }
    }

    private func threshold(_ bytes: Int64) -> some View {
        let week = passed(bytes)
        let title = "\(bytes / 1_000_000_000_000) TB saved"
        let text = week.map { "Week of \($0.label)" } ?? "\(Words.size(max(0, bytes - saved))) to go"
        return medal(title, text, "checkmark", .green, saved >= bytes,
                     week.map { "Passed \(title) in the week of \($0.label)." } ?? text)
    }

    private func medal(_ title: String, _ subtitle: String, _ symbol: String, _ color: Color, _ earned: Bool,
                       _ tip: String) -> some View {
        VStack(spacing: 6) {
            Circle()
                .fill(earned ? AnyShapeStyle(RadialGradient(colors: [color.mix(with: .white, by: 0.35), color, color.mix(with: .black, by: 0.22)],
                                                           center: UnitPoint(x: 0.35, y: 0.3), startRadius: 0, endRadius: 47))
                      : AnyShapeStyle(.gray.opacity(0.22)))
                .overlay { Circle().strokeBorder(earned ? .white.opacity(0.32) : .gray.opacity(0.17), lineWidth: earned ? 3 : 2) }
                .shadow(color: .black.opacity(earned ? 0.13 : 0), radius: 3, y: 2)
                .overlay { Image(systemName: symbol).font(.system(size: 27)).foregroundStyle(earned ? .white : .gray) }
                .frame(width: 64, height: 64)
            Text(title).fontWeight(.semibold)
            Text(subtitle).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity).multilineTextAlignment(.center).font(.system(size: 12)).help("\(title)\n\(tip)")
    }
}
