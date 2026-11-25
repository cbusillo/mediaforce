import SwiftUI

struct RingSector {
    let group: String
    let slice: SavingsSlice?
    let start: Double
    let end: Double
    let inner: Double
    let outer: Double
    let color: Color
    let label: String?

    func radius(_ progress: Double) -> Double {
        slice == nil ? outer : 292 + (outer - 292) * progress
    }

    func path(_ progress: Double) -> Path {
        let from = Angle.radians(start - .pi / 2)
        let to = Angle.radians(end - .pi / 2)
        var path = Path()
        path.addArc(center: .zero, radius: radius(progress), startAngle: from, endAngle: to, clockwise: false)
        path.addArc(center: .zero, radius: inner, startAngle: to, endAngle: from, clockwise: true)
        path.closeSubpath()
        return path
    }

    static func sectors(_ groups: [SavingsGroup], zoomed: Bool) -> [Self] {
        let total = Double(groups.reduce(0) { $0 + $1.before })
        guard total > 0 else { return [] }
        var angle = 0.0
        var result: [Self] = []
        for group in groups {
            let span = Double(group.before) / total * 2 * .pi
            let gap = min(0.004, span / 4)
            if !zoomed, let compressed = group.groupedCompressedBytes {
                let start = angle + gap
                let end = angle + span - gap
                let split = start + (end - start) * min(1, max(0, Double(compressed) / Double(group.before)))
                if split > start {
                    result.append(Self(group: group.id, slice: nil, start: start, end: split,
                                       inner: 120, outer: 196, color: .green, label: nil))
                }
                if end > split {
                    result.append(Self(group: group.id, slice: nil, start: split, end: end,
                                       inner: 120, outer: 196, color: .gray.opacity(0.38), label: nil))
                }
            } else if !zoomed {
                let tint: Color = group.saved > 0 ? .green.opacity(group.saved * 2 > group.before ? 1 : 0.7) : .gray.opacity(0.38)
                result.append(Self(group: group.id, slice: nil, start: angle + gap, end: angle + span - gap,
                                   inner: 120, outer: 196, color: tint.opacity(group.isShow ? 1 : 0.75), label: nil))
            }
            for slice in group.slices where slice.before > 0 {
                let width = Double(slice.before) / total * 2 * .pi
                if !zoomed && width < 0.002 {
                    angle += width
                    continue
                }
                let gap = min(zoomed ? 0.006 : 0.002, width / 4)
                let inner = zoomed ? 120.0 : 200.0
                let ratio = min(1, max(0, Double(slice.current) / Double(slice.before)))
                let outer = inner + max(zoomed ? 10 : 8, (292 - inner) * ratio)
                result.append(Self(group: group.id, slice: slice, start: angle + gap, end: angle + width - gap,
                                   inner: inner, outer: outer, color: slice.saved > 0 ? .green : .gray.opacity(zoomed ? 0.38 : 0.22),
                                   label: zoomed ? slice.name.replacingOccurrences(of: "Season ", with: "S") : nil))
                angle += width
            }
        }
        return result
    }
}

private struct RingDrawing: View, Animatable {
    let sectors: [RingSector]
    let highlighted: String?
    nonisolated var progress: Double

    nonisolated var animatableData: Double {
        get { progress }
        set { progress = newValue }
    }

    var body: some View {
        Canvas { context, size in
            context.translateBy(x: size.width / 2, y: size.height / 2)
            context.scaleBy(x: size.width / 620, y: size.height / 620)
            for sector in sectors {
                let alpha = highlighted == nil || highlighted == sector.group ? 1.0 : 0.45
                context.fill(sector.path(progress), with: .color(sector.color.opacity(alpha)))
                if let label = sector.label {
                    let mid = (sector.start + sector.end) / 2
                    context.draw(Text(label).font(.system(size: 12)).foregroundStyle(.secondary),
                                 at: CGPoint(x: 304 * sin(mid), y: -304 * cos(mid)))
                }
            }
        }
    }
}

struct SavingsTip: View {
    let title: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).fontWeight(.semibold)
            Text(detail).foregroundStyle(.secondary)
        }
        .font(.system(size: 12)).padding(.vertical, 7).padding(.horizontal, 10)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
        .overlay { RoundedRectangle(cornerRadius: 8).strokeBorder(.quaternary) }
        .shadow(color: .black.opacity(0.14), radius: 6, y: 2)
        .allowsHitTesting(false)
    }
}

struct SavingsRing: View {
    let library: SavingsLibrary
    let today: Int64
    let posters: PosterStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var zoom: String?
    @State private var pointer: CGPoint?
    @State private var progress = 0.0

