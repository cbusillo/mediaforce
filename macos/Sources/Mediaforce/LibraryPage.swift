import SwiftUI

struct LibraryPage: View {
    let facts: MainWindowFacts
    let posters: PosterStore
    // Opens what a name, poster or slice on the page names.
    let open: (Place) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                // Only when there is something to explain; the title bar already says what the work is doing.
                if Words.workExplanation(facts) != nil || facts.lastChange != nil {
                    WorkStatus(facts: facts, open: open)
                        .padding(.bottom, 22)
                }
                StorageBar(facts: facts, open: open)
                    .padding(.bottom, 26)
                HStack(alignment: .top, spacing: 28) {
                    VStack(alignment: .leading, spacing: 20) {
                        FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters, open: open)
                        if !facts.samples.approvedNotStarted.isEmpty {
                            SampleShelf(facts: facts, posters: posters) { open(.sample($0)) }
                        }
                        Latest(events: Array(facts.events.prefix(4)), posters: posters, open: open)
                    }
                    .frame(width: 556, alignment: .leading)
                    VStack(alignment: .leading, spacing: 26) {
                        BiggestTable(shows: Array(facts.biggestNotStarted.prefix(BiggestTable.rowLimit)), samples: facts.samples.waitingOnYou,
                                     posters: posters, open: open)
                        Computers(computers: facts.computers) { open(.computer($0)) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(.horizontal, 34)
            .padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(nsColor: .textBackgroundColor))
        .navigationTitle(Words.library)
        .navigationSubtitle(Words.workHeadline(facts))
    }
}
