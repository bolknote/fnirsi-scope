import Cocoa
import FlutterMacOS

class MainFlutterWindow: NSWindow {
  override func awakeFromNib() {
    let flutterViewController = FlutterViewController()
    self.contentViewController = flutterViewController
    if let screen = NSScreen.main {
      let visible = screen.visibleFrame
      self.setContentSize(NSSize(width: min(1280, max(760, visible.width - 80)),
                                 height: min(820, max(600, visible.height - 80))))
      self.center()
    }
    self.minSize = NSSize(width: 760, height: 520)
    RegisterGeneratedPlugins(registry: flutterViewController)
    super.awakeFromNib()
    self.title = "Осциллограф — FNIRSI 2C53T"
    // Set the running application's Dock icon as well as its bundle icon.
    if let iconURL = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
       let icon = NSImage(contentsOf: iconURL) {
      NSApplication.shared.applicationIconImage = icon
    }
  }
}
