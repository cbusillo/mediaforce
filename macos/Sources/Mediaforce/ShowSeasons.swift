import SwiftUI

/// A whole show's seasons on its page: each with its state and its next step, the sample page or the season's page.
struct ShowSeasons: View {
    let show: TVShowsFacts.Show
    let openSample: (String) -> Void
    let openItem: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(Words.seasonsHeading)
                .font(.headline)
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 18, verticalSpacing: 8) {
                ForEach(show.seasons) { season in
                    GridRow {
                        Text(Words.seasonName(season))
                        Text(Words.count(season.episodeCount, "episode"))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                        Text(Words.size(season.bytes))
                            .monospacedDigit()
                            .gridColumnAlignment(.trailing)
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            if LibraryStateGroup([season]) == .needsYou { WaitingMark() }
                            Text(Words.tvSeasonState(season))
                        }
                        .help(Words.sampleFailure(season.failure) ?? "")
                        Button(Words.seasonButton(season)) {
                            if season.opensSample {
                                openSample(season.prefix)
                            } else {
                                openItem(season.prefix)
                            }
                        }
                        .controlSize(.small)
                    }
                }
            }
            if let looseFiles = show.looseFiles {
                Text(Words.looseFiles(looseFiles))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