    var body: some View {
        let selected = library.shows.first { $0.id == zoom }
        let sectors = RingSector.sectors(selected.map { [$0] } ?? library.groups, zoomed: selected != nil)
        GeometryReader { geometry in
            let side = max(1, min(560, geometry.size.width - 328))
            let hovered = pointer.flatMap { hit($0, side: side, sectors: sectors) }
            let group = selected ?? library.groups.first { $0.id == hovered?.group }
            HStack(spacing: 28) {
                ZStack {
                    RingDrawing(sectors: sectors, highlighted: hovered?.group, progress: progress)
                        .contentShape(Rectangle())
                        .onContinuousHover { phase in
                            if case .active(let location) = phase { pointer = location } else { pointer = nil }
                        }
                        .onTapGesture { location in
                            if selected == nil, let hit = hit(location, side: side, sectors: sectors),
                               library.shows.contains(where: { $0.id == hit.group }) {
                                pointer = nil
                                zoom = hit.group
                            }
                        }
                    center(group, zoomed: selected != nil)
                    if let pointer, let slice = hovered?.slice, let group {
                        let sizes = slice.saved > 0 ? "\(Words.size(slice.before)) → \(Words.size(slice.current))"
                            : "\(Words.size(slice.current)), still original"
                        let percent = selected != nil && slice.saved > 0 ? " · \(Int((100 * Double(slice.saved) / Double(slice.before)).rounded()))% smaller" : ""
                        SavingsTip(title: "\(group.name), \(slice.name)", detail: sizes + percent)
                            .fixedSize().position(x: min(side - 115, max(115, pointer.x + 70)), y: min(side - 26, pointer.y + 33))
                    }
                }
                .frame(width: side, height: side)
                .frame(maxWidth: .infinity)
                legend(group ?? library.shows.max { $0.saved < $1.saved })
                    .frame(width: 300, alignment: .leading)
            }
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        .frame(height: min(560, max(1, width - 328)))
        .task(id: zoom) {
            progress = reduceMotion ? 1 : 0
            guard !reduceMotion else { return }
            try? await Task.sleep(for: .milliseconds(180))
            if !Task.isCancelled { withAnimation(.easeOut(duration: 1.5)) { progress = 1 } }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Ring map of the library by show and season")
        .accessibilityActions {
            if selected == nil {
                ForEach(library.shows) { show in Button(show.name) { zoom = show.id } }
            }
        }
    }

    @State private var width: CGFloat = 840

    private func hit(_ point: CGPoint, side: CGFloat, sectors: [RingSector]) -> RingSector? {
        let dx = 620 * (point.x / side - 0.5)
        let dy = 620 * (point.y / side - 0.5)
        let angle = (atan2(dx, -dy) + 2 * .pi).truncatingRemainder(dividingBy: 2 * .pi)
        let radius = hypot(dx, dy)
        return sectors.first { angle >= $0.start && angle <= $0.end && radius >= $0.inner && radius <= $0.radius(progress) }
    }

    @ViewBuilder private func center(_ group: SavingsGroup?, zoomed: Bool) -> some View {
        VStack(spacing: 4) {
            if let group {
                if group.isShow, let image = posters.image(for: group.id) {
                    Image(nsImage: image).resizable().scaledToFill().frame(width: 62, height: 93)
                        .clipShape(RoundedRectangle(cornerRadius: 5))
                }
                Text(group.name).fontWeight(.semibold)
                Text("\(Words.size(group.before)) → \(Words.size(group.current))").font(.caption).foregroundStyle(.secondary)
                if zoomed {
                    Button("All shows") { pointer = nil; zoom = nil }.controlSize(.small)
                }
            } else {
                Text(Words.size(today)).font(.system(size: 20, weight: .semibold))
                Text("TV and movies today").font(.caption).foregroundStyle(.secondary)
            }
        }
        .multilineTextAlignment(.center)
        .allowsHitTesting(zoomed)
    }

    private func legend(_ group: SavingsGroup?) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Library").fontWeight(.semibold)
                Text("Shows by original size. Outer ring: seasons. Click a show to see its seasons.").foregroundStyle(.secondary)
            }
            key("Compressed. Ring depth shows the new size.", .green)
            key("Original", .gray.opacity(0.38))
            Divider()
            if let group {
                VStack(alignment: .leading, spacing: 3) {
                    Text(group.name).fontWeight(.semibold)
                    Text(group.summary).foregroundStyle(.secondary)
                }
            }
        }.font(.system(size: 13))
    }

    private func key(_ text: String, _ color: Color) -> some View {
        HStack(alignment: .top, spacing: 10) {
            RoundedRectangle(cornerRadius: 4).fill(color).frame(width: 14, height: 14)
            Text(text)
        }
    }
}
