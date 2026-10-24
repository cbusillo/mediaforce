import Foundation

enum Words {
    static func size(_ bytes: Int64) -> String {
        let value = Double(bytes)
        if value >= 1e12 { return (value / 1e12).formatted(.number.precision(.fractionLength(2))) + " TB" }
        if value >= 1e9 { return (value / 1e9).formatted(.number.precision(.fractionLength(0))) + " GB" }
        if value >= 1e6 { return (value / 1e6).formatted(.number.precision(.fractionLength(0))) + " MB" }
        return (value / 1e3).formatted(.number.precision(.fractionLength(0))) + " KB"
    }

    static func count(_ count: Int, _ noun: String) -> String {
        "\(count.formatted()) \(noun)\(count == 1 ? "" : "s")"
    }

    static func season(_ show: String, _ season: Int?) -> String {
        guard let season else { return show }
        return "\(show), Season \(season)"
    }

    static func computerState(_ code: String) -> String {
        switch code {
        case "ready": "Ready"
        case "busy": "Compressing"
        case "checking": "Checking this computer"
        case "outside_schedule": "Off schedule"
        case "cooling_down": "Resting after repeated failures"
        case "not_encoding": "Makes samples only"
        case "storage_unreachable": "Can't reach shared storage"
        case "password_needed": "Needs its password once"
        case "unreachable": "Can't reach it. Is it on and awake?"
        case "remote_login_off": "Needs Remote Login turned on"
        case "identity_changed": "Its identity changed"
        case "new_name": "Needs to be confirmed"
        case "not_found": "Can't find it on the network"
        case "no_address": "Has no address"
        case "python_missing": "Needs Python 3"
        case "unreadable_reply": "Its reply couldn't be read"
        default: "Not ready"
        }
    }
}
