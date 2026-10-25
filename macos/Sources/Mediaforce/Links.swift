import SwiftUI

/// A name, count or line that opens what it names. It keeps the look of the text around it; the pointer becomes a
/// hand and the text brightens under it, so it reads as clickable without looking like a web link.
struct QuietLinkStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        QuietLink(configuration: configuration)
    }

    private struct QuietLink: View {
        let configuration: Configuration
        @State private var hovering = false

        var body: some View {
            configuration.label
                .foregroundStyle(hovering ? AnyShapeStyle(.primary) : AnyShapeStyle(.secondary))
                .opacity(configuration.isPressed ? 0.6 : 1)
                .contentShape(Rectangle())
                .pointerStyle(.link)
                .onHover { hovering = $0 }
        }
    }
}
