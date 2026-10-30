struct PageNavigation {
    private(set) var place: Place? = .library
    private(set) var libraryKind = MediaKind.everything
    private var history: [Place] = []
    private var titles: [Place: String] = [:]

    var back: Place? { history.last }
    var backTitle: String? { back.map { titles[$0] ?? Words.backTitle($0) } }

    mutating func name(_ place: Place, _ title: String) {
        titles[place] = title
    }

    mutating func select(_ place: Place?) {
        self.place = place
        followLibrary(place)
        history.removeAll()
    }

    mutating func open(_ place: Place) {
        guard place != self.place else { return }
        if let current = self.place { history.append(current) }
        self.place = place
        followLibrary(place)
    }

    mutating func goBack() {
        guard let previous = history.popLast() else { return }
        place = previous
        followLibrary(previous)
    }

    private mutating func followLibrary(_ place: Place?) {
        switch place {
        case .library: libraryKind = .everything
        case .libraryKind(let kind), .mediaKind(let kind, _): libraryKind = kind
        default: break
        }
    }
}
