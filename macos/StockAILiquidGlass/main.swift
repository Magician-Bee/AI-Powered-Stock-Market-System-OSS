import AppKit
import Foundation
import WebKit

private enum AdaptiveApplicationIcon {
    static func apply() {
        let appearance = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua])
        let resourceName = appearance == .darkAqua ? "StockAIIconDark" : "StockAIIconLight"
        guard let url = Bundle.main.url(forResource: resourceName, withExtension: "png"),
              let image = NSImage(contentsOf: url)
        else { return }
        image.isTemplate = false
        NSApp.applicationIconImage = image
    }
}

private final class AppearanceObservingView: NSView {
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        DispatchQueue.main.async {
            AdaptiveApplicationIcon.apply()
        }
    }
}

private enum AppleLiquidGlass {
    static func makeGlassView(
        contentView: NSView? = nil,
        cornerRadius: CGFloat,
        tintAlpha: CGFloat,
        tintColor: NSColor = .white,
        style: NSGlassEffectView.Style = .clear
    ) -> NSGlassEffectView {
        let glassView = NSGlassEffectView(frame: .zero)
        let content = contentView ?? NSView(frame: .zero)
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor.clear.cgColor
        content.autoresizingMask = [.width, .height]
        glassView.contentView = content
        glassView.style = style
        glassView.cornerRadius = cornerRadius
        glassView.tintColor = tintColor.withAlphaComponent(tintAlpha)
        glassView.wantsLayer = true
        glassView.isHidden = true
        return glassView
    }

    static func makeContainer(
        glassViews: [NSGlassEffectView],
        spacing: CGFloat = 0
    ) -> NSGlassEffectContainerView {
        let container = NSGlassEffectContainerView(frame: .zero)
        let content = NSView(frame: .zero)
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor.clear.cgColor
        glassViews.forEach { glassView in
            glassView.translatesAutoresizingMaskIntoConstraints = true
            content.addSubview(glassView)
        }
        container.translatesAutoresizingMaskIntoConstraints = false
        container.contentView = content
        container.spacing = spacing
        return container
    }
}

private final class ToolbarOverlayView: NSView {
    var interactiveFrame: NSRect = .zero

    override func hitTest(_ point: NSPoint) -> NSView? {
        interactiveFrame.contains(point) ? super.hitTest(point) : nil
    }
}

private struct SidebarSelectionSnapshot: Equatable {
    let viewID: String
    let code: String
    let title: String
    let frame: CGRect
    let usesLightAppearance: Bool
}

private final class SidebarSelectionContentView: NSView {
    private let codeLabel = NSTextField(labelWithString: "")
    private let titleLabel = NSTextField(labelWithString: "")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor

        codeLabel.font = .systemFont(ofSize: 11, weight: .bold)
        codeLabel.alignment = .center
        titleLabel.font = .systemFont(ofSize: 14, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        [codeLabel, titleLabel].forEach {
            $0.drawsBackground = false
            $0.isBezeled = false
            $0.isEditable = false
            $0.isSelectable = false
            addSubview($0)
        }
    }

    required init?(coder: NSCoder) {
        nil
    }

    override func layout() {
        super.layout()
        let labelHeight: CGFloat = 28
        let labelY = bounds.midY - labelHeight / 2
        codeLabel.frame = NSRect(x: 12, y: labelY, width: 34, height: labelHeight)
        titleLabel.frame = NSRect(
            x: 58,
            y: labelY,
            width: max(0, bounds.width - 70),
            height: labelHeight
        )
    }

    func apply(_ snapshot: SidebarSelectionSnapshot) {
        codeLabel.stringValue = snapshot.code
        titleLabel.stringValue = snapshot.title
        codeLabel.textColor = snapshot.usesLightAppearance
            ? NSColor(calibratedRed: 0.25, green: 0.36, blue: 0.45, alpha: 1)
            : NSColor.white.withAlphaComponent(0.86)
        titleLabel.textColor = snapshot.usesLightAppearance
            ? NSColor(calibratedRed: 0.10, green: 0.18, blue: 0.25, alpha: 1)
            : NSColor.white.withAlphaComponent(0.94)
    }
}

private final class SidebarSelectionOverlayView: NSView {
    private let selectionContent = SidebarSelectionContentView(frame: .zero)
    private let glassView: NSGlassEffectView
    private var hasSelection = false

    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        glassView = AppleLiquidGlass.makeGlassView(
            contentView: selectionContent,
            cornerRadius: 22,
            tintAlpha: 0,
            style: .clear
        )
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        glassView.tintColor = nil
        glassView.isHidden = true
        addSubview(glassView)
        setAccessibilityElement(false)
    }

    required init?(coder: NSCoder) {
        nil
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        nil
    }

    func update(_ snapshot: SidebarSelectionSnapshot?) {
        guard let snapshot else {
            glassView.isHidden = true
            hasSelection = false
            return
        }

        selectionContent.apply(snapshot)
        glassView.style = .clear
        glassView.tintColor = nil
        glassView.cornerRadius = snapshot.frame.height / 2
        glassView.isHidden = false

        guard hasSelection else {
            glassView.frame = snapshot.frame
            hasSelection = true
            return
        }

        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.42
            context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            context.allowsImplicitAnimation = true
            glassView.animator().frame = snapshot.frame
        }
    }
}

private final class ToolbarSearchField: NSTextField {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }
}

