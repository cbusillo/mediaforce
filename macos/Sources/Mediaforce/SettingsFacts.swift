import Foundation

struct LibrarySettingsFacts: Decodable, Sendable, Equatable {
    let libraries: [Library]
    let workingFolder: String
    let catalogRefreshHours: Double
    let types: [String]
    let revision: String

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
    let revision: String
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
    let revision: String

    init(_ facts: LibrarySettingsFacts) {
        rows = facts.libraries.map { Row(library: $0) }
        workingFolder = facts.workingFolder
        catalogRefreshHours = facts.catalogRefreshHours
        revision = facts.revision
    }

    var saveBody: LibrarySettingsSave {
        LibrarySettingsSave(
            libraries: rows.map(\.library),
            workingFolder: workingFolder.trimmingCharacters(in: .whitespaces),
            catalogRefreshHours: catalogRefreshHours,
            revision: revision
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

struct ComputerSettingsFacts: Decodable, Sendable, Equatable {
    let computers: [Computer]
    let schedules: [Schedule]
    let builtInSchedules: [String]
    let abilities: [String]
    let mediaAccessOptions: [String]
    let days: [String]
    let libraries: [Library]
    let revision: String

    /// `id` names the stored computer it was loaded from; a new computer's is empty.
    struct Computer: Codable, Sendable, Equatable {
        var id: String
        var name: String
        var address: String
        var abilities: [String]
        var schedule: String
        var libraries: [String]
        var atOnce: Int
        var wakeAddress: String
        var priority: Int
        var mediaAccess: String
        var mediaforceFolder: String
        var workingFolder: String
        var scratchFolder: String
        var libraryFolders: [LibraryFolder]
        var startCommand: String
        var stopCommand: String
        var startWaitSeconds: Int
    }

    struct LibraryFolder: Codable, Sendable, Equatable {
        var library: String
        var folder: String
    }

    /// A new schedule's key is a placeholder the server replaces; computers choose it by that key until then.
    struct Schedule: Codable, Sendable, Equatable {
        var key: String
        var name: String
        var days: [String]
        var allDayDays: [String]
        var startHour: Int
        var endHour: Int
    }

    struct Library: Decodable, Sendable, Equatable {
        let key: String
        let name: String
    }
}

struct ComputerSettingsSave: Encodable, Sendable, Equatable {
    let computers: [ComputerSettingsFacts.Computer]
    let schedules: [ComputerSettingsFacts.Schedule]
    let revision: String
}

struct ComputerSettingsSaveResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
    let settings: ComputerSettingsFacts?
}

struct ComputerSettingsDraft: Equatable {
    struct ComputerRow: Identifiable, Equatable {
        let id = UUID()
        var computer: ComputerSettingsFacts.Computer
    }

    struct ScheduleRow: Identifiable, Equatable {
        let id = UUID()
        var schedule: ComputerSettingsFacts.Schedule
    }

    var computers: [ComputerRow]
    var schedules: [ScheduleRow]
    let revision: String

    init(_ facts: ComputerSettingsFacts) {
        computers = facts.computers.map { ComputerRow(computer: $0) }
        schedules = facts.schedules.map { ScheduleRow(schedule: $0) }
        revision = facts.revision
    }

    var saveBody: ComputerSettingsSave {
        ComputerSettingsSave(
            computers: computers.map(\.computer),
            schedules: schedules.map(\.schedule),
            revision: revision
        )
    }

    mutating func addComputer(abilities: [String], mediaAccess: String) {
        computers.append(ComputerRow(computer: .init(
            id: "", name: "", address: "", abilities: abilities, schedule: "always", libraries: [], atOnce: 1,
            wakeAddress: "", priority: 0, mediaAccess: mediaAccess, mediaforceFolder: "", workingFolder: "",
            scratchFolder: "", libraryFolders: [], startCommand: "", stopCommand: "", startWaitSeconds: 180
        )))
    }

    mutating func addSchedule(days: [String]) {
        schedules.append(ScheduleRow(schedule: .init(
            key: "new-\(UUID().uuidString.lowercased())", name: "", days: days, allDayDays: [], startHour: 22, endHour: 8
        )))
    }

    mutating func removeComputer(_ id: ComputerRow.ID) {
        computers.removeAll { $0.id == id }
    }

    mutating func removeSchedule(_ id: ScheduleRow.ID) {
        schedules.removeAll { $0.id == id }
    }

    func computersUsing(_ schedule: ComputerSettingsFacts.Schedule) -> Int {
        computers.filter { $0.computer.schedule == schedule.key }.count
    }

    /// An empty list means every library, as the website stores it; choosing every library stores it empty again.
    static func allows(_ computer: ComputerSettingsFacts.Computer, library: String) -> Bool {
        computer.libraries.isEmpty || computer.libraries.contains(library)
    }

    static func togglingLibrary(_ library: String, in chosen: [String], all: [String]) -> [String] {
        var explicit = chosen.isEmpty ? all : chosen
        if let index = explicit.firstIndex(of: library) {
            explicit.remove(at: index)
        } else {
            explicit.append(library)
        }
        guard !explicit.isEmpty else { return chosen }
        return Set(all).isSubset(of: explicit) ? [] : explicit
    }

    /// A day runs either in the window or all day, never both.
    static func togglingDay(
        _ day: String, allDay: Bool, in schedule: ComputerSettingsFacts.Schedule, order: [String]
    ) -> ComputerSettingsFacts.Schedule {
        var schedule = schedule
        let chosen = allDay ? schedule.allDayDays : schedule.days
        if chosen.contains(day) {
            if allDay { schedule.allDayDays.removeAll { $0 == day } } else { schedule.days.removeAll { $0 == day } }
            return schedule
        }
        let ordered: ([String]) -> [String] = { days in order.filter { days.contains($0) || $0 == day } }
        if allDay {
            schedule.allDayDays = ordered(schedule.allDayDays)
            schedule.days.removeAll { $0 == day }
        } else {
            schedule.days = ordered(schedule.days)
            schedule.allDayDays.removeAll { $0 == day }
        }
        return schedule
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
