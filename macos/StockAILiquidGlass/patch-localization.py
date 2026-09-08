#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return source.replace(old, new, 1)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch-localization.py SOURCE.swift OUTPUT.swift")

    source_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    source = source_path.read_text(encoding="utf-8")

    source = replace_once(
        source,
        """    private var accountButtonGlass: NSGlassEffectView?\n    private var appliedTheme: String?\n""",
        """    private var accountButtonGlass: NSGlassEffectView?\n    private var appliedTheme: String?\n    private var toolbarEyebrowText = \"行情資料 · 策略研究 · 風控執行 · 投資工作區\"\n    private var toolbarPlaceholderText = \"交給 AI Agent：查行情、分析風險或操作專案…\"\n    private var toolbarSendText = \"傳送\"\n    private var toolbarSearchAccessibilityText = \"與 AI Agent 對話\"\n    private var toolbarAccountAccessibilityText = \"Codex 帳號\"\n""",
        "toolbar localization state",
    )

    source = replace_once(
        source,
        """    func applyInterfaceTheme(_ theme: String) {\n""",
        """    func applyInterfaceStrings(\n        eyebrow: String,\n        placeholder: String,\n        sendLabel: String,\n        searchAccessibility: String,\n        accountAccessibility: String,\n        theme: String\n    ) {\n        toolbarEyebrowText = eyebrow\n        toolbarPlaceholderText = placeholder\n        toolbarSendText = sendLabel\n        toolbarSearchAccessibilityText = searchAccessibility\n        toolbarAccountAccessibilityText = accountAccessibility\n        eyebrowLabel.stringValue = toolbarEyebrowText\n        searchField.setAccessibilityLabel(toolbarSearchAccessibilityText)\n        searchButton.setAccessibilityLabel(toolbarSendText)\n        accountButton.setAccessibilityLabel(toolbarAccountAccessibilityText)\n        appliedTheme = nil\n        applyInterfaceTheme(theme)\n    }\n\n    func applyInterfaceTheme(_ theme: String) {\n""",
        "toolbar localization method",
    )

    source = replace_once(
        source,
        """        searchField.placeholderAttributedString = NSAttributedString(\n            string: \"交給 AI Agent：查行情、分析風險或操作專案…\",\n""",
        """        searchField.placeholderAttributedString = NSAttributedString(\n            string: toolbarPlaceholderText,\n""",
        "localized native placeholder",
    )

    source = replace_once(
        source,
        """        searchButton.attributedTitle = NSAttributedString(\n            string: \"傳送\",\n""",
        """        searchButton.attributedTitle = NSAttributedString(\n            string: toolbarSendText,\n""",
        "localized native send button",
    )

    source = replace_once(
        source,
        """        toolbarContent?.titleLabel.stringValue = (body[\"title\"] as? String) ?? \"股市 AI 系統\"\n        toolbarContent?.applyOperatorLabel((body[\"operatorLabel\"] as? String) ?? \"Codex\")\n        let theme = (body[\"theme\"] as? String) ?? \"exchange\"\n        toolbarContent?.applyInterfaceTheme(theme)\n        updateChromeGlassTheme(theme: theme)\n""",
        """        toolbarContent?.titleLabel.stringValue = (body[\"title\"] as? String) ?? \"股市 AI 系統\"\n        toolbarContent?.applyOperatorLabel((body[\"operatorLabel\"] as? String) ?? \"Codex\")\n        let theme = (body[\"theme\"] as? String) ?? \"exchange\"\n        toolbarContent?.applyInterfaceStrings(\n            eyebrow: (body[\"eyebrow\"] as? String) ?? \"行情資料 · 策略研究 · 風控執行 · 投資工作區\",\n            placeholder: (body[\"searchPlaceholder\"] as? String) ?? \"交給 AI Agent：查行情、分析風險或操作專案…\",\n            sendLabel: (body[\"searchButton\"] as? String) ?? \"傳送\",\n            searchAccessibility: (body[\"searchAccessibility\"] as? String) ?? \"與 AI Agent 對話\",\n            accountAccessibility: (body[\"accountAccessibility\"] as? String) ?? \"Codex 帳號\",\n            theme: theme\n        )\n        updateChromeGlassTheme(theme: theme)\n""",
        "native message localization",
    )

    source = replace_once(
        source,
        """        window.webkit.messageHandlers.glassLayout.postMessage({\n          viewport:{width:window.innerWidth,height:window.innerHeight},\n          sidebar:box(document.querySelector('.sidebar')),\n          toolbar:box(topbar),\n          title:document.getElementById('viewTitle')?.textContent?.trim() || '股市 AI 系統',\n          operatorLabel:document.getElementById('accountLabel')?.textContent?.trim() || 'Codex',\n          chartFocus:document.documentElement.dataset.chartFocus === 'true',\n          theme\n        });\n""",
        """        const prompt = document.getElementById('globalAgentPrompt');\n        const sendButton = document.getElementById('globalAgentSend');\n        const accountButton = document.getElementById('accountMenuBtn');\n        window.webkit.messageHandlers.glassLayout.postMessage({\n          viewport:{width:window.innerWidth,height:window.innerHeight},\n          sidebar:box(document.querySelector('.sidebar')),\n          toolbar:box(topbar),\n          title:document.getElementById('viewTitle')?.textContent?.trim() || 'Stock AI System',\n          operatorLabel:document.getElementById('accountLabel')?.textContent?.trim() || 'Codex',\n          chartFocus:document.documentElement.dataset.chartFocus === 'true',\n          theme,\n          language:document.documentElement.lang || 'zh-Hant',\n          eyebrow:document.querySelector('.eyebrow')?.textContent?.trim() || '',\n          searchPlaceholder:prompt?.getAttribute('placeholder') || '',\n          searchButton:sendButton?.textContent?.trim() || '',\n          searchAccessibility:prompt?.getAttribute('aria-label') || '',\n          accountAccessibility:accountButton?.getAttribute('title') || ''\n        });\n""",
        "native bridge localized payload",
    )

    source = replace_once(
        source,
        """      if (title) new MutationObserver(scheduleLayout).observe(title,{subtree:true,childList:true,characterData:true});\n      const operatorLabel = document.getElementById('accountLabel');\n      if (operatorLabel) new MutationObserver(scheduleLayout).observe(operatorLabel,{subtree:true,childList:true,characterData:true});\n      new MutationObserver(scheduleLayout).observe(document.documentElement,{attributes:true,attributeFilter:['data-ui-theme','data-chart-focus']});\n""",
        """      if (title) new MutationObserver(scheduleLayout).observe(title,{subtree:true,childList:true,characterData:true});\n      const chromeTextObserver = new MutationObserver(scheduleLayout);\n      [\n        document.querySelector('.eyebrow'),\n        document.getElementById('globalAgentPrompt'),\n        document.getElementById('globalAgentSend'),\n        document.getElementById('accountMenuBtn'),\n        document.getElementById('accountLabel')\n      ].filter(Boolean).forEach((element) => chromeTextObserver.observe(element,{\n        subtree:true,childList:true,characterData:true,attributes:true,\n        attributeFilter:['placeholder','aria-label','title']\n      }));\n      new MutationObserver(scheduleLayout).observe(document.documentElement,{attributes:true,attributeFilter:['data-ui-theme','data-chart-focus','lang']});\n""",
        "native bridge language observer",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
