import SwiftUI

struct ActivityPage: View {
    let facts: MainWindowFacts
    let model: MainWindowModel
    private var posters: PosterStore { model.posters }
    let open: (Place) -> Void
    private let nameWidth: CGFloat = 235

    var body: some View {
        RefreshingPage(title: "Activity", subtitle: "The next 24 hours", load: { try await model.client.activity() }) { activity in
            WorkStatus(facts: facts, open: open)
            clock(activity)
            ForEach(facts.computers) { computer in
                computerRow(computer, activity)
                Divider()
            }
            lineUp(activity)
            if let next = activity.nextSeason {
                Button("Next: \(Words.season(next.show, next.season))") { open(.item(next.prefix)) }
                    .buttonStyle(QuietLinkStyle(primary: true))
                    .font(.headline)
            }
            Text("Held back for you").font(.headline)
            let waiting = Attention.waitingItems(facts)
            if waiting.isEmpty {
                Text("Nothing needs you right now.").foregroundStyle(.secondary)
            }
            WaitingItems(items: waiting, posters: posters, open: open)
            let holds = activity.earlierHolds
            if !holds.isEmpty {
                DisclosureGroup("Work still marked as held (\(holds.count))") {
                    Text("Needs you above shows the current decisions. These records also include earlier attempts.")
                        .foregroundStyle(.secondary)
                    ForEach(holds) { file in
                        DecisionLine {
                            Text("\(file.title): \(file.waiting ?? "Its earlier attempt is still marked as held.")")
                        } buttons: {
                            Button("Open") { open(.item(file.prefix)) }
                        }
                    }
                }
            }
            FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters, open: open)
        }
    }

    private func clock(_ activity: ActivityFacts) -> some View {
        HStack(alignment: .bottom, spacing: 18) {
            Text("Computers").font(.headline).frame(width: nameWidth, alignment: .leading)
            HStack {
                Text("Now")
                Spacer()
                if let start = ActivityFacts.date(activity.startsAt) {
                    ForEach(1..<6) { index in
                        Text(start.addingTimeInterval(Double(index) * 4 * 3600),
                             format: .dateTime.hour(.defaultDigits(amPM: .abbreviated)).timeZone(.specificName(.short)))
                        Spacer()
                    }
                }
                Text("24 h")
            }
            .font(.caption).foregroundStyle(.secondary).monospacedDigit()
        }
    }

    private func computerRow(_ computer: MainWindowFacts.Computer, _ activity: ActivityFacts) -> some View {
        let details = activity.computers.first { $0.key == computer.key }
        let running = details?.running ?? []
        let files = running + activity.linedUp.filter { $0.computerKey == computer.key }
        return HStack(alignment: .top, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Button { open(.computer(computer.key)) } label: {
                    Label(computer.name ?? computer.key, systemImage: Words.computerSymbol(computer))
                        .font(.headline)
                }
                .buttonStyle(QuietLinkStyle(primary: true))
                Text(Words.computerState(computer.state)).foregroundStyle(.secondary)
                if let first = details?.windows.first {
                    Text(windowWords(first, activity: activity)).font(.caption).foregroundStyle(.secondary)
                }
                if details?.finishCurrentFile == true {
                    Text("Finishes its file when the window closes").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(running) { file in
                    Text(file.title).font(.callout)
                    Text(ActivityFacts.time(file.finishesAt).map { "Finishes about \($0)" } ?? "Finish time is not known yet")
                        .font(.title3.weight(.semibold)).monospacedDigit()
                    if let progress = file.progress {
                        ProgressView(value: progress).accessibilityLabel(file.title)
                    } else {
                        ProgressView().progressViewStyle(.linear).controlSize(.small).accessibilityLabel("Measuring progress")
                    }
                }
            }
            .frame(width: nameWidth, alignment: .leading)
            ActivityTimeline(activity: activity, windows: details?.windows ?? [], files: files,
                             runningIDs: Set(running.map(\.id)), posters: posters, open: open)
        }
        .padding(.vertical, 5)
    }

    private func windowWords(_ window: ActivityFacts.Window, activity: ActivityFacts) -> String {
        let open = activity.fraction(window.startsAt) == 0
        let close = activity.fraction(window.endsAt) == 1
        if open && close { return "Work window open for the next 24 hours" }
        let start = ActivityFacts.time(window.startsAt) ?? ""
        let end = ActivityFacts.time(window.endsAt) ?? ""
        if open { return "Open until \(end)" }
        if close { return "Opens \(start)" }
        return "\(start) to \(end)"
    }

    private func lineUp(_ activity: ActivityFacts) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Lined up").font(.headline)
                Spacer()
                Text("Times and computers are estimates").font(.caption).foregroundStyle(.secondary)
            }
            if activity.linedUp.isEmpty {
                Text("No files are lined up right now.").foregroundStyle(.secondary)
            }
            ForEach(Array(activity.linedUp.enumerated()), id: \.element.id) { index, file in
                HStack(alignment: .center, spacing: 12) {
                    Text("\(index + 1)").foregroundStyle(.tertiary).monospacedDigit().frame(width: 28, alignment: .trailing)
                    Button { open(.item(file.prefix)) } label: {
                        HStack(spacing: 10) {
                            MediaPoster(titlePrefix: file.showPrefix, name: file.show, kind: file.mediaKind, posters: posters, width: 28)
                            Text(file.title).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .buttonStyle(QuietLinkStyle(primary: true))
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(ActivityFacts.time(file.startsAt).map { "Starts about \($0)" } ?? file.waiting ?? "Start time is not known yet")
                        if let computer = file.computerName { Text(computer).foregroundStyle(.secondary) }
                    }
                    .font(.callout).monospacedDigit().frame(width: 260, alignment: .trailing)
                }
                Divider()
            }
        }
    }
}

