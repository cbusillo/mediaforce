import SwiftUI

enum SavingsDesign {
    case footprint, cinema
}

extension EnvironmentValues {
    @Entry var savingsDesign: SavingsDesign = .footprint
}

struct SavingsFootprint: View {
    let facts: MainWindowFacts
    private let mint = Color(red: 0.65, green: 0.93, blue: 0.78)
    private let ink = Color(red: 0.075, green: 0.22, blue: 0.20)

    var body: some View {
        let now = MediaKind.everything.totalBytes(in: facts.library) - MediaKind.everything.originalBytes(in: facts.library)
        let before = now + facts.spaceSaved.bytes
        HStack(spacing: 24) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Saved all time").font(.headline)
                Text(Words.size(facts.spaceSaved.bytes))
                    .font(.system(size: 57, weight: .medium, design: .rounded))
                Text("Across \(Words.count(facts.spaceSaved.itemCount, "finished file"))")
            }
            Spacer(minLength: 4)
            if before > 0, now >= 0, now <= before {
                let fraction = Double(now) / Double(before)
                VStack(alignment: .trailing, spacing: 2) {
                    Text((1 - fraction).formatted(.percent.precision(.fractionLength(0))))
                        .font(.system(size: 30, weight: .medium, design: .rounded))
                    Text("smaller").font(.callout)
                }
                .foregroundStyle(mint)
                Canvas { context, size in
                    let rect = CGRect(origin: .zero, size: size).insetBy(dx: 2, dy: 2)
                    let original = Path(ellipseIn: rect)
                    context.fill(original, with: .color(mint.opacity(0.13)))
                    context.clip(to: original)
                    for x in stride(from: -size.height, to: size.width, by: 6) {
                        var line = Path()
                        line.move(to: CGPoint(x: x, y: size.height))
                        line.addLine(to: CGPoint(x: x + size.height, y: 0))
                        context.stroke(line, with: .color(mint.opacity(0.45)), lineWidth: 0.8)
                    }
                    context.stroke(original, with: .color(mint), lineWidth: 2)
                    let diameter = rect.width * sqrt(fraction)
                    let smaller = CGRect(x: rect.midX - diameter / 2, y: rect.maxY - diameter, width: diameter, height: diameter)
                    context.fill(Path(ellipseIn: smaller), with: .color(mint))
                }
                .frame(width: 135, height: 135)
                .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Original files").font(.callout)
                    Text(Words.size(before)).font(.title2.weight(.semibold))
                    Text("After compression").font(.callout).padding(.top, 4)
                    Text(Words.size(now)).font(.title2.weight(.semibold)).foregroundStyle(mint)
                }
            }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 28)
        .padding(.vertical, 19)
        .background(ink, in: RoundedRectangle(cornerRadius: 16))
    }
}

struct SavingsTicket: View {
    let facts: MainWindowFacts
    @Environment(\.colorScheme) private var scheme
    private var paper: Color {
        scheme == .dark ? Color(red: 0.23, green: 0.16, blue: 0.15) : Color(red: 0.99, green: 0.93, blue: 0.82)
    }
    private var ink: Color {
        scheme == .dark ? Color(red: 1, green: 0.82, blue: 0.64) : Color(red: 0.36, green: 0.14, blue: 0.12)
    }

    var body: some View {
        let movies = facts.library.movies
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Saved all time").font(.headline)
                Text(Words.size(facts.spaceSaved.bytes))
                    .font(.system(size: 60, weight: .medium, design: .serif))
                Text("Across \(Words.count(facts.spaceSaved.itemCount, "finished file"))")
            }
            Canvas { context, size in
                var cut = Path()
                cut.move(to: CGPoint(x: 0, y: 1))
                cut.addLine(to: CGPoint(x: size.width, y: 1))
                context.stroke(cut, with: .color(ink.opacity(0.55)), style: StrokeStyle(lineWidth: 1.2, dash: [3, 4]))
            }
            .frame(height: 2)
            .accessibilityHidden(true)
            if movies.titleCount > 0, movies.totalBytes > 0 {
                let average = Double(movies.totalBytes) / Double(movies.titleCount)
                let count = Int(Double(max(0, facts.spaceSaved.bytes)) / average)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Room for about").font(.headline)
                    Text(Words.count(count, "movie")).font(.system(size: 30, weight: .semibold, design: .serif))
                    Text("At your library’s average movie size")
                        .font(.callout)
                }
            } else {
                Label(Words.count(facts.spaceSaved.episodeCount, "episode"), systemImage: "tv")
            }
        }
        .foregroundStyle(ink)
        .padding(.horizontal, 31)
        .padding(.vertical, 20)
        .frame(maxWidth: 345, alignment: .leading)
        .background {
            Canvas { context, size in
                let bounds = CGRect(origin: .zero, size: size)
                var ticket = Path(roundedRect: bounds, cornerRadius: 12)
                for side in [bounds.minX, bounds.maxX] {
                    ticket.addEllipse(in: CGRect(x: side - 9, y: bounds.midY - 9, width: 18, height: 18))
                }
                context.clip(to: Path(bounds))
                context.fill(ticket, with: .color(paper), style: FillStyle(eoFill: true))
                context.stroke(Path(roundedRect: bounds.insetBy(dx: 13, dy: 8), cornerRadius: 5),
                               with: .color(ink.opacity(0.44)), lineWidth: 1)
            }
        }
    }
}

struct SavingsWeeks: View {
    let weeks: [SavingsHistory.Week]

    var body: some View {
        let peak = Double(max(1, weeks.map(\.bytes).max() ?? 0))
        VStack(alignment: .leading, spacing: 9) {
            Text("Saved each week").font(.title3.weight(.semibold))
            HStack(spacing: 5) {
                ForEach(weeks) { week in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(week.label).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                        Text(Words.size(week.bytes)).font(.system(.body, design: .rounded, weight: .semibold))
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(9)
                    .background(Color.teal.opacity(0.07 + 0.30 * max(0, Double(week.bytes)) / peak), in: RoundedRectangle(cornerRadius: 5))
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }
}
