// Prints the Sparkle public EdDSA key for the private key on standard input, so a release can refuse a
// private key that does not match the public key its app ships. Accepts Sparkle's exported key: a base64
// 32-byte seed, or the older 96-byte private-and-public form.
import CryptoKit
import Foundation

let text = String(decoding: FileHandle.standardInput.readDataToEndOfFile(), as: UTF8.self)
guard let key = Data(base64Encoded: text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
    FileHandle.standardError.write(Data("The Sparkle private key is not base64\n".utf8))
    exit(1)
}
switch key.count {
case 32:
    print(try Curve25519.Signing.PrivateKey(rawRepresentation: key).publicKey.rawRepresentation.base64EncodedString())
case 96:
    print(key.suffix(32).base64EncodedString())
default:
    FileHandle.standardError.write(Data("The Sparkle private key has \(key.count) bytes, not 32 or 96\n".utf8))
    exit(1)
}
