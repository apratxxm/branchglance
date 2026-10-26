import Cocoa

if let index = CommandLine.arguments.firstIndex(of: "--render-preview"), index + 1 < CommandLine.arguments.count {
    MainActor.assumeIsolated { renderPreviews(to: CommandLine.arguments[index + 1]) }
    exit(0)
}

let app = NSApplication.shared
let host = Host()
app.delegate = host
app.run()
