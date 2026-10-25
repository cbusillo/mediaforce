import Foundation

struct LibrarySettingsFacts: Decodable, Sendable, Equatable {
    let libraries: [Library]
    let workingFolder: String
    let catalogRefreshHours: Double
    let types: [String]

    struct Library: Codable, Sendable, Equatable {
        var key: String
        var name: String
        var folder: String
        var type: String
        var typeChangeConfirmation: String?
    }
}

struct LibrarySettingsSave: Encodable, Sendable, Equatable {
    let libraries: [LibrarySettingsFacts.Library]
    let workingFolder: String
    let catalogRefreshHours: Double
}

struct LibrarySettingsSaveResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
    let settings: LibrarySettingsFacts?
}

struct LibraryTypePreview: Decodable, Sendable, Equatable {
    let key: String
    let fromType: String
    let toType: String
    let itemCount: Int
    let acknowledgement: String
}

struct LibraryTypePreviewResult: Decodable, Sendable {
    let preview: LibraryTypePreview
}

struct LibrarySettingsDraft: Equatable {
    struct Row: Identifiable, Equatable {
        let id = UUID()
        var library: LibrarySettingsFacts.Library
    }

    enum TypeChoice: Equatable {
        case applied
        case needsPreview(key: String)
    }

    var rows: [Row]
    var workingFolder: String
    var catalogRefreshHours: Double

    init(_ facts: LibrarySettingsFacts) {
        rows = facts.libraries.map { Row(library: $0) }
        workingFolder = facts.workingFolder
        catalogRefreshHours = facts.catalogRefreshHours
    }

    var saveBody: LibrarySettingsSave {
        LibrarySettingsSave(
            libraries: rows.map(\.library),
            workingFolder: workingFolder.trimmingCharacters(in: .whitespaces),
            catalogRefreshHours: catalogRefreshHours
        )
    }

    mutating func addLibrary(type: String) {
        rows.append(Row(library: .init(key: "", name: "", folder: "", type: type)))
    }

    mutating func remove(_ id: Row.ID) {
        rows.removeAll { $0.id == id }
    }

    /// A saved library changes type only after the server's preview is confirmed, as on the website.
    mutating func chooseType(_ type: String, for id: Row.ID, saved: LibrarySettingsFacts) -> TypeChoice {
        guard let index = rows.firstIndex(where: { $0.id == id }), rows[index].library.type != type else { return .applied }
        let key = rows[index].library.key
        guard let savedType = saved.libraries.first(where: { $0.key == key && !key.isEmpty })?.type,
              savedType != type else {
            rows[index].library.type = type
            rows[index].library.typeChangeConfirmation = nil
            return .applied
        }
        return .needsPreview(key: key)
    }

    mutating func confirmType(_ preview: LibraryTypePreview, for id: Row.ID) {
        guard let index = rows.firstIndex(where: { $0.id == id }) else { return }
        rows[index].library.type = preview.toType
        rows[index].library.typeChangeConfirmation = preview.acknowledgement
    }
}

extension JSONEncoder {
    static let mediaforce: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = .sortedKeys
        return encoder
    }()
}
