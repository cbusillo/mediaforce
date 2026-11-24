import Charts
import SwiftUI

struct SavingsCurve: View {
    struct Point: Identifiable {
        let date: Date
        let week: SavingsHistory.Week?
        let total: Int64
        var id: Date { date }
    }

    let points: [Point]
    @State private var hovered: Date?

    init(history: SavingsHistory, observed: Date) {
        var series: [Point] = []
        var total: Int64 = 0
        for week in history.weeks {
            guard let noon = week.date else { continue }
            let start = noon.addingTimeInterval(-12 * 3600)
            if series.last?.date != start {
                series.append(Point(date: start, week: nil, total: total))
            }
            total += week.bytes
            let end = min(observed, start.addingTimeInterval(7 * 86400))
            if end == series.last?.date { series.removeLast() }
            series.append(Point(date: end, week: week, total: total))
        }
        points = series
    }

    var body: some View {
        let point = hovered.flatMap { date in points.min { abs($0.date.timeIntervalSince(date)) < abs($1.date.timeIntervalSince(date)) } }
        Chart {
            ForEach(points) { sample in
                AreaMark(x: .value("Week", sample.date), y: .value("Saved", sample.total))
                    .interpolationMethod(.monotone)
                    .foregroundStyle(LinearGradient(colors: [.green.opacity(0.26), .green.opacity(0.01)], startPoint: .top, endPoint: .bottom))
                LineMark(x: .value("Week", sample.date), y: .value("Saved", sample.total))
                    .interpolationMethod(.monotone).foregroundStyle(.green).lineStyle(StrokeStyle(lineWidth: 2.5))
            }
            if let end = points.last {
                PointMark(x: .value("Week", end.date), y: .value("Saved", end.total)).foregroundStyle(.green)
                    .annotation(position: .topLeading) { Text("Today").font(.caption2).foregroundStyle(.secondary) }
            }
            if let point {
                RuleMark(x: .value("Week", point.date)).foregroundStyle(.secondary.opacity(0.3))
                PointMark(x: .value("Week", point.date), y: .value("Saved", point.total)).foregroundStyle(.green)
            }
        }
        .chartXAxis(.hidden).chartYAxis(.hidden)
        .chartOverlay { proxy in
            GeometryReader { geometry in
                if let plot = proxy.plotFrame {
                    let bounds = geometry[plot]
                    Rectangle().fill(.clear).contentShape(Rectangle())
                        .onContinuousHover { phase in
                            if case .active(let location) = phase {
                                hovered = proxy.value(atX: location.x - bounds.minX, as: Date.self)
                            } else { hovered = nil }
                        }
                    if let point, let x = proxy.position(forX: point.date) {
                        SavingsTip(title: point.week.map { "Week of " + $0.label } ?? point.date.formatted(.dateTime.month().day()),
                                   detail: point.week.map { "\(Words.size(point.total)) saved in total · \(Words.size($0.bytes)) that week" }
                                       ?? (point.total == 0 ? "Mediaforce started" : "\(Words.size(point.total)) saved in total"))
                            .fixedSize().position(x: min(bounds.width - 130, max(130, x)), y: bounds.midY)
                    }
                }
            }
        }
        .overlay(alignment: .bottomLeading) {
            if let start = points.first {
                Text(start.date.formatted(.dateTime.month(.wide))).font(.caption2).foregroundStyle(.tertiary)
                    .allowsHitTesting(false)
            }
        }
        .overlay(alignment: .bottom) {
            if points.count > 9, let date = points.suffix(8).first?.date {
                Text(date.formatted(.dateTime.month(.wide))).font(.caption2).foregroundStyle(.tertiary).allowsHitTesting(false)
            }
        }
        .frame(height: 120)
    }
}
