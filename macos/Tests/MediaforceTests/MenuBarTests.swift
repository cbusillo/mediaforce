import Foundation
import XCTest
@testable import Mediaforce

final class MenuBarTests: XCTestCase {
    /// The shared facts with their work replaced, and with nothing waiting for the owner unless asked.
    private func facts(work: String, ownerWaiting: Bool = false) throws -> MainWindowFacts {
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        json["work"] = try JSONSerialization.jsonObject(with: Data(work.utf8))
        if !ownerWaiting {
            json["samples"] = ["waiting_on_you": [], "approved_not_started": []]
            json["computers"] = []
            json["original_backups"] = ["count": 0, "bytes": 0]
        }
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: json))
    }

    private func work(paused: Bool = false, reasons: [String] = [], running: Int = 0, waiting: Int = 0, seasons: [String] = []) -> String {
        let named = seasons.map { prefix in
            let parts = prefix.split(separator: "/").map(String.init)
            let season = parts.count > 2 ? parts[2].split(separator: " ").last.map(String.init) ?? "null" : "null"
            return """
            {"prefix": "\(prefix)", "show_prefix": "\(parts.prefix(2).joined(separator: "/"))", "show": "\(parts[1])",
             "season_folder": \(parts.count > 2 ? "\"\(parts[2])\"" : "null"), "season": \(season)}
            """
        }
        return """
        {"state": "idle", "paused_by_owner": \(paused), "stop_reasons": [\(reasons.map { "\"\($0)\"" }.joined(separator: ","))],
         "running_count": \(running), "running_seasons": [\(named.joined(separator: ","))], "waiting_count": \(waiting), "storage": []}
        """
    }

    func testTheStatusLineSaysWhatMediaforceIsDoing() throws {
        XCTAssertEqual(Words.menuStatus(try facts(work: work(running: 1, seasons: ["tv/Futurama/Season 3"]))), "Compressing Futurama, Season 3")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(running: 3, seasons: ["movies/Heat (1995)", "tv/ER/Season 1"]))),
                       "Compressing Heat (1995) and 1 more")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(), ownerWaiting: true)), "Waiting for you")
        XCTAssertEqual(Words.menuStatus(try facts(work: work())), "Up to date")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(paused: true, running: 1, seasons: ["tv/ER/Season 1"]))), "Paused")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(reasons: ["no_computer_ready"], waiting: 2))), "Waiting for a computer")
    }

    func testTheStatusLineOpensWhatItNames() throws {
        XCTAssertEqual(try facts(work: work(running: 1, seasons: ["tv/Futurama/Season 3"])).statusOpens, .item("tv/Futurama/Season 3"))
        XCTAssertEqual(try facts(work: work(reasons: ["no_computer_ready"], waiting: 2)).statusOpens, .computers)
        XCTAssertNil(try facts(work: work(paused: true, running: 1, seasons: ["tv/ER/Season 1"])).statusOpens)
        XCTAssertNil(try facts(work: work()).statusOpens)
    }

    func testAComputerNeedingTheOwnerIsWaitingForYou() throws {
        var facts = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        facts["work"] = try JSONSerialization.jsonObject(with: Data(work().utf8))
        facts["samples"] = ["waiting_on_you": [], "approved_not_started": []]
        facts["original_backups"] = ["count": 0, "bytes": 0]
        let decoded = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: facts))
        XCTAssertEqual(Words.menuStatus(decoded), "Waiting for you")
    }

    func testFactsFromAServerWithoutRunningSeasonsStillDecode() throws {
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(MainWindowTests.facts.utf8))
        XCTAssertEqual(facts.work.runningSeasons.count, 0)
    }

    func testTheMenuListsWhatWaitsAndOpensItsPage() throws {
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(MainWindowTests.facts.utf8))
        XCTAssertEqual(Attention.menuLines(facts).map(\.opens), [.sample("tv/Lucifer/Season 2"), .computers, .spaceSaved])
    }
}
