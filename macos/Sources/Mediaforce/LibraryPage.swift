import SwiftUI

struct LibraryPage: View {
    let facts: MainWindowFacts
    let posters: PosterStore
    // Opens what a name, poster or slice on the page names.
    let open: (Place) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                WorkStatus(facts: facts)
                    .padding(.bottom, 22)
                StorageBar(facts: facts, open: open)
                    .padding(.bottom, 26)
                HStack(alignment: .top, spacing: 28) {
                    VStack(alignment: .leading, spacing: 20) {
                        FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters, open: open)
                        if !facts.samples.waitingOnYou.isEmpty || !facts.samples.approvedNotStarted.isEmpty {
                            SampleShelf(facts: facts, posters: posters) { open(.sample($0)) }
                        }
                        Latest(events: Array(facts.events.prefix(4)), open: open)
                    }
                    .frame(width: 556, alignment: .leading)
                    VStack(alignment: .leading, spacing: 26) {
                        BiggestTable(shows: Array(facts.biggestNotStarted.prefix(BiggestTable.rowLimit)), samples: facts.samples.waitingOnYou, open: open)
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
        .navigationTitle("Library")
    }
}
