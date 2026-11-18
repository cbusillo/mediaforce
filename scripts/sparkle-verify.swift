// Exits 0 when SIGNATURE is a valid Sparkle EdDSA signature of FILE by PUBLIC_KEY (both base64), as Sparkle checks it.
// Usage: swift scripts/sparkle-verify.swift PUBLIC_KEY SIGNATURE FILE
import CryptoKit
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 4,
      let key = Data(base64Encoded: arguments[1]),
      let signature = Data(base64Encoded: arguments[2]),
      let file = FileManager.default.contents(atPath: arguments[3]),
      let publicKey = try? Curve25519.Signing.PublicKey(rawRepresentation: key),
      publicKey.isValidSignature(signature, for: file) else {
    FileHandle.standardError.write(Data("The update's Sparkle signature does not verify\n".utf8))
    exit(1)
}
