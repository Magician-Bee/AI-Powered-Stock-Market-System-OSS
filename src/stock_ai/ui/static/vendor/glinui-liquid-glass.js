/*
 * Static-browser port of Glin UI's useLiquidGlass hook.
 * Source repo: external/glinui/packages/ui/src/lib/use-liquid-glass.tsx
 * Keeps the same idea: SVG displacement-map refraction plus CSS blur/saturate.
 */
(function () {
  function supportsBackdropSvgFilter() {
    return /Chrome|Chromium|Edg\//.test(navigator.userAgent || "");
  }

  function buildDisplacementMap(width, height, profile) {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return "";
    const img = ctx.createImageData(width, height);
    const px = img.data;
    const mx = width / 2;
    const my = height / 2;

    for (let row = 0; row < height; row += 1) {
      for (let col = 0; col < width; col += 1) {
        const i = (row * width + col) * 4;
        const nx = (col - mx) / mx;
        const ny = (row - my) / my;
        const dist = profile === "convex"
          ? Math.hypot(nx, ny)
          : (Math.abs(nx) ** 4 + Math.abs(ny) ** 4) ** 0.25;

        if (dist >= 1 || dist < 0.002) {
          px[i] = 128;
          px[i + 1] = 128;
          px[i + 2] = 128;
          px[i + 3] = 255;
          continue;
        }

        let grad = profile === "convex"
          ? dist / Math.max(1 - dist * dist, 1e-4) ** 0.5
          : dist ** 3 / Math.max(1 - dist ** 4, 1e-4) ** 0.75;
        grad *= (1 - dist) ** 0.5;
        grad = Math.min(grad, 1);

        const angle = Math.atan2(ny, nx);
        px[i] = Math.round(Math.max(0, Math.min(255, 128 + Math.cos(angle) * grad * 127)));
        px[i + 1] = Math.round(Math.max(0, Math.min(255, 128 + Math.sin(angle) * grad * 127)));
        px[i + 2] = 128;
        px[i + 3] = 255;
      }
    }

    ctx.putImageData(img, 0, 0);
    return canvas.toDataURL("image/png");
  }

  function attachLiquidGlass(targets, opts) {
    const options = Object.assign({
      displacement: 22,
      blur: 28,
      saturate: 1.8,
      profile: "squircle",
    }, opts || {});
    const elements = Array.from(targets || []).filter(Boolean);
    if (!elements.length) return { active: false, count: 0, mode: "none" };

    if (!supportsBackdropSvgFilter()) {
      elements.forEach((el) => {
        el.dataset.glinuiLiquidGlass = "css-fallback";
        el.style.backdropFilter = `blur(${options.blur}px) saturate(${options.saturate})`;
        el.style.webkitBackdropFilter = `blur(${options.blur}px) saturate(${options.saturate})`;
      });
      return { active: true, count: elements.length, mode: "css-fallback" };
    }

    const defs = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    defs.setAttribute("width", "0");
    defs.setAttribute("height", "0");
    defs.setAttribute("aria-hidden", "true");
    defs.style.cssText = "position:fixed;top:0;left:0;pointer-events:none";
    const defsNode = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.appendChild(defsNode);
    document.body.appendChild(defs);

    const refresh = () => {
      defsNode.replaceChildren();
      elements.forEach((el, index) => {
        const rect = el.getBoundingClientRect();
        if (rect.width < 16 || rect.height < 16) return;
        const filterId = `glinui-liquid-${index}`;
        const map = buildDisplacementMap(
          Math.max(16, Math.round(rect.width * 0.5)),
          Math.max(16, Math.round(rect.height * 0.5)),
          options.profile
        );
        if (!map) return;

        const filter = document.createElementNS("http://www.w3.org/2000/svg", "filter");
        filter.setAttribute("id", filterId);
        filter.setAttribute("x", "0");
        filter.setAttribute("y", "0");
        filter.setAttribute("width", String(Math.round(rect.width)));
        filter.setAttribute("height", String(Math.round(rect.height)));
        filter.setAttribute("filterUnits", "userSpaceOnUse");
        filter.setAttribute("color-interpolation-filters", "sRGB");

        const image = document.createElementNS("http://www.w3.org/2000/svg", "feImage");
        image.setAttribute("href", map);
        image.setAttribute("x", "0");
        image.setAttribute("y", "0");
        image.setAttribute("width", String(Math.round(rect.width)));
        image.setAttribute("height", String(Math.round(rect.height)));
        image.setAttribute("result", "dispMap");
        image.setAttribute("preserveAspectRatio", "none");

        const displacement = document.createElementNS("http://www.w3.org/2000/svg", "feDisplacementMap");
        displacement.setAttribute("in", "SourceGraphic");
        displacement.setAttribute("in2", "dispMap");
        displacement.setAttribute("scale", String(options.displacement));
        displacement.setAttribute("xChannelSelector", "R");
        displacement.setAttribute("yChannelSelector", "G");

        filter.appendChild(image);
        filter.appendChild(displacement);
        defsNode.appendChild(filter);
        el.dataset.glinuiLiquidGlass = "svg-refraction";
        el.style.backdropFilter = `url(#${filterId}) blur(${options.blur}px) saturate(${options.saturate})`;
        el.style.webkitBackdropFilter = `url(#${filterId}) blur(${options.blur}px) saturate(${options.saturate})`;
      });
    };

    refresh();
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(refresh, 120);
    }, { passive: true });

    return { active: true, count: elements.length, mode: "svg-refraction" };
  }

  window.GlinUILiquidGlass = { attach: attachLiquidGlass };
})();
