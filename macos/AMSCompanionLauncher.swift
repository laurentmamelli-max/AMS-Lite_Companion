import AppKit
import CoreGraphics
import Foundation
import WebKit

private let dashboardURL = URL(string: "http://127.0.0.1:8765/")!
private let embeddedDashboardURL = URL(string: "http://127.0.0.1:8765/?embedded=1")!
private let stateURL = URL(string: "http://127.0.0.1:8765/api/state")!
private let shutdownURL = URL(string: "http://127.0.0.1:8765/api/shutdown")!

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    private var statusItem: NSStatusItem!
    private var statusLine: NSMenuItem!
    private var panelMenuItem: NSMenuItem!
    private var dockMenuItem: NSMenuItem!
    private var spoolLines: [NSMenuItem] = []
    private var panel: NSPanel!
    private var webView: WKWebView!
    private var engine: Process?
    private var pollTimer: Timer?
    private var bambuSeen = false
    private var bambuMissingPolls = 0
    private var quitting = false
    private var panelDocked = true

    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: ["panelDocked": true])
        panelDocked = UserDefaults.standard.bool(forKey: "panelDocked")
        buildMenu()
        buildPanel()
        startEngine(showPanel: true)
        launchBambuStudio()
        pollTimer = Timer.scheduledTimer(timeInterval: 3.0,
                                         target: self,
                                         selector: #selector(poll),
                                         userInfo: nil,
                                         repeats: true)
        poll()
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        if let process = engine, process.isRunning {
            process.terminate()
        }
    }

    private func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if #available(macOS 11.0, *) {
                button.image = NSImage(systemSymbolName: "circle.grid.2x2.fill",
                                       accessibilityDescription: "AMS Lite Companion")
            } else {
                button.title = "AMS"
            }
            button.toolTip = "AMS Lite Companion"
        }

        let menu = NSMenu()
        let title = NSMenuItem(title: "AMS Lite Companion v1.3", action: nil, keyEquivalent: "")
        title.isEnabled = false
        menu.addItem(title)

        statusLine = NSMenuItem(title: "Démarrage…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())

        for slot in 1...4 {
            let line = NSMenuItem(title: "A\(slot) · Chargement…", action: nil, keyEquivalent: "")
            line.isEnabled = false
            spoolLines.append(line)
            menu.addItem(line)
        }

        menu.addItem(.separator())
        panelMenuItem = NSMenuItem(title: "Afficher le panneau Companion",
                                   action: #selector(togglePanel),
                                   keyEquivalent: "p")
        menu.addItem(panelMenuItem)
        dockMenuItem = NSMenuItem(title: "Suivre la fenêtre Bambu Studio",
                                  action: #selector(toggleDocking),
                                  keyEquivalent: "d")
        dockMenuItem.state = panelDocked ? .on : .off
        menu.addItem(dockMenuItem)
        menu.addItem(NSMenuItem(title: "Ouvrir le tableau complet dans le navigateur",
                                action: #selector(openBrowserDashboard),
                                keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Ouvrir Bambu Studio",
                                action: #selector(openBambu),
                                keyEquivalent: "b"))
        menu.addItem(NSMenuItem(title: "Redémarrer le moteur",
                                action: #selector(restartEngine),
                                keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "Afficher le journal",
                                action: #selector(openLog),
                                keyEquivalent: "l"))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quitter Companion",
                                action: #selector(quitCompanion),
                                keyEquivalent: "q"))
        menu.items.forEach { $0.target = self }
        statusItem.menu = menu
    }

    private func buildPanel() {
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let width = min(440.0, max(390.0, visible.width * 0.3))
        let height = min(760.0, visible.height - 30.0)
        let rect = NSRect(x: visible.maxX - width,
                          y: visible.maxY - height,
                          width: width,
                          height: height)
        panel = NSPanel(contentRect: rect,
                        styleMask: [.titled, .closable, .resizable, .utilityWindow],
                        backing: .buffered,
                        defer: false)
        panel.title = "AMS Lite Companion"
        panel.minSize = NSSize(width: 370, height: 480)
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = false
        panel.collectionBehavior = [.fullScreenAuxiliary]
        panel.delegate = self

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: panel.contentView?.bounds ?? .zero, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        panel.contentView = webView
    }

    private func pythonExecutable() -> String? {
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/usr/bin/python3"
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func bundledScript() -> String? {
        Bundle.main.path(forResource: "ams_companion", ofType: "py")
    }

    private func engineIsReachable(completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: stateURL)
        request.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let ok = data != nil && (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(ok) }
        }.resume()
    }

    private func startEngine(showPanel: Bool) {
        engineIsReachable { [weak self] alreadyRunning in
            guard let self = self else { return }
            if alreadyRunning {
                self.statusLine.title = "Moteur connecté"
                if showPanel { self.showPanelWhenReady(attempt: 0) }
                return
            }
            guard let python = self.pythonExecutable(), let script = self.bundledScript() else {
                self.showAlert(title: "Python 3 est introuvable",
                               message: "Installez Python 3 avec Homebrew : brew install python")
                self.statusLine.title = "Python 3 manquant"
                return
            }

            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = [script, "--no-browser"]
            if let null = FileHandle(forWritingAtPath: "/dev/null") {
                process.standardOutput = null
                process.standardError = null
            }
            process.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self = self, !self.quitting else { return }
                    self.statusLine.title = "Moteur arrêté"
                }
            }
            do {
                try process.run()
                self.engine = process
                self.statusLine.title = "Connexion au moteur…"
                if showPanel { self.showPanelWhenReady(attempt: 0) }
            } catch {
                self.statusLine.title = "Échec du démarrage"
                self.showAlert(title: "Companion n’a pas démarré", message: error.localizedDescription)
            }
        }
    }

    private func showPanelWhenReady(attempt: Int) {
        engineIsReachable { [weak self] ready in
            guard let self = self else { return }
            if ready {
                self.statusLine.title = "Moteur connecté"
                self.showPanel()
            } else if attempt < 24 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.showPanelWhenReady(attempt: attempt + 1)
                }
            } else {
                self.statusLine.title = "Interface inaccessible"
                self.showAlert(title: "Interface inaccessible",
                               message: "Consultez le journal depuis le menu AMS Lite Companion.")
            }
        }
    }

    private func showPanel() {
        if webView.url == nil {
            webView.load(URLRequest(url: embeddedDashboardURL))
        } else {
            webView.reload()
        }
        if panelDocked { dockPanelToBambuStudio() }
        panel.makeKeyAndOrderFront(nil)
        panelMenuItem.title = "Masquer le panneau Companion"
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func togglePanel() {
        if panel.isVisible {
            panel.orderOut(nil)
            panelMenuItem.title = "Afficher le panneau Companion"
        } else {
            engineIsReachable { [weak self] ready in
                if ready {
                    self?.showPanel()
                } else {
                    self?.startEngine(showPanel: true)
                }
            }
        }
    }

    @objc private func toggleDocking() {
        panelDocked.toggle()
        UserDefaults.standard.set(panelDocked, forKey: "panelDocked")
        dockMenuItem.state = panelDocked ? .on : .off
        if panelDocked { dockPanelToBambuStudio() }
    }

    @objc private func openBrowserDashboard() {
        engineIsReachable { [weak self] ready in
            if ready {
                NSWorkspace.shared.open(dashboardURL)
            } else {
                self?.startEngine(showPanel: false)
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                    NSWorkspace.shared.open(dashboardURL)
                }
            }
        }
    }

    @objc private func poll() {
        var request = URLRequest(url: stateURL)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self = self else { return }
            DispatchQueue.main.async {
                if let data = data,
                   (response as? HTTPURLResponse)?.statusCode == 200,
                   let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.updateMenu(state)
                } else {
                    self.statusLine.title = "Moteur arrêté"
                }
                self.monitorBambuStudio()
            }
        }.resume()
    }

    private func updateMenu(_ state: [String: Any]) {
        if let printer = state["printer"] as? [String: Any] {
            let connected = printer["connected"] as? Bool ?? false
            let printState = printer["state"] as? String ?? "INCONNU"
            let progress = (printer["progress"] as? NSNumber)?.intValue ?? 0
            statusLine.title = connected
                ? "Imprimante connectée · \(printState) \(progress)%"
                : "Moteur actif · imprimante déconnectée"
            panel.title = connected
                ? "AMS Lite Companion · \(printState) \(progress)%"
                : "AMS Lite Companion"
        }
        guard let spools = state["spools"] as? [String: Any] else { return }
        for slot in 1...4 {
            guard let spool = spools[String(slot)] as? [String: Any] else { continue }
            let name = spool["name"] as? String ?? "Bobine A\(slot)"
            let remaining = (spool["remaining_g"] as? NSNumber)?.doubleValue ?? 0
            spoolLines[slot - 1].title = String(format: "A%d · %@ · %.1f g", slot, name, remaining)
        }
    }

    private func bambuApplication() -> NSRunningApplication? {
        NSWorkspace.shared.runningApplications.first { app in
            let name = (app.localizedName ?? "").lowercased()
            let bundle = (app.bundleIdentifier ?? "").lowercased()
            return name == "bambustudio" || name == "bambu studio" ||
                (bundle.contains("bambu") && bundle.contains("studio"))
        }
    }

    private func isBambuStudioRunning() -> Bool { bambuApplication() != nil }

    private func bambuWindowFrame() -> NSRect? {
        guard let app = bambuApplication(),
              let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                       kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        let mainTop = NSScreen.screens.first?.frame.maxY ?? 0
        var best: NSRect?
        for info in windows {
            guard (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == app.processIdentifier,
                  (info[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let bounds = info[kCGWindowBounds as String] as? [String: Any],
                  let rawX = bounds["X"] as? NSNumber,
                  let rawY = bounds["Y"] as? NSNumber,
                  let rawWidth = bounds["Width"] as? NSNumber,
                  let rawHeight = bounds["Height"] as? NSNumber else { continue }
            let cgRect = CGRect(x: CGFloat(rawX.doubleValue),
                                y: CGFloat(rawY.doubleValue),
                                width: CGFloat(rawWidth.doubleValue),
                                height: CGFloat(rawHeight.doubleValue))
            let rect = NSRect(x: cgRect.minX,
                              y: mainTop - cgRect.maxY,
                              width: cgRect.width,
                              height: cgRect.height)
            if rect.width * rect.height > (best?.width ?? 0) * (best?.height ?? 0) {
                best = rect
            }
        }
        return best
    }

    private func dockPanelToBambuStudio() {
        guard panelDocked, panel.isVisible || bambuSeen else { return }
        let bambu = bambuWindowFrame()
        let screen = bambu.flatMap { frame in
            NSScreen.screens.first(where: { $0.frame.intersects(frame) })
        } ?? NSScreen.main
        guard let visible = screen?.visibleFrame else { return }

        let width = min(panel.frame.width, visible.width)
        let height = min(panel.frame.height, visible.height)
        var x = visible.maxX - width
        var y = visible.maxY - height
        if let bambu = bambu {
            let gap = 8.0
            if bambu.maxX + gap + width <= visible.maxX {
                x = bambu.maxX + gap
            } else if bambu.minX - gap - width >= visible.minX {
                x = bambu.minX - gap - width
            }
            y = min(max(bambu.maxY - height, visible.minY), visible.maxY - height)
        }
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func monitorBambuStudio() {
        if isBambuStudioRunning() {
            bambuSeen = true
            bambuMissingPolls = 0
            if panelDocked && panel.isVisible { dockPanelToBambuStudio() }
        } else if bambuSeen {
            bambuMissingPolls += 1
            if bambuMissingPolls >= 2 { requestQuit() }
        }
    }

    private func findBambuStudio() -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "/Applications/BambuStudio.app",
            "/Applications/Bambu Studio.app",
            "\(home)/Applications/BambuStudio.app",
            "\(home)/Applications/Bambu Studio.app"
        ]
        return candidates.first(where: { FileManager.default.fileExists(atPath: $0) })
            .map { URL(fileURLWithPath: $0) }
    }

    private func launchBambuStudio() {
        guard !isBambuStudioRunning() else {
            bambuSeen = true
            return
        }
        guard let appURL = findBambuStudio() else {
            showAlert(title: "Bambu Studio officiel introuvable",
                      message: "Placez BambuStudio.app dans le dossier Applications. Companion reste disponible depuis son icône dans la barre des menus.")
            return
        }
        let configuration = NSWorkspace.OpenConfiguration()
        NSWorkspace.shared.openApplication(at: appURL, configuration: configuration) { [weak self] _, error in
            DispatchQueue.main.async {
                if let error = error {
                    self?.showAlert(title: "Impossible d’ouvrir Bambu Studio", message: error.localizedDescription)
                } else {
                    self?.bambuSeen = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                        self?.dockPanelToBambuStudio()
                    }
                }
            }
        }
    }

    @objc private func openBambu() { launchBambuStudio() }

    @objc private func restartEngine() {
        sendShutdown()
        if let process = engine, process.isRunning { process.terminate() }
        engine = nil
        webView.loadHTMLString("<html><body style='font-family:-apple-system;padding:24px'>Redémarrage du moteur…</body></html>",
                               baseURL: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.startEngine(showPanel: true)
        }
    }

    @objc private func openLog() {
        let log = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/AMS Lite Companion/companion.log")
        if FileManager.default.fileExists(atPath: log.path) {
            NSWorkspace.shared.activateFileViewerSelecting([log])
        } else {
            showAlert(title: "Journal absent", message: "Aucun journal n’a encore été créé.")
        }
    }

    private func sendShutdown() {
        var request = URLRequest(url: shutdownURL)
        request.httpMethod = "POST"
        request.httpBody = Data("{}".utf8)
        request.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: request).resume()
    }

    @objc private func quitCompanion() { requestQuit() }

    private func requestQuit() {
        guard !quitting else { return }
        quitting = true
        statusLine.title = "Arrêt…"
        sendShutdown()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
            NSApp.terminate(nil)
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        panelMenuItem.title = "Afficher le panneau Companion"
        return false
    }

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.scheme == "about" ||
            ((url.host == "127.0.0.1" || url.host == "localhost") && url.port == 8765) {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
