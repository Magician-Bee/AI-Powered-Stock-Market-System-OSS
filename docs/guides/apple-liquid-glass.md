# Apple Liquid Glass 實作依據

本專案不把第三方 WebGL、CSS `backdrop-filter` 或相似材質宣稱為 Apple Liquid Glass。Apple 公開的真正 Liquid Glass 是原生框架 API：

- AppKit：`NSGlassEffectView`、`NSGlassEffectContainerView`
- SwiftUI：`Glass`、`glassEffect(_:in:)`、`GlassEffectContainer`
- UIKit：`UIGlassEffect`、`UIGlassContainerEffect`

macOS 原生殼層位於 `macos/StockAILiquidGlass/main.swift`，並以 macOS 26 SDK 的公開型別 `NSGlassEffectView` 與 `NSGlassEffectContainerView` 直接編譯。程式只使用 Apple 公開的 `NSGlassEffectView.Style.clear`、`contentView`、`cornerRadius`、`tintColor` 與 `spacing` 屬性，不透過 KVC、私有 selector 或自行推測的 style／variant 數字。側欄活動項目是一顆持續存在、沒有 tint 的 `NSGlassEffectView`；切換功能只用 AppKit 動畫移動同一顆玻璃的 `frame`。它不放進融合容器、不建立來源與目的地兩顆玻璃，也不做 matched-geometry 形狀合併，因此長距離切換不會再收腰成蝴蝶結。

原本網頁的側欄、按鈕和響應式排版仍是 UI 的排列依據。原生程式不會建立第二套導覽結構，而是透過 `ResizeObserver` 取得原版 `.sidebar`、`.topbar` 與活動 `.nav-btn` 的實際位置，讓原生玻璃跟著原版 UI 移動與縮放。活動按鈕的網頁底色和文字會停用，代號與標題放在同一顆 AppKit 原生玻璃的 `contentView`，因此不會再出現白色網頁底與原生玻璃互相疊加。上方功能列的標題同樣放在主 `NSGlassEffectView.contentView` 裡；搜尋輸入槽與按鈕均使用公開的 `.clear` 系統玻璃，不疊加自製材質漸層。`1060px` 與 `760px` 的原版斷點因此仍然有效。

設定頁是系統連線的統一入口：Codex 帳號、專案同步、Skills、MCP 與資料／通知 API 都集中顯示，前端只取得是否設定與連線狀態，不會取得 API key 或 Token 內容。使用者不再直接調整玻璃渲染品質、濃度或折射參數；介面改提供七套深色／淺色風格，以及可獨立搭配的市場星圖、精密網格、流動波紋、速度光帶、行情方塊與純色背景。

內容區的獨立卡片、表格、摘要與事件清單之間保留 `18px` 區塊間距；條件選股在 `760px` 以下改成帶欄位名稱的單欄卡片，首頁完整行動清單也維持單欄，避免卡片黏合、文字被壓成直排或表格超出視窗。凡是使用藍、綠、黃、紅或紫色邊框的卡片，表面也會套用同色系的淡玻璃漸層、內高光與下層陰影，不再只有邊框有顏色。

視窗本體、根視圖與內容底層保持不透明黑色；只有側欄和上方功能列使用原生玻璃。這可避免桌面或其他視窗從內容區透出。

AppKit 玻璃使用 Swift 6.2 與 macOS 26 SDK 的公開 API 編譯；JavaScript 橋接只負責回報原版網頁元件的公開座標，不參與玻璃材質渲染。編譯結果是 arm64 與 x86_64 的 Universal 2 App，並隨專案封裝；一般使用者不必安裝 Xcode。需要自行重建原生 App 的開發者，必須提供 Swift 6.2 與 macOS 26 SDK，亦可用 `STOCK_AI_SWIFTC`、`STOCK_AI_MACOS_SDK` 指定位置。

## 官方資料

- [Apple：Meet Liquid Glass](https://developer.apple.com/videos/play/wwdc2025/219/)
- [Apple：Applying Liquid Glass to custom views](https://developer.apple.com/documentation/swiftui/applying-liquid-glass-to-custom-views)
- [Apple：GlassEffectContainer](https://developer.apple.com/documentation/swiftui/glasseffectcontainer)
- [Apple：glassEffectID](https://developer.apple.com/documentation/swiftui/view/glasseffectid%28_%3Ain%3A%29)
- [Apple：matchedGeometry](https://developer.apple.com/documentation/swiftui/glasseffecttransition/matchedgeometry)
- [Apple：Adopting Liquid Glass](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass)
- [Apple：NSGlassEffectView](https://developer.apple.com/documentation/appkit/nsglasseffectview)
- [Apple：NSGlassEffectContainerView](https://developer.apple.com/documentation/appkit/nsglasseffectcontainerview/contentview)
- [Apple Design Resources](https://developer.apple.com/design/resources/)
- [WebKit：Safari 26 web-platform features](https://webkit.org/blog/17333/webkit-features-in-safari-26-0/)

Apple Design Resources 提供 Figma／Sketch UI Kits 與 Icon Composer 等設計資源；它們是設計與資產工具，不是可在一般網頁直接呼叫的 Liquid Glass 渲染 API。Apple 公開的 Safari 26 Web 平台功能也沒有提供 `NSGlassEffectView` 的 CSS 或 JavaScript 等價 API。因此，本專案在 Mac App 內以 AppKit 原生玻璃覆蓋網頁的導覽與控制層，停用重複的 WebGL 模擬鏡片，但保留網頁原本的內容、排列和互動。
