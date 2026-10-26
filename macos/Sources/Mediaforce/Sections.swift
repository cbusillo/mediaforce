import SwiftUI

struct WorkStatus: View {
    let facts: MainWindowFacts
    private var work: MainWindowFacts.Work { facts.work }
    private var lastChange: MainWindowFacts.Event? { facts.lastChange }

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: symbol)
                .font(.system(size: 24, weight: .regular))
                .foregroundStyle(.secondary)
                .frame(width: 32, alignment: .center)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 4) {
                Text(Words.workHeadline(facts))
                    .font(.title3.weight(.semibold))
                if let explanation = Words.workExplanation(facts) {
                    Text(explanation)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 640, alignment: .leading)
                }
                if let lastChange {
                    Text("Last change: \(Words.event(lastChange)), \(Words.moment(lastChange.at))")
                        .font(.callout)
                        .foregroundStyle(.tertiary)
                        .padding(.top, 2)
                }
            }
            Spacer(minLength: 0)
        }
    }

    private var symbol: String {
        if work.stopReasons.contains("storage_unavailable") { return "externaldrive.badge.xmark" }
        if work.runningCount == 0, work.waitingCount == 0, !facts.samples.waitingOnYou.isEmpty { return "hand.raised" }
        switch work.state {
        case "paused": return "pause.circle"
        case "running": return "gearshape.2"
        case "stopped": return "exclamationmark.circle"
        default: return "checkmark.circle"
        }
    }
}

struct StorageBar: View {
    let facts: MainWindowFacts
    // The saved part opens Space saved; each show's slice opens that show.
    let open: (Place) -> Void
    private let barHeight: CGFloat = 40

    var body: some View {
        let saved = Double(facts.spaceSaved.bytes)
        let original = Double(facts.library.tv.originalBytes)
        let total = max(saved + original, 1)
        GeometryReader { geo in
            let width = geo.size.width
            let savedWidth = max(width * saved / total, 0)
            let pointsPerByte = (width - savedWidth) / max(original, 1)
            let slicesWidth = facts.biggestNotStarted.reduce(0) { $0 + Double($1.bytes) * pointsPerByte }

            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 1) {
                    ZStack(alignment: .leading) {
                        Rectangle().fill(Color.accentColor)
                        Text("\(Words.size(facts.spaceSaved.bytes)) saved")
                            .font(.callout.weight(.semibold))
                            .foregroundStyle(.white)
                            .lineLimit(1)
                            .fixedSize()
                            .padding(.leading, 11)
                    }
                    .frame(width: savedWidth)
                    .contentShape(Rectangle())
                    .onTapGesture { open(.spaceSaved) }
                    .pointerStyle(.link)
                    .help("\(Words.size(facts.spaceSaved.bytes)) saved from \(facts.spaceSaved.episodeCount) episodes in \(facts.spaceSaved.seasonCount) seasons")

                    ForEach(facts.biggestNotStarted) { show in
                        Rectangle()
                            .fill(Color.primary.opacity(0.17))
                            .frame(width: max(1, Double(show.bytes) * pointsPerByte - 1))
                            .contentShape(Rectangle())
                            .onTapGesture { open(.item(show.showPrefix)) }
                            .pointerStyle(.link)
                            .help("\(show.show): \(Words.size(show.bytes)), not started")
                    }

                    ZStack(alignment: .trailing) {
                        Rectangle().fill(Color.primary.opacity(0.09))
                        Text("\(Words.size(facts.library.tv.originalBytes)) of TV still original")
                            .font(.callout.weight(.medium))
                            .foregroundStyle(.secondary)
                            .padding(.trailing, 12)
                    }
                    .help("\(Words.size(facts.library.tv.originalBytes)) of TV is still in its original form")
                }
                .frame(height: barHeight)
                .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))

                ZStack(alignment: .topLeading) {
                    Text("\(facts.spaceSaved.episodeCount) episodes")
                        .foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 3) {
                        Path { path in
                            path.move(to: CGPoint(x: 0.5, y: 0))
                            path.addLine(to: CGPoint(x: 0.5, y: 4))
                            path.addLine(to: CGPoint(x: slicesWidth - 0.5, y: 4))
                            path.addLine(to: CGPoint(x: slicesWidth - 0.5, y: 0))
                        }
                        .stroke(Color.secondary.opacity(0.7), lineWidth: 1)
                        .frame(width: slicesWidth, height: 5)
                        Text("Biggest shows not started")
                            .foregroundStyle(.secondary)
                            .fixedSize()
                    }
                    .offset(x: savedWidth + 1)
                    Text("\(facts.library.tv.showCount) shows, \(facts.library.tv.seasonCount) seasons, \(facts.library.tv.episodeCount.formatted()) episodes in all")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                .font(.caption)
                .monospacedDigit()
            }
        }
        .frame(height: barHeight + 7 + 26)
    }
}
