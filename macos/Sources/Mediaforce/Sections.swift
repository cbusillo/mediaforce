import SwiftUI

/// What the work's state in the title bar means, when it needs saying, and the last change, which opens its season.
struct WorkStatus: View {
    let facts: MainWindowFacts
    let open: (Place) -> Void
    private var lastChange: MainWindowFacts.Event? { facts.lastChange }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let explanation = Words.workExplanation(facts) {
                Text(explanation)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 640, alignment: .leading)
            }
            if let lastChange {
                let line = "Last change: \(Words.event(lastChange)), \(Words.moment(lastChange.at))"
                Group {
                    if let season = lastChange.season {
                        Button(line) { open(.item(season.prefix)) }
                            .buttonStyle(QuietLinkStyle())
                    } else {
                        Text(line).foregroundStyle(.tertiary)
                    }
                }
                .font(.callout)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct StorageBar: View {
    let facts: MainWindowFacts
    let kind: MediaKind
    let open: (Place) -> Void

    var body: some View {
        HStack(spacing: 32) {
            metric(Words.size(kind.totalBytes(in: facts.library)), label: "In the library")
            metric(Words.size(kind.originalBytes(in: facts.library)), label: "Still original")
            if kind == .everything {
                Button { open(.spaceSaved) } label: {
                    metric(Words.size(facts.spaceSaved.bytes), label: "Space saved")
                }
                .buttonStyle(.plain)
                .pointerStyle(.link)
            }
            Spacer()
        }
    }

    private func metric(_ value: String, label: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value).font(.title2.weight(.semibold)).monospacedDigit()
            Text(label).font(.callout).foregroundStyle(.secondary)
        }
    }
}
