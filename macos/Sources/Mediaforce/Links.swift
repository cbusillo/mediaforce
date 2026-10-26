import SwiftUI

/// A name, count or line that opens what it names. It keeps the look of the text around it; the pointer becomes a
/// hand and the text brightens under it, so it reads as clickable without looking like a web link.
struct QuietLinkStyle: ButtonStyle {
    // A name in a list stays primary and takes the accent under the pointer; secondary text brightens to primary.
    var primary = false

    func makeBody(configuration: Configuration) -> some View {
        QuietLink(configuration: configuration, primary: primary)
    }

    private struct QuietLink: View {
        let configuration: Configuration
        let primary: Bool
        @State private var hovering = false

        private var style: AnyShapeStyle {
            switch (primary, hovering) {
            case (true, true): AnyShapeStyle(Color.accentColor)
            case (true, false), (false, true): AnyShapeStyle(.primary)
            case (false, false): AnyShapeStyle(.secondary)
            }
        }

        var body: some View {
            configuration.label
                .foregroundStyle(style)
                .opacity(configuration.isPressed ? 0.6 : 1)
                .contentShape(Rectangle())
                .pointerStyle(.link)
                .onHover { hovering = $0 }
        }
    }
}
