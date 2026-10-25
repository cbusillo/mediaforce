import SwiftUI

struct MoviesPage: View {
    let model: MainWindowModel
    @State private var search = ""
    @State private var stateGroup: LibraryStateGroup?
    @State private var selected: MoviesFacts.Movie.ID?
    @State private var sortOrder = [KeyPathComparator(\MoviesFacts.Movie.bytes, order: .reverse)]
    @Environment(\.openURL) private var openURL

    var body: some View {
        RefreshingPage(title: Words.movies, load: { try await model.client.movies() }) { (facts: MoviesFacts) in
            let movies = visibleMovies(facts)
            HStack(spacing: 12) {
                TextField(Words.searchMovies, text: $search)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 260)
                LibraryStatePicker(selection: $stateGroup)
                Spacer()
                Text(Words.shown(movies.count, of: facts.movies.count, "movie"))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            MoviesTable(movies: movies, selected: $selected, sortOrder: $sortOrder)
            if movies.isEmpty {
                Text(Words.noMatchingMovies)
                    .foregroundStyle(.secondary)
            } else if let movie = movies.first(where: { $0.id == selected }) {
                MovieDetail(movie: movie) { openURL(model.client.seasonPage(movie.prefix)) }
            } else {
                Text(Words.chooseMovie)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func visibleMovies(_ facts: MoviesFacts) -> [MoviesFacts.Movie] {
        facts.movies
            .filter { $0.matches(search) && (stateGroup == nil || $0.stateGroup == stateGroup) }
            .sorted(using: sortOrder)
    }
}

struct MoviesTable: View {
    let movies: [MoviesFacts.Movie]
    @Binding var selected: MoviesFacts.Movie.ID?
    @Binding var sortOrder: [KeyPathComparator<MoviesFacts.Movie>]

    var body: some View {
        Table(movies, selection: $selected, sortOrder: $sortOrder) {
            TableColumn(Words.titleColumn, value: \MoviesFacts.Movie.title) { (movie: MoviesFacts.Movie) in
                Text(movie.title)
            }
            TableColumn(Words.filesColumn, value: \MoviesFacts.Movie.fileCount) { (movie: MoviesFacts.Movie) in
                Text("\(movie.fileCount)")
                    .monospacedDigit()
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(50)
            TableColumn(Words.sizeColumn, value: \MoviesFacts.Movie.bytes) { (movie: MoviesFacts.Movie) in
                Text(Words.size(movie.bytes))
                    .monospacedDigit()
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(80)
            TableColumn(Words.estimatedSavedColumn, value: \MoviesFacts.Movie.estimatedSavedSortBytes) { (movie: MoviesFacts.Movie) in
                Text(movie.estimatedSavedBytes.map(Words.size) ?? Words.notKnownYet)
                    .monospacedDigit()
                    .foregroundStyle(movie.estimatedSavedBytes == nil ? HierarchicalShapeStyle.secondary : HierarchicalShapeStyle.primary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(150)
            TableColumn(Words.stateColumn, value: \MoviesFacts.Movie.stateRank) { (movie: MoviesFacts.Movie) in
                Text(Words.movieState(movie.state))
            }
            .width(140)
        }
        .tableStyle(.inset(alternatesRowBackgrounds: true))
        .frame(height: TVShowsTable.height)
    }
}

struct MovieDetail: View {
    let movie: MoviesFacts.Movie
    let openInBrowser: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(movie.title)
                .font(.headline)
            Text(Words.movieDetail(movie))
                .foregroundStyle(.secondary)
            Button(Words.openInBrowser, action: openInBrowser)
                .controlSize(.small)
        }
    }
}
