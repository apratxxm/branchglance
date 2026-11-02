// Renders Resources/Branchglance.icns from assets/brand/mark-full.svg:
// the brand mark on a dark rounded square in the macOS icon grid.
// Run from the repo root: swift macos/make_icon.swift, then
// iconutil -c icns macos/Resources/Branchglance.iconset -o macos/Resources/Branchglance.icns
import AppKit

let mark = NSImage(contentsOfFile: "assets/brand/mark-full.svg")!
let iconset = "macos/Resources/Branchglance.iconset"
try? FileManager.default.createDirectory(atPath: iconset, withIntermediateDirectories: true)

func render(_ pixels: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(pixels) / 1024
    // macOS grid: 824pt body inset 100pt, ~185pt corner radius.
    let body = NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
    NSColor(red: 0x0B / 255, green: 0x0B / 255, blue: 0x0D / 255, alpha: 1).setFill()
    NSBezierPath(roundedRect: body, xRadius: 185 * s, yRadius: 185 * s).fill()
    mark.draw(in: body.insetBy(dx: 170 * s, dy: 170 * s))
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for size in [16, 32, 128, 256, 512] {
    try! render(size).write(to: URL(fileURLWithPath: "\(iconset)/icon_\(size)x\(size).png"))
    try! render(size * 2).write(to: URL(fileURLWithPath: "\(iconset)/icon_\(size)x\(size)@2x.png"))
}