private struct ActivityTimeline: View {
    let activity: ActivityFacts
    let windows: [ActivityFacts.Window]
    let files: [ActivityFacts.File]
    let runningIDs: Set<String>
    let posters: PosterStore
    let open: (Place) -> Void

    private var lanes: [[ActivityFacts.File]] {
        var rows: [[ActivityFacts.File]] = [[]]
        for file in files.sorted(by: { ($0.startsAt ?? "") < ($1.startsAt ?? "") }) {
            let index = rows.firstIndex { row in
                guard let last = row.last else { return true }
                guard let end = last.finishesAt else { return false }
                return end <= (file.startsAt ?? "")
            } ?? rows.count
            if index == rows.count { rows.append([]) }
            rows[index].append(file)
        }
        return rows
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .topLeading) {
                Rectangle().fill(Color.primary.opacity(0.2)).frame(height: 1).offset(y: 30)
                ForEach(Array(windows.enumerated()), id: \.offset) { _, window in
                    if let start = activity.fraction(window.startsAt), let end = activity.fraction(window.endsAt) {
                        Rectangle().fill(Color.primary.opacity(0.075))
                            .frame(width: max(0, geometry.size.width * (end - start)), height: CGFloat(lanes.count) * 48 + 12)
                            .offset(x: geometry.size.width * start)
                            .help("Work window: \(ActivityFacts.time(window.startsAt) ?? "") to \(ActivityFacts.time(window.endsAt) ?? "")")
                    }
                }
                ForEach(0..<7) { index in
                    Rectangle().fill(index == 0 ? Color.accentColor : Color.primary.opacity(0.08))
                        .frame(width: 1, height: CGFloat(lanes.count) * 48 + 12)
                        .offset(x: geometry.size.width * Double(index) / 6)
                }
                ForEach(Array(lanes.enumerated()), id: \.offset) { lane, files in
                    ForEach(files) { file in
                        if let timestamp = file.startsAt, let start = activity.fraction(timestamp), start < 1 {
                            let end = file.finishesAt.flatMap(activity.fraction)
                            let width = end.map { max(0, geometry.size.width * ($0 - start) - 2) } ?? 44
                            Button { open(.item(file.prefix)) } label: {
                                HStack(spacing: 6) {
                                    MediaPoster(titlePrefix: file.showPrefix, name: file.show, kind: file.mediaKind, posters: posters, width: 24)
                                    Text(end == nil ? "?" : file.title).font(.caption).lineLimit(2)
                                    Spacer(minLength: 0)
                                }
                                .padding(4)
                                .frame(width: width, height: 44, alignment: .leading)
                                .background(runningIDs.contains(file.id) ? Color.accentColor.opacity(0.16) : Color(nsColor: .textBackgroundColor))
                                .border(runningIDs.contains(file.id) ? Color.accentColor : Color.primary.opacity(0.18), width: 1)
                                .clipped()
                            }
                            .buttonStyle(.plain)
                            .offset(x: geometry.size.width * start + 1, y: CGFloat(lane) * 48 + 6)
                            .help("\(file.title). \(ActivityFacts.time(file.finishesAt).map { "Finishes about \($0)" } ?? "Finish time is not known yet")")
                            .accessibilityLabel(file.title)
                        }
                    }
                }
            }
        }
        .frame(height: max(100, CGFloat(lanes.count) * 48 + 12))
    }
}