private final class SearchFieldHostView: NSView {
    weak var searchField: NSTextField?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func mouseDown(with event: NSEvent) {
        if let searchField {
            window?.makeFirstResponder(searchField)
        }
        super.mouseDown(with: event)
    }
}

private enum ControlGlassDepth {
    case recessed
    case raised
}

private final class AdaptiveToolbarContentView: NSView {
    let titleLabel = NSTextField(labelWithString: "首頁")
    let accountButton: NSButton
    let searchField = ToolbarSearchField(frame: .zero)
    let searchButton: NSButton
    private let eyebrowLabel = NSTextField(labelWithString: "行情資料 · 策略研究 · 風控執行 · 投資工作區")
    private let searchIcon = NSImageView(frame: .zero)
    private let searchFieldHost = SearchFieldHostView(frame: .zero)
    private let searchButtonHost = NSView(frame: .zero)
    private let accountButtonHost = NSView(frame: .zero)
    private var searchFieldGlass: NSGlassEffectView?
    private var searchButtonGlass: NSGlassEffectView?
    private var accountButtonGlass: NSGlassEffectView?
    private var appliedTheme: String?
    private var operatorLabel = "Codex"

    init(target: AnyObject, searchAction: Selector, accountAction: Selector) {
        searchButton = NSButton(title: "傳送", target: target, action: searchAction)
        accountButton = NSButton(title: "Codex", target: target, action: accountAction)
        super.init(frame: .zero)

        eyebrowLabel.font = .systemFont(ofSize: 10, weight: .semibold)
        eyebrowLabel.textColor = .secondaryLabelColor
        eyebrowLabel.lineBreakMode = .byTruncatingTail
        titleLabel.font = .systemFont(ofSize: 24, weight: .bold)
        searchField.placeholderString = "交給 AI Agent：查行情、分析風險或操作專案…"
        searchField.font = .systemFont(ofSize: 14, weight: .medium)
        searchField.isBezeled = false
        searchField.drawsBackground = false
        searchField.backgroundColor = .clear
        searchField.textColor = .white
        searchField.focusRingType = .none
        searchField.isEditable = true
        searchField.isSelectable = true
        searchField.isEnabled = true
        searchField.cell?.usesSingleLineMode = true
        searchField.cell?.lineBreakMode = .byTruncatingTail
        searchField.setAccessibilityLabel("與 AI Agent 對話")
        searchIcon.image = NSImage(systemSymbolName: "sparkles", accessibilityDescription: "AI Agent")
        searchIcon.imageScaling = .scaleProportionallyDown
        searchIcon.contentTintColor = NSColor.white.withAlphaComponent(0.64)
        searchButton.isBordered = false
        searchButton.contentTintColor = .white
        searchButton.focusRingType = .none
        searchButton.setAccessibilityLabel("傳送給 AI Agent")
        accountButton.isBordered = false
        accountButton.contentTintColor = .white
        accountButton.focusRingType = .none
        accountButton.image = NSImage(
            systemSymbolName: "person.crop.circle",
            accessibilityDescription: "Codex 帳號"
        )
        accountButton.imagePosition = .imageLeading
        accountButton.setAccessibilityLabel("Codex 帳號")

        [searchFieldHost, searchButtonHost, accountButtonHost].forEach {
            $0.wantsLayer = true
            $0.layer?.backgroundColor = NSColor.clear.cgColor
        }
        searchFieldHost.addSubview(searchIcon)
        searchFieldHost.addSubview(searchField)
        searchFieldHost.searchField = searchField
        searchButtonHost.addSubview(searchButton)
        accountButtonHost.addSubview(accountButton)

        let darkGlassTint = NSColor(calibratedWhite: 0.04, alpha: 1)
        searchFieldGlass = AppleLiquidGlass.makeGlassView(
            cornerRadius: 14,
            tintAlpha: 0.32,
            tintColor: darkGlassTint
        )
        searchButtonGlass = AppleLiquidGlass.makeGlassView(
            cornerRadius: 14,
            tintAlpha: 0.36,
            tintColor: darkGlassTint
        )
        accountButtonGlass = AppleLiquidGlass.makeGlassView(
            cornerRadius: 14,
            tintAlpha: 0.34,
            tintColor: darkGlassTint
        )
        [searchFieldGlass, searchButtonGlass, accountButtonGlass].compactMap { $0 }.forEach {
            $0.isHidden = false
            $0.alphaValue = 0.97
            addSubview($0)
        }
        [searchFieldHost, searchButtonHost, accountButtonHost].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = true
            addSubview($0)
        }
        configureControlGlass(searchFieldGlass, depth: .recessed)
        configureControlGlass(searchButtonGlass, depth: .raised)
        configureControlGlass(accountButtonGlass, depth: .raised)

