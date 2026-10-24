import SwiftUI

struct LibraryPage: View {
    let facts: MainWindowFacts
    let posters: PosterStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text("Library")
                    .font(.system(size: 30, weight: .bold))
                    .padding(.bottom, 10)
                WorkStatus(facts: facts)
                    .padding(.bottom, 22)
                StorageBar(facts: facts)
                    .padding(.bottom, 26)
                HStack(alignment: .top, spacing: 28) {
                    VStack(alignment: .leading, spacing: 20) {
                        FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters)
                        if !facts.samples.waitingOnYou.isEmpty || !facts.samples.approvedNotStarted.isEmpty {
                            SampleShelf(facts: facts, posters: posters)
                        }
                        Latest(events: Array(facts.events.prefix(4)))
                    }
                    .frame(width: 556, alignment: .leading)
                    VStack(alignment: .leading, spacing: 26) {
                        BiggestTable(shows: facts.biggestNotStarted, samples: facts.samples.waitingOnYou)
                        Computers(computers: facts.computers)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(.horizontal, 34)
            .padding(.top, 6)
            .padding(.bottom, 24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(nsColor: .textBackgroundColor))
    }
}
