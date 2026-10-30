import SwiftUI

@MainActor
@Observable
final class PosterStore {
    private let client: MediaforceClient
    private(set) var images: [String: NSImage] = [:]
    private var requested: Set<String> = []

    init(client: MediaforceClient) {
        self.client = client
    }

    /// A show's or movie's poster by its title prefix ("tv/Show" or "movies/Title (Year)"), fetched once.
    func image(for titlePrefix: String) -> NSImage? {
        if !requested.contains(titlePrefix) {
            requested.insert(titlePrefix)
            Task { images[titlePrefix] = await client.poster(titlePrefix: titlePrefix) }
        }
        return images[titlePrefix]
    }
}

struct PosterView: View {
    static let nameSizes: [CGFloat] = [12, 10.5, 9]

    static func nameSize(_ name: String, fitting width: CGFloat) -> CGFloat {
        let longestWord = name.split(separator: " ").map(String.init).max { $0.count < $1.count } ?? name
        return nameSizes.first { size in
            let font = NSFont.systemFont(ofSize: size, weight: .semibold)
            return (longestWord as NSString).size(withAttributes: [.font: font]).width <= width
        } ?? nameSizes[nameSizes.count - 1]
    }

    let show: String
    let image: NSImage?
    let headline: String
    let detail: String
    var width: CGFloat = 84

    var body: some View {
        let height = (width / 210 * 295).rounded()
        ZStack(alignment: .bottom) {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: width, height: height)
                    .clipped()
            } else {
                Text(show)
                    .font(.system(size: Self.nameSize(show, fitting: width - 12), weight: .semibold))
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                    .truncationMode(.tail)
                    .frame(width: width - 12)
                    .padding(.bottom, 30)
                    .padding(8)
                    .frame(width: width, height: height)
                    .background(Color.primary.opacity(0.06))
            }
            VStack(alignment: .leading, spacing: 0) {
                Text(headline)
                    .font(.system(size: 12.5, weight: .semibold))
                    .monospacedDigit()
                    .minimumScaleFactor(0.75)
                Text(detail)
                    .font(.system(size: 10))
                    .opacity(0.82)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .lineLimit(1)
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 6)
            .padding(.vertical, 5)
            .background(.ultraThinMaterial)
            .environment(\.colorScheme, .dark)
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.14), lineWidth: 0.5)
        )
    }
}

struct Computers: View {
    let computers: [MainWindowFacts.Computer]
    // Opens that computer in Settings › Computers.
    let open: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Computers")
                .font(.headline)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 7) {
                ForEach(computers) { computer in
                    GridRow {
                        Image(systemName: Words.computerSymbol(computer))
                            .foregroundStyle(.secondary)
                            .frame(width: 22)
                        Button { open(computer.key) } label: {
                            VStack(alignment: .leading, spacing: 1) {
                                Text(computer.name ?? computer.key)
                                    .foregroundStyle(.primary)
                                HStack(spacing: 4) {
                                    if computer.needsYou { WaitingMark().accessibilityHidden(true) }
                                    Text(Words.computerState(computer.state))
                                }
                                .font(.caption)
                            }
                        }
                        .buttonStyle(QuietLinkStyle())
                        .help(Words.openInSettings)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .padding(.leading, 6)
        }
    }
}
