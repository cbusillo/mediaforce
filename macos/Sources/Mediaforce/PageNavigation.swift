struct PageNavigation {
    private(set) var place: Place? = .library
    private var history: [Place] = []

    var back: Place? { history.last }

    mutating func select(_ place: Place?) {
        self.place = place
        history.removeAll()
    }

    mutating func open(_ place: Place) {
        guard place != self.place else { return }
        if let current = self.place { history.append(current) }
        self.place = place
    }

    mutating func goBack() {
        guard let previous = history.popLast() else { return }
        place = previous
    }
}
