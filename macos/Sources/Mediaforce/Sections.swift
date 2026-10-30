import SwiftUI

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