        [eyebrowLabel, titleLabel].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = true
            addSubview($0)
        }
        applyInterfaceTheme("exchange")
    }

    required init?(coder: NSCoder) {
        nil
    }

    func applyInterfaceTheme(_ theme: String) {
        let light = theme == "pearl" || theme == "daylight"
        guard theme != appliedTheme || titleLabel.textColor == nil else { return }
        appliedTheme = theme
        let foreground = light ? NSColor(calibratedWhite: 0.10, alpha: 1) : .white
        let secondary = light
            ? NSColor(calibratedWhite: 0.24, alpha: 0.82)
            : NSColor.white.withAlphaComponent(0.58)
        titleLabel.textColor = foreground
        eyebrowLabel.textColor = secondary
        searchField.textColor = foreground
        searchIcon.contentTintColor = light
            ? NSColor(calibratedWhite: 0.25, alpha: 0.64)
            : NSColor.white.withAlphaComponent(0.64)
        searchField.placeholderAttributedString = NSAttributedString(
            string: "交給 AI Agent：查行情、分析風險或操作專案…",
            attributes: [
                .foregroundColor: light
                    ? NSColor(calibratedWhite: 0.28, alpha: 0.72)
                    : NSColor.white.withAlphaComponent(0.46),
                .font: NSFont.systemFont(ofSize: 14, weight: .medium),
            ]
        )
        searchButton.contentTintColor = foreground
        accountButton.contentTintColor = foreground
        searchButton.attributedTitle = NSAttributedString(
            string: "傳送",
            attributes: [.foregroundColor: foreground, .font: NSFont.systemFont(ofSize: 13, weight: .semibold)]
        )
        accountButton.attributedTitle = NSAttributedString(
            string: operatorLabel,
            attributes: [.foregroundColor: foreground, .font: NSFont.systemFont(ofSize: 13, weight: .semibold)]
        )
        updateControlGlassTheme(searchFieldGlass, depth: .recessed, theme: theme)
        updateControlGlassTheme(searchButtonGlass, depth: .raised, theme: theme)
        updateControlGlassTheme(accountButtonGlass, depth: .raised, theme: theme)
        needsDisplay = true
    }

    func applyOperatorLabel(_ value: String) {
        let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
        let next = normalized.isEmpty ? "Agent" : normalized
        guard next != operatorLabel else { return }
        operatorLabel = next
        let foreground = accountButton.contentTintColor ?? .white
        accountButton.attributedTitle = NSAttributedString(
            string: operatorLabel,
            attributes: [.foregroundColor: foreground, .font: NSFont.systemFont(ofSize: 13, weight: .semibold)]
        )
        accountButton.setAccessibilityLabel("\(operatorLabel) 操作員與模型設定")
        needsLayout = true
    }

    override func layout() {
        super.layout()
        let width = bounds.width
        let height = bounds.height
        let inset: CGFloat = width < 760 ? 14 : 18
        let buttonWidth: CGFloat = width < 760 ? 76 : 82
        let accountWidth: CGFloat = width < 760 ? 46 : 112
        accountButton.imagePosition = width < 760 ? .imageOnly : .imageLeading

        if width < 760 {
            eyebrowLabel.frame = NSRect(x: inset, y: height - 34, width: width - accountWidth - 50, height: 14)
            titleLabel.frame = NSRect(x: inset, y: height - 64, width: width - accountWidth - 50, height: 30)
            place(accountButtonGlass, host: accountButtonHost, control: accountButton, frame: NSRect(x: width - inset - accountWidth, y: height - 58, width: accountWidth, height: 38), horizontalInset: 4, verticalInset: 3)
            place(searchButtonGlass, host: searchButtonHost, control: searchButton, frame: NSRect(x: width - inset - buttonWidth, y: 10, width: buttonWidth, height: 42), horizontalInset: 4, verticalInset: 3)
            place(searchFieldGlass, host: searchFieldHost, control: searchField, frame: NSRect(
                x: inset,
                y: 10,
                width: max(120, width - (inset * 3) - buttonWidth),
                height: 42
            ), horizontalInset: 10, verticalInset: 0)
        } else {
            let controlHeight: CGFloat = 44
            let controlsY = (height - controlHeight) / 2
            let titleMinimum: CGFloat = 180
            let availableSearchWidth = width - accountWidth - buttonWidth - titleMinimum - (inset * 5) - 8
            let searchWidth = min(600, max(340, availableSearchWidth))
            let accountFrame = NSRect(
                x: width - inset - accountWidth,
                y: controlsY,
                width: accountWidth,
                height: controlHeight
            )
            let searchButtonFrame = NSRect(
                x: accountFrame.minX - inset - buttonWidth,
                y: controlsY,
                width: buttonWidth,
                height: controlHeight
            )
            let searchFieldFrame = NSRect(
                x: searchButtonFrame.minX - 8 - searchWidth,
                y: controlsY,
                width: searchWidth,
                height: controlHeight
            )
            place(accountButtonGlass, host: accountButtonHost, control: accountButton, frame: accountFrame, horizontalInset: 5, verticalInset: 3)
            place(searchButtonGlass, host: searchButtonHost, control: searchButton, frame: searchButtonFrame, horizontalInset: 5, verticalInset: 3)
            place(searchFieldGlass, host: searchFieldHost, control: searchField, frame: searchFieldFrame, horizontalInset: 11, verticalInset: 0)
            let titleWidth = max(170, searchFieldFrame.minX - (inset * 2))
            eyebrowLabel.frame = NSRect(x: inset, y: height / 2 + 7, width: titleWidth, height: 14)
            titleLabel.frame = NSRect(x: inset, y: height / 2 - 24, width: titleWidth, height: 31)
        }
    }

    private func configureControlGlass(_ glass: NSGlassEffectView?, depth: ControlGlassDepth) {
        guard let glass else { return }
        glass.wantsLayer = true
        glass.cornerRadius = 14
        glass.style = .clear
        updateControlGlassTheme(glass, depth: depth, theme: "exchange")
    }

    private func updateControlGlassTheme(
        _ glass: NSGlassEffectView?,
        depth: ControlGlassDepth,
        theme: String
    ) {
        guard let glass else { return }
        let light = theme == "pearl" || theme == "daylight"
        let host: NSView? = glass === searchFieldGlass
            ? searchFieldHost
            : (glass === searchButtonGlass ? searchButtonHost : accountButtonHost)
        glass.style = .clear
        glass.cornerRadius = 14
        host?.wantsLayer = true
        host?.layer?.cornerRadius = 14
        host?.layer?.backgroundColor = NSColor.clear.cgColor
        host?.layer?.borderWidth = 0
        host?.layer?.borderColor = NSColor.clear.cgColor
        host?.layer?.shadowOpacity = 0
        if light {
            glass.isHidden = false
            glass.alphaValue = 1.0
            glass.tintColor = nil
        } else {
            glass.alphaValue = 1.0
            let themeTint: NSColor
            switch theme {
            case "clarity":
                themeTint = NSColor(calibratedRed: 0.05, green: 0.20, blue: 0.31, alpha: 1)
            case "terminal":
                themeTint = NSColor(calibratedRed: 0.025, green: 0.20, blue: 0.11, alpha: 1)
            case "aurora":
                themeTint = NSColor(calibratedRed: 0.17, green: 0.08, blue: 0.30, alpha: 1)
            case "graphite":
                themeTint = NSColor(calibratedWhite: 0.035, alpha: 1)
            default:
                themeTint = NSColor(calibratedWhite: 0.08, alpha: 1)
            }
            glass.tintColor = themeTint.withAlphaComponent(depth == .recessed ? 0.08 : 0.12)
        }
    }

    private func place(
        _ glass: NSView?,
        host: NSView,
        control: NSView,
        frame: NSRect,
        horizontalInset: CGFloat,
        verticalInset: CGFloat
    ) {
        if let glass {
            glass.frame = frame
            glass.layer?.sublayers?.first(where: { $0.name == "control-glass-depth" })?.frame = glass.bounds.insetBy(dx: 1, dy: 1)
            glass.layer?.shadowPath = CGPath(
                roundedRect: glass.bounds,
                cornerWidth: 14,
                cornerHeight: 14,
                transform: nil
            )
            host.frame = frame
            layoutControl(control, in: host.bounds, horizontalInset: horizontalInset, verticalInset: verticalInset)
        } else {
            if control === searchField {
                host.frame = frame
                layoutControl(control, in: host.bounds, horizontalInset: horizontalInset, verticalInset: verticalInset)
            } else {
                control.frame = frame
            }
        }
    }

    private func layoutControl(
        _ control: NSView,
        in bounds: NSRect,
        horizontalInset: CGFloat,
        verticalInset: CGFloat
    ) {
        if control === searchField {
            let iconSize: CGFloat = 18
            let iconX = horizontalInset + 2
            let textHeight: CGFloat = 28
            searchIcon.frame = NSRect(
                x: iconX,
                y: bounds.midY - iconSize / 2,
                width: iconSize,
                height: iconSize
            )
            control.frame = NSRect(
                x: iconX + iconSize + 9,
                y: bounds.midY - textHeight / 2 - 1,
                width: max(0, bounds.width - iconX - iconSize - 9 - horizontalInset),
                height: textHeight
            )
        } else {
            control.frame = bounds.insetBy(dx: horizontalInset, dy: verticalInset)
        }
    }
}

