import AppKit
import Foundation

private let dashboardURL = URL(string: "http://127.0.0.1:8765/")!
private let stateURL = URL(string: "http://127.0.0.1:8765/api/state")!
private let shutdownURL = URL(string: "http://127.0.0.1:8765/api/shutdown")!

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var statusLine: NSMenuItem!
    private var spoolLines: [NSMenuItem] = []
    private var engine: Process?
    private var pollTimer: Timer?
    private var bambuSeen = false
    private var bambuMissingPolls = 0
    private var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        startEngine(openDashboard: true)
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
        let title = NSMenuItem(title: "AMS Lite Companion", action: nil, keyEquivalent: "")
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
        menu.addItem(NSMenuItem(title: "Ouvrir le tableau de bord",
                                action: #selector(openDashboard),
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

    private func startEngine(openDashboard: Bool) {
        engineIsReachable { [weak self] alreadyRunning in
            guard let self = self else { return }
            if alreadyRunning {
                self.statusLine.title = "Moteur connecté"
                if openDashboard { self.openDashboardWhenReady(attempt: 0) }
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
                if openDashboard { self.openDashboardWhenReady(attempt: 0) }
            } catch {
                self.statusLine.title = "Échec du démarrage"
                self.showAlert(title: "Companion n’a pas démarré", message: error.localizedDescription)
            }
        }
    }

    private func openDashboardWhenReady(attempt: Int) {
        engineIsReachable { [weak self] ready in
            guard let self = self else { return }
            if ready {
                self.statusLine.title = "Moteur connecté"
                NSWorkspace.shared.open(dashboardURL)
            } else if attempt < 20 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.openDashboardWhenReady(attempt: attempt + 1)
                }
            } else {
                self.statusLine.title = "Moteur inaccessible"
                self.showAlert(title: "Interface inaccessible",
                               message: "Consultez le journal depuis le menu AMS Lite Companion.")
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
        }
        guard let spools = state["spools"] as? [String: Any] else { return }
        for slot in 1...4 {
            guard let spool = spools[String(slot)] as? [String: Any] else { continue }
            let name = spool["name"] as? String ?? "Bobine A\(slot)"
            let remaining = (spool["remaining_g"] as? NSNumber)?.doubleValue ?? 0
            spoolLines[slot - 1].title = String(format: "A%d · %@ · %.1f g", slot, name, remaining)
        }
    }

    private func isBambuStudioRunning() -> Bool {
        NSWorkspace.shared.runningApplications.contains { app in
            let name = (app.localizedName ?? "").lowercased()
            let bundle = (app.bundleIdentifier ?? "").lowercased()
            return name == "bambustudio" || name == "bambu studio" ||
                (bundle.contains("bambu") && bundle.contains("studio"))
        }
    }

    private func monitorBambuStudio() {
        if isBambuStudioRunning() {
            bambuSeen = true
            bambuMissingPolls = 0
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
                }
            }
        }
    }

    @objc private func openDashboard() {
        engineIsReachable { [weak self] ready in
            if ready {
                NSWorkspace.shared.open(dashboardURL)
            } else {
                self?.startEngine(openDashboard: true)
            }
        }
    }

    @objc private func openBambu() { launchBambuStudio() }

    @objc private func restartEngine() {
        sendShutdown()
        if let process = engine, process.isRunning { process.terminate() }
        engine = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.startEngine(openDashboard: true)
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