private final class StockAIViewController: NSViewController, WKNavigationDelegate, WKUIDelegate,
    WKScriptMessageHandler, NSTextFieldDelegate
{
    private let serverURL: URL
    private let webView: WKWebView
    private var sidebarGlass: NSGlassEffectView?
    private var toolbarGlass: NSGlassEffectView?
    private var toolbarOverlay: ToolbarOverlayView?
    private var toolbarContent: AdaptiveToolbarContentView?
    private var sidebarSelectionOverlay: SidebarSelectionOverlayView?
    private var glassViewportSize: NSSize = .zero
    private var appliedChromeTheme: String?

    init(serverURL: URL) {
        self.serverURL = serverURL
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: Self.nativeModeScript,
                injectionTime: .atDocumentEnd,
                forMainFrameOnly: true
            )
        )
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init(nibName: nil, bundle: nil)
        configuration.userContentController.add(self, name: "glassLayout")
    }

    required init?(coder: NSCoder) {
        nil
    }

    deinit {
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "glassLayout")
    }

    override func loadView() {
        let root = AppearanceObservingView(frame: NSRect(x: 0, y: 0, width: 1440, height: 900))
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.black.cgColor
        view = root

        let toolbarContent = AdaptiveToolbarContentView(
            target: self,
            searchAction: #selector(runSearch),
            accountAction: #selector(openAccount)
        )
        toolbarContent.searchField.delegate = self

        let sidebarGlass = AppleLiquidGlass.makeGlassView(
            cornerRadius: 24,
            tintAlpha: 0.06
        )
        let toolbarGlass = AppleLiquidGlass.makeGlassView(
            cornerRadius: 22,
            tintAlpha: 0.035
        )
        let sidebarContainer = AppleLiquidGlass.makeContainer(
            glassViews: [sidebarGlass]
        )
        let toolbarContainer = AppleLiquidGlass.makeContainer(
            glassViews: [toolbarGlass]
        )

        self.sidebarGlass = sidebarGlass
        self.toolbarGlass = toolbarGlass
        self.toolbarContent = toolbarContent
        toolbarContent.autoresizingMask = [.width, .height]
        toolbarContent.translatesAutoresizingMaskIntoConstraints = true
        toolbarGlass.alphaValue = 1.0
        updateChromeGlassTheme(theme: "exchange")
        root.addSubview(sidebarContainer)
        NSLayoutConstraint.activate([
            sidebarContainer.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sidebarContainer.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            sidebarContainer.topAnchor.constraint(equalTo: root.topAnchor),
            sidebarContainer.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.clear.cgColor
        root.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.topAnchor.constraint(equalTo: root.topAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        let sidebarSelectionOverlay = SidebarSelectionOverlayView(frame: .zero)
        self.sidebarSelectionOverlay = sidebarSelectionOverlay
        sidebarSelectionOverlay.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(sidebarSelectionOverlay)
        NSLayoutConstraint.activate([
            sidebarSelectionOverlay.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sidebarSelectionOverlay.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            sidebarSelectionOverlay.topAnchor.constraint(equalTo: root.topAnchor),
            sidebarSelectionOverlay.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        let toolbarOverlay = ToolbarOverlayView(frame: .zero)
        self.toolbarOverlay = toolbarOverlay
        toolbarOverlay.translatesAutoresizingMaskIntoConstraints = false
        toolbarOverlay.addSubview(toolbarContainer)
        toolbarOverlay.addSubview(toolbarContent)
        root.addSubview(toolbarOverlay)
        NSLayoutConstraint.activate([
            toolbarOverlay.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            toolbarOverlay.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            toolbarOverlay.topAnchor.constraint(equalTo: root.topAnchor),
            toolbarOverlay.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            toolbarContainer.leadingAnchor.constraint(equalTo: toolbarOverlay.leadingAnchor),
            toolbarContainer.trailingAnchor.constraint(equalTo: toolbarOverlay.trailingAnchor),
            toolbarContainer.topAnchor.constraint(equalTo: toolbarOverlay.topAnchor),
            toolbarContainer.bottomAnchor.constraint(equalTo: toolbarOverlay.bottomAnchor),
        ])
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        webView.load(URLRequest(url: serverURL))
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard message.name == "glassLayout",
              let body = message.body as? [String: Any]
        else {
            return
        }

        if (body["kind"] as? String) == "navigationSelection" {
            guard let viewport = body["viewport"] as? [String: Any] else { return }
            updateSidebarSelection(
                from: body["selection"] as? [String: Any],
                viewID: (body["view"] as? String) ?? "",
                code: (body["code"] as? String) ?? "",
                title: (body["label"] as? String) ?? "",
                theme: (body["theme"] as? String) ?? "exchange",
                viewport: viewport
            )
            return
        }

        guard let viewport = body["viewport"] as? [String: Any] else { return }
        let viewportWidth = number(viewport["width"])
        let viewportHeight = number(viewport["height"])
        guard viewportWidth > 0, viewportHeight > 0 else { return }

        let viewportSize = NSSize(width: viewportWidth, height: viewportHeight)
        view.layoutSubtreeIfNeeded()
        let scaleX = view.bounds.width / viewportWidth
        let scaleY = view.bounds.height / viewportHeight
        updateGlass(
            sidebarGlass,
            from: body["sidebar"] as? [String: Any],
            scaleX: scaleX,
            scaleY: scaleY
        )
        let chartFocus = (body["chartFocus"] as? Bool) ?? false
        updateGlass(
            toolbarGlass,
            from: chartFocus ? nil : body["toolbar"] as? [String: Any],
            scaleX: scaleX,
            scaleY: scaleY
        )
        if let toolbarGlass, !toolbarGlass.isHidden {
            toolbarOverlay?.interactiveFrame = toolbarGlass.frame
        }
        glassViewportSize = viewportSize
        toolbarContent?.titleLabel.stringValue = (body["title"] as? String) ?? "股市 AI 系統"
        toolbarContent?.applyOperatorLabel((body["operatorLabel"] as? String) ?? "Codex")
        let theme = (body["theme"] as? String) ?? "exchange"
        toolbarContent?.applyInterfaceTheme(theme)
        updateChromeGlassTheme(theme: theme)
    }

    private func updateSidebarSelection(
        from rectangle: [String: Any]?,
        viewID: String,
        code: String,
        title: String,
        theme: String,
        viewport: [String: Any]
    ) {
        guard let rectangle else {
            sidebarSelectionOverlay?.update(nil)
            return
        }
        let viewportWidth = number(viewport["width"])
        let viewportHeight = number(viewport["height"])
        guard viewportWidth > 0, viewportHeight > 0 else { return }
        view.layoutSubtreeIfNeeded()
        let scaleX = view.bounds.width / viewportWidth
        let scaleY = view.bounds.height / viewportHeight
        let width = number(rectangle["width"]) * scaleX
        let height = number(rectangle["height"]) * scaleY
        guard width > 1, height > 1 else {
            sidebarSelectionOverlay?.update(nil)
            return
        }
        let rawFrame = CGRect(
            x: number(rectangle["x"]) * scaleX,
            y: number(rectangle["y"]) * scaleY,
            width: width,
            height: height
        )
        let targetFrame = rawFrame.insetBy(dx: 7, dy: 4)
        sidebarSelectionOverlay?.update(
            SidebarSelectionSnapshot(
                viewID: viewID,
                code: code,
                title: title,
                frame: targetFrame,
                usesLightAppearance: theme == "pearl" || theme == "daylight"
            )
        )
    }

    private func updateChromeGlassTheme(theme: String) {
        guard theme != appliedChromeTheme || toolbarGlass?.layer?.borderColor == nil else { return }
        appliedChromeTheme = theme
        let light = theme == "pearl" || theme == "daylight"

        guard let glass = toolbarGlass else { return }
        glass.wantsLayer = true
        glass.style = .clear
        if light {
            glass.isHidden = false
            // The light interface must reveal the page backdrop with no grey
            // material wash. The toolbar controls are separate sibling views,
            // so hiding this material does not fade the search field/buttons.
            glass.alphaValue = 1.0
            glass.tintColor = nil
            glass.layer?.backgroundColor = NSColor.clear.cgColor
            glass.layer?.borderWidth = 0
            glass.layer?.borderColor = NSColor.clear.cgColor
            glass.layer?.shadowColor = NSColor.black.cgColor
            glass.layer?.shadowOpacity = 0.0
            glass.layer?.shadowRadius = 0
            glass.layer?.shadowOffset = NSSize(width: 0, height: -4)
        } else {
            glass.isHidden = false
            let themeTint: NSColor
            switch theme {
            case "clarity":
                themeTint = NSColor(calibratedRed: 0.035, green: 0.16, blue: 0.25, alpha: 1)
            case "terminal":
                themeTint = NSColor(calibratedRed: 0.018, green: 0.15, blue: 0.075, alpha: 1)
            case "aurora":
                themeTint = NSColor(calibratedRed: 0.14, green: 0.055, blue: 0.25, alpha: 1)
            case "graphite":
                themeTint = NSColor(calibratedWhite: 0.018, alpha: 1)
            default:
                themeTint = NSColor(calibratedWhite: 0.04, alpha: 1)
            }
            glass.alphaValue = 1.0
            glass.tintColor = themeTint.withAlphaComponent(0.08)
            glass.layer?.backgroundColor = NSColor.clear.cgColor
            glass.layer?.borderWidth = 0
            glass.layer?.borderColor = nil
            glass.layer?.shadowColor = NSColor.black.cgColor
            glass.layer?.shadowOpacity = 0.24
            glass.layer?.shadowRadius = 14
            glass.layer?.shadowOffset = NSSize(width: 0, height: -3)
        }
    }

    private func updateGlass(
        _ glassView: NSGlassEffectView?,
        from rectangle: [String: Any]?,
        scaleX: CGFloat,
        scaleY: CGFloat
    ) {
        guard let glassView, let rectangle else {
            glassView?.isHidden = true
            if glassView === toolbarGlass {
                toolbarContent?.isHidden = true
                toolbarOverlay?.interactiveFrame = .zero
            }
            return
        }
        let width = number(rectangle["width"]) * scaleX
        let height = number(rectangle["height"]) * scaleY
        guard width > 1, height > 1 else {
            glassView.isHidden = true
            if glassView === toolbarGlass {
                toolbarContent?.isHidden = true
                toolbarOverlay?.interactiveFrame = .zero
            }
            return
        }
        let x = number(rectangle["x"]) * scaleX
        let top = number(rectangle["y"]) * scaleY
        glassView.frame = NSRect(
            x: x,
            y: view.bounds.height - top - height,
            width: width,
            height: height
        )
        let radius = max(0, number(rectangle["radius"]) * min(scaleX, scaleY))
        glassView.cornerRadius = radius
        glassView.isHidden = false
        if glassView === toolbarGlass, let toolbarContent {
            toolbarContent.isHidden = false
            toolbarContent.frame = glassView.frame
            toolbarContent.needsLayout = true
            toolbarContent.layoutSubtreeIfNeeded()
        }
    }

    private func number(_ value: Any?) -> CGFloat {
        CGFloat((value as? NSNumber)?.doubleValue ?? 0)
    }

    @objc private func runSearch() {
        guard let toolbarContent else { return }
        let query = toolbarContent.searchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return }
        let data = try? JSONSerialization.data(withJSONObject: [query])
        let array = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        let encoded = String(array.dropFirst().dropLast())
        webView.evaluateJavaScript(
            """
            (() => {
              const input = document.getElementById('globalAgentPrompt');
              if (!input) return false;
              input.value = \(encoded);
              input.dispatchEvent(new Event('input', {bubbles:true}));
              document.getElementById('globalAgentSend')?.click();
              if (typeof setView === 'function') setView('home');
              setTimeout(() => document.querySelector('.agent-runtime-panel')?.scrollIntoView({behavior:'smooth',block:'start'}), 120);
              return true;
            })()
            """
        ) { [weak self] result, error in
            guard error == nil, (result as? Bool) == true else { return }
            DispatchQueue.main.async {
                self?.toolbarContent?.searchField.stringValue = ""
            }
        }
    }

    @objc private func openAccount() {
        webView.evaluateJavaScript("document.getElementById('accountMenuBtn')?.click()")
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        if let movement = obj.userInfo?["NSTextMovement"] as? Int,
           movement == NSReturnTextMovement {
            runSearch()
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.host == "127.0.0.1" || url.host == "localhost" || url.scheme == "about" {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    private static let nativeModeScript = """
    (() => {
      document.documentElement.dataset.nativeLiquidGlass = 'appkit';
      const style = document.createElement('style');
      style.id = 'apple-native-liquid-glass-mode';
      style.textContent = `
        /* Keep the page's theme-owned background.  WKWebView paints its
           transparent document onto a white backing store, which erased the
           exchange theme's white text and made the application look blank. */
        html:not([data-ui-theme="pearl"]):not([data-ui-theme="daylight"]) body{
          background:var(--page-bg)!important
        }
        html:is([data-ui-theme="pearl"],[data-ui-theme="daylight"]) body{
          background:#e7ecf0!important
        }
        .app-shell{background:transparent!important}
        .liquid-native-canvas,.liquid-lens-proxy,.liquid-webgl-lens,
        body > canvas[data-liquid-ignore],.glass-sample-layer{display:none!important}
        .sidebar,.topbar,.glass-navigation{
          background:transparent!important;
          backdrop-filter:none!important;
          -webkit-backdrop-filter:none!important
        }
        html[data-native-liquid-glass="appkit"][data-ui-theme="pearl"] body .topbar.glass-navigation.glass-group,
        html[data-native-liquid-glass="appkit"][data-ui-theme="daylight"] body .topbar.glass-navigation.glass-group{
          background:transparent!important;
          background-color:transparent!important;
          border-color:transparent!important;
          box-shadow:none!important;
          outline-color:transparent!important
        }
        .topbar-content{visibility:hidden!important}
        html[data-native-liquid-glass="appkit"] .nav-menu.stock-nav-switcher::after{
          display:none!important
        }
        html[data-native-liquid-glass="appkit"] .nav-menu{gap:4px!important}
        html[data-native-liquid-glass="appkit"] .nav-btn,
        html[data-native-liquid-glass="appkit"] .nav-btn:hover,
        html[data-native-liquid-glass="appkit"] .nav-btn.active,
        html[data-native-liquid-glass="appkit"] .nav-btn.active:hover{
          position:relative!important;
          z-index:52!important;
          min-height:48px!important;
          padding:9px 12px!important;
          border-radius:999px!important;
          background:transparent!important;
          border-color:transparent!important;
          box-shadow:none!important;
          backdrop-filter:none!important;
          -webkit-backdrop-filter:none!important;
          transform:none!important
        }
        html[data-native-liquid-glass="appkit"] .nav-btn:hover:not(.active){
          background:rgba(127,151,173,.075)!important
        }
        html[data-native-liquid-glass="appkit"] .nav-btn>span:not(.glass-control-lens){
          background:rgba(127,151,173,.075)!important;
          border-color:rgba(127,151,173,.14)!important;
          box-shadow:none!important
        }
        html[data-native-liquid-glass="appkit"] .nav-btn.active{
          color:transparent!important;
          text-shadow:none!important
        }
        html[data-native-liquid-glass="appkit"] .nav-btn.active::after{display:none!important}
        html[data-native-liquid-glass="appkit"] .nav-btn.active>span:not(.glass-control-lens){
          opacity:0!important
        }
        html[data-native-liquid-glass="appkit"] body .sidebar .nav-menu.stock-nav-switcher::after{
          content:none!important;
          display:none!important;
          background:transparent!important;
          backdrop-filter:none!important;
          -webkit-backdrop-filter:none!important;
          box-shadow:none!important;
          animation:none!important;
          transition:none!important
        }
        html[data-native-liquid-glass="appkit"] body .sidebar .nav-menu.stock-nav-switcher>.nav-btn.active,
        html[data-native-liquid-glass="appkit"] body .sidebar .nav-menu.stock-nav-switcher>.nav-btn.active:hover{
          color:transparent!important;
          background:transparent!important;
          border-color:transparent!important;
          box-shadow:none!important;
          backdrop-filter:none!important;
          -webkit-backdrop-filter:none!important
        }
        html[data-native-liquid-glass="appkit"] body .sidebar .nav-menu.stock-nav-switcher>.nav-btn.active>span:not(.glass-control-lens){
          color:transparent!important;
          opacity:0!important;
          background:transparent!important;
          border-color:transparent!important;
          box-shadow:none!important
        }
        .panel > :is(.event-list,.table,.summary-cards,.process-flow,.answer,.json-box,.linkage-result)
          + :is(.event-list,.table,.summary-cards,.process-flow,.answer,.json-box,.linkage-result){
          margin-top:18px!important
        }
      `;
      document.head.appendChild(style);

      const box = (element) => {
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        const radius = Number.parseFloat(getComputedStyle(element).borderRadius) || 0;
        return {x:rect.x,y:rect.y,width:rect.width,height:rect.height,radius};
      };
      let selectionScheduled = false;
      const reportNavigationSelection = () => {
        selectionScheduled = false;
        const active = document.querySelector('.nav-menu .nav-btn.active');
        const code = active?.querySelector(':scope > span')?.textContent?.trim() || '';
        const label = active ? [...active.childNodes]
          .filter((node) => node.nodeType === Node.TEXT_NODE)
          .map((node) => node.textContent || '')
          .join(' ')
          .replace(/\\s+/g,' ')
          .trim() : '';
        window.webkit.messageHandlers.glassLayout.postMessage({
          kind:'navigationSelection',
          viewport:{width:window.innerWidth,height:window.innerHeight},
          selection:box(active),
          view:active?.dataset.view || '',
          code,
          label:label || active?.dataset.title || active?.getAttribute('title') || '',
          theme:document.documentElement.dataset.uiTheme || 'exchange'
        });
      };
      const scheduleNavigationSelection = () => {
        if (selectionScheduled) return;
        selectionScheduled = true;
        requestAnimationFrame(reportNavigationSelection);
      };
      let scheduled = false;
      const reportLayout = () => {
        scheduled = false;
        const theme = document.documentElement.dataset.uiTheme || 'exchange';
        const topbar = document.querySelector('.topbar');
        const usesTransparentLightChrome = theme === 'pearl' || theme === 'daylight';
        if (topbar) {
          if (usesTransparentLightChrome) {
            topbar.style.setProperty('background', 'transparent', 'important');
            topbar.style.setProperty('background-color', 'transparent', 'important');
            topbar.style.setProperty('border-color', 'transparent', 'important');
            topbar.style.setProperty('box-shadow', 'none', 'important');
            topbar.style.setProperty('outline-color', 'transparent', 'important');
          } else {
            ['background','background-color','border-color','box-shadow','outline-color'].forEach((property) => {
              topbar.style.removeProperty(property);
            });
          }
        }
        window.webkit.messageHandlers.glassLayout.postMessage({
          viewport:{width:window.innerWidth,height:window.innerHeight},
          sidebar:box(document.querySelector('.sidebar')),
          toolbar:box(topbar),
          title:document.getElementById('viewTitle')?.textContent?.trim() || '股市 AI 系統',
          operatorLabel:document.getElementById('accountLabel')?.textContent?.trim() || 'Codex',
          chartFocus:document.documentElement.dataset.chartFocus === 'true',
          theme
        });
        scheduleNavigationSelection();
      };
      const scheduleLayout = () => {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(reportLayout);
      };
      window.addEventListener('resize', scheduleLayout, {passive:true});
      window.addEventListener('load', scheduleLayout, {once:true});
      const observer = new ResizeObserver(scheduleLayout);
      const sidebar = document.querySelector('.sidebar');
      const toolbar = document.querySelector('.topbar');
      if (sidebar) observer.observe(sidebar);
      if (toolbar) observer.observe(toolbar);
      const title = document.getElementById('viewTitle');
      if (title) new MutationObserver(scheduleLayout).observe(title,{subtree:true,childList:true,characterData:true});
      const operatorLabel = document.getElementById('accountLabel');
      if (operatorLabel) new MutationObserver(scheduleLayout).observe(operatorLabel,{subtree:true,childList:true,characterData:true});
      new MutationObserver(scheduleLayout).observe(document.documentElement,{attributes:true,attributeFilter:['data-ui-theme','data-chart-focus']});
      const navMenu = document.querySelector('.nav-menu');
      if (navMenu) {
        new MutationObserver(scheduleNavigationSelection).observe(navMenu,{
          subtree:true,
          childList:true,
          attributes:true,
          attributeFilter:['class']
        });
        navMenu.addEventListener('click', scheduleNavigationSelection, {passive:true});
        navMenu.addEventListener('scroll', scheduleNavigationSelection, {passive:true});
      }
      scheduleLayout();
      scheduleNavigationSelection();
    })();
    """
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow?

    private func installMainMenu() {
        let mainMenu = NSMenu()

        let applicationMenuItem = NSMenuItem()
        mainMenu.addItem(applicationMenuItem)
        let applicationMenu = NSMenu(title: "股市 AI 系統")
        applicationMenu.addItem(
            NSMenuItem(
                title: "關於股市 AI 系統",
                action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                keyEquivalent: ""
            )
        )
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(
            NSMenuItem(
                title: "結束股市 AI 系統",
                action: #selector(NSApplication.terminate(_:)),
                keyEquivalent: "q"
            )
        )
        applicationMenuItem.submenu = applicationMenu

        let editMenuItem = NSMenuItem(title: "編輯", action: nil, keyEquivalent: "")
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "編輯")
        let editCommands: [(String, Selector, String)] = [
            ("剪下", #selector(NSText.cut(_:)), "x"),
            ("複製", #selector(NSText.copy(_:)), "c"),
            ("貼上", #selector(NSText.paste(_:)), "v"),
            ("全選", #selector(NSText.selectAll(_:)), "a"),
        ]
        for (title, action, shortcut) in editCommands {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: shortcut)
            item.target = nil
            editMenu.addItem(item)
        }
        editMenuItem.submenu = editMenu
        NSApp.mainMenu = mainMenu
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMainMenu()
        let serverURLString = Bundle.main.object(forInfoDictionaryKey: "StockAIServerURL") as? String
            ?? "http://127.0.0.1:8000/"
        guard let serverURL = URL(string: serverURLString) else {
            NSApp.terminate(nil)
            return
        }

        let controller = StockAIViewController(serverURL: serverURL)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1500, height: 940),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "股市 AI 系統"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isOpaque = true
        window.backgroundColor = .black
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 680, height: 560)
        window.contentViewController = controller
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        AdaptiveApplicationIcon.apply()
        self.window = window
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

private let application = NSApplication.shared
private let delegate = AppDelegate()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
