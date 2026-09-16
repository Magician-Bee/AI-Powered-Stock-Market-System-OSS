function applyOpenSourceGlassClasses() {
  const uiRoot = document.querySelector('body > .app-shell') || document;
  const contentSelector = '.panel,.metric,.event,.mini-card,.rt-card,.rt-book,.row,.process-step,.answer,.json-box,.linkage-result';
  uiRoot.querySelectorAll(contentSelector).forEach((el) => {
    el.classList.remove('glin-glass-card', 'liquid-surface');
    el.classList.add('content-surface');
  });
  uiRoot.querySelectorAll('.sidebar,.topbar').forEach((el) => {
    el.classList.add('glass-navigation', 'glass-group');
    el.dataset.glassComponent = 'GlassNavigation';
  });
  uiRoot.querySelectorAll('.searchbar,.side-card').forEach((el) => {
    el.classList.add('glass-surface');
    el.dataset.glassComponent = 'GlassSurface';
  });
  uiRoot.querySelectorAll('.searchbar,.nav-menu,.design-strip').forEach((el) => {
    el.classList.add('glass-group');
    el.dataset.glassGroup = 'true';
  });
  uiRoot.querySelectorAll('button:not(.nav-btn)').forEach((el) => {
    el.classList.remove('glin-liquid-button', 'liquid-surface');
    el.classList.add('glass-button');
    el.dataset.glassComponent = 'GlassButton';
  });
  uiRoot.querySelectorAll('.nav-btn').forEach((el) => {
    el.classList.remove('glin-liquid-button', 'liquid-surface', 'glass-button', 'glass-refract-control');
    el.classList.add('stock-nav-option');
    el.dataset.glassComponent = 'NavigationOption';
    delete el.dataset.glassRefraction;
    el.querySelectorAll(':scope > .glass-control-lens').forEach((lens) => lens.remove());
  });
  const navMenu = uiRoot.querySelector('.sidebar .nav-menu');
  if (navMenu) {
    navMenu.classList.add('stock-nav-switcher');
    navMenu.dataset.sourcePen = 'KwpRaGr';
    navMenu.querySelectorAll(':scope > .nav-active-lens').forEach((lens) => lens.remove());
  }
  uiRoot.querySelectorAll('.searchbar,.topbar button').forEach((el) => {
    el.classList.add('glass-refract-control');
    el.dataset.glassRefraction = 'webgl';
    if (!el.querySelector(':scope > .glass-control-lens')) {
      const lens = document.createElement('span');
      lens.className = 'glass-control-lens';
      lens.setAttribute('aria-hidden', 'true');
      el.prepend(lens);
    }
  });
  uiRoot.querySelectorAll('input,textarea,select').forEach((el) => {
    el.classList.remove('liquid-surface');
    el.classList.add('glass-control');
  });
  document.documentElement.dataset.glinuiGlass = 'token-bridge';
  document.documentElement.dataset.glassSystem = 'liquid-glass-ui-v1';
}

function ensureOpticalBackdropPattern() {
  const field = document.querySelector('[data-market-tile-field]');
  if (!field || field.children.length) return;
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < 144; index += 1) {
    const tile = document.createElement('span');
    tile.className = 'market-tile';
    fragment.appendChild(tile);
  }
  field.appendChild(fragment);
}

function initLiveGlassSampling() {
  window.__glassSamplerController?.abort();
  const layer = document.getElementById('glassSampleLayer');
  const backdropSource = document.querySelector('[data-glass-optical-background]');
  const appSource = document.querySelector('body > .app-shell');
  if (!layer || !backdropSource || !appSource) return null;

  const controller = new AbortController();
  const { signal } = controller;
  let captureTimer = 0;
  let scrollFrame = 0;
  let scrollIdleTimer = 0;
  let scrollFrameCount = 0;
  let scrolling = false;
  let captureRunning = false;
  let capturePending = false;
  let revision = 0;

  const sanitizeClone = (source, clone) => {
    const sourceCanvases = [...source.querySelectorAll('canvas')];
    const cloneCanvases = [...clone.querySelectorAll('canvas')];
    cloneCanvases.forEach((cloneCanvas, index) => {
      const sourceCanvas = sourceCanvases[index];
      if (!sourceCanvas || !sourceCanvas.width || !sourceCanvas.height) {
        cloneCanvas.remove();
        return;
      }
      try {
        const image = document.createElement('img');
        image.src = sourceCanvas.toDataURL('image/png');
        image.className = cloneCanvas.className;
        image.alt = '';
        image.setAttribute('data-liquid-canvas-snapshot', sourceCanvas.id || `canvas-${index}`);
        image.style.width = `${sourceCanvas.clientWidth || sourceCanvas.width}px`;
        image.style.height = `${sourceCanvas.clientHeight || sourceCanvas.height}px`;
        image.style.display = getComputedStyle(sourceCanvas).display;
        cloneCanvas.replaceWith(image);
      } catch (error) {
        cloneCanvas.remove();
      }
    });
    clone.querySelectorAll('[data-liquid-ignore],.glass-control-lens').forEach((element) => element.remove());
    clone.querySelectorAll('[id]').forEach((element) => element.removeAttribute('id'));
    clone.querySelectorAll('[name]').forEach((element) => element.removeAttribute('name'));
    clone.querySelectorAll('[for]').forEach((element) => element.removeAttribute('for'));
    // The optical sampling clone is not application state. Leaving route and
    // action metadata on it creates a second set of workspace roots, so
    // loaders update the invisible sample instead of the visible page.
    clone.querySelectorAll('[data-ui-id],[data-ui-action],[data-ui-scope],[data-ui-version],[data-workspace-parent],[data-workspace-tab],[data-route-content],[data-view]').forEach((element) => {
      ['data-ui-id', 'data-ui-action', 'data-ui-scope', 'data-ui-version', 'data-workspace-parent', 'data-workspace-tab', 'data-route-content', 'data-view']
        .forEach(attribute => element.removeAttribute(attribute));
    });
    clone.querySelectorAll('input,textarea,select,button,a').forEach((element) => {
      element.setAttribute('tabindex', '-1');
      element.removeAttribute('autofocus');
    });
  };

  const rebuild = () => {
    const fullDocumentHeight = Math.max(
      document.documentElement.scrollHeight,
      document.body.scrollHeight,
      window.innerHeight,
    );
    // The lenses only ever sample the visible viewport. Capturing a very
    // long dashboard creates a multi-megapixel texture, blocks interaction
    // and offers no additional visual information to the fixed chrome.
    const documentHeight = Math.min(2400, fullDocumentHeight);
    const sampleDocument = document.createElement('div');
    sampleDocument.className = 'glass-sample-document';
    sampleDocument.setAttribute('data-liquid-sample-source', '');
    sampleDocument.style.width = `${window.innerWidth}px`;
    sampleDocument.style.height = `${documentHeight}px`;

    const backdropClone = backdropSource.cloneNode(true);
    backdropClone.removeAttribute('data-glass-optical-background');
    backdropClone.style.height = `${documentHeight}px`;
    // Keep the WebGL sampling source local and deterministic. html2canvas can
    // otherwise report a failed image load for the decorative wallpaper even
    // though the visible UI already has it rendered by the browser.
    backdropClone.style.backgroundImage = 'radial-gradient(ellipse at 22% 24%, rgba(88,184,255,.22), transparent 48%), radial-gradient(ellipse at 82% 76%, rgba(51,231,205,.16), transparent 52%)';
    const tileField = backdropClone.querySelector('[data-market-tile-field]');
    if (tileField) {
      tileField.style.backgroundImage = 'linear-gradient(132deg, transparent 26%, rgba(154,220,255,.12) 47%, transparent 70%)';
      const requiredTiles = Math.min(1440, Math.ceil(documentHeight / 64) * 12);
      while (tileField.children.length < requiredTiles) {
        const tile = document.createElement('span');
        tile.className = 'market-tile';
        tileField.appendChild(tile);
      }
    }

    const appClone = appSource.cloneNode(true);
    appClone.classList.add('glass-sample-app');
    appClone.setAttribute('data-liquid-sample-clone', '');
    appClone.setAttribute('aria-hidden', 'true');
    appClone.setAttribute('inert', '');
    sanitizeClone(appSource, appClone);
    // html2canvas 1.x cannot parse modern computed `color()` values emitted
    // by CSS color-mix(). The application chrome is itself glass, so sample
    // the optical backdrop rather than recursively rendering the whole UI.
    appClone.setAttribute('data-liquid-ignore', '');

    sampleDocument.append(backdropClone, appClone);
    layer.style.width = `${window.innerWidth}px`;
    layer.style.height = `${documentHeight}px`;
    layer.dataset.sampleWidth = String(window.innerWidth);
    layer.dataset.sampleHeight = String(documentHeight);
    layer.replaceChildren(sampleDocument);
    revision += 1;
    document.documentElement.dataset.glassSampleRevision = String(revision);
    document.documentElement.dataset.glassSampleDocumentHeight = String(documentHeight);
    document.documentElement.dataset.glassSampleView = document.querySelector('body > .app-shell .view.active')?.id || 'none';
  };

  const runCapture = async () => {
    if (scrolling) {
      capturePending = true;
      return;
    }
    if (captureRunning) {
      capturePending = true;
      return;
    }
    captureRunning = true;
    try {
      rebuild();
      const renderer = window.__liquidGLRenderer__;
      if (renderer?.captureSnapshot) {
        await renderer.captureSnapshot();
        const activeView = document.querySelector('body > .app-shell .view.active')?.id;
        const activeCanvas = activeView === 'stock' ? $('priceChart') : null;
        if (activeCanvas && renderer.updateCanvasRegion?.(activeCanvas)) {
          document.documentElement.dataset.glassCanvasSync = 'texture-subimage';
          document.documentElement.dataset.glassCanvasSyncReason = 'capture-complete-chart-patch';
        }
      }
    } finally {
      captureRunning = false;
      if (capturePending) {
        capturePending = false;
        scheduleCapture(320);
      }
    }
  };

  const scheduleCapture = (delay = 220) => {
    if (scrolling) {
      capturePending = true;
      return;
    }
    window.clearTimeout(captureTimer);
    captureTimer = window.setTimeout(runCapture, delay);
  };

  const syncScrollSampling = () => {
    scrolling = true;
    window.clearTimeout(scrollIdleTimer);
    scrollIdleTimer = window.setTimeout(() => {
      scrolling = false;
      if (capturePending) {
        capturePending = false;
        scheduleCapture(40);
      }
    }, 180);
    if (scrollFrame) return;
    scrollFrame = window.requestAnimationFrame(() => {
      scrollFrame = 0;
      scrollFrameCount += 1;
      document.documentElement.dataset.glassSampleScrollY = String(Math.round(window.scrollY));
      document.documentElement.dataset.glassScrollFrame = String(scrollFrameCount);
      window.__liquidGLRenderer__?.render?.();
    });
  };

  const observer = new MutationObserver((records) => {
    const needsCapture = records.some((record) => {
      const target = record.target instanceof Element ? record.target : record.target.parentElement;
      return !target?.closest('#stockEventToggle');
    });
    if (needsCapture) scheduleCapture();
  });
  observer.observe(appSource, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['class', 'value', 'aria-expanded', 'aria-selected'],
  });
  observer.observe(backdropSource, { childList: true, subtree: true, attributes: true });
  window.addEventListener('scroll', syncScrollSampling, { passive: true, signal });
  window.addEventListener('resize', () => scheduleCapture(), { passive: true, signal });
  syncScrollSampling();

  const abort = () => {
    observer.disconnect();
    window.clearTimeout(captureTimer);
    window.clearTimeout(scrollIdleTimer);
    if (scrollFrame) window.cancelAnimationFrame(scrollFrame);
    controller.abort();
  };
  window.addEventListener('pagehide', abort, { once: true, signal });
  window.__glassSamplerController = { abort, rebuild, scheduleCapture, syncScrollSampling };
  rebuild();
  document.documentElement.dataset.glassSampling = 'live-document-uv-scroll';
  return window.__glassSamplerController;
}

function syncCanvasToLiquidTexture(canvas, reason = 'canvas-render') {
  if (!canvas) return;
  window.requestAnimationFrame(() => {
    const activeView = document.querySelector('body > .app-shell .view.active')?.id || 'none';
    const renderer = window.__liquidGLRenderer__;
    const sampler = window.__glassSamplerController;
    const patchReady = document.documentElement.dataset.glassSampleView === activeView
      && renderer?.updateCanvasRegion?.(canvas);
    if (!patchReady) sampler?.scheduleCapture?.(80);
    document.documentElement.dataset.glassCanvasSync = patchReady ? 'texture-subimage' : 'full-capture-scheduled';
    document.documentElement.dataset.glassCanvasSyncReason = reason;
  });
}

function initDynamicGlassInteractions(preferences) {
  window.__glassInteractionController?.abort();
  const controller = new AbortController();
  const { signal } = controller;
  const root = document.documentElement;
  let activeElement = null;
  let activeGroup = null;
  let pressedElement = null;
  let pendingPointer = null;
  let animationFrame = 0;
  root.dataset.glassLifecycle = 'managed';

  const resetElement = (element) => {
    if (!element) return;
    element.style.setProperty('--glass-rotate-x', '0deg');
    element.style.setProperty('--glass-rotate-y', '0deg');
    element.style.setProperty('--glass-shadow-x', '0px');
    element.style.setProperty('--glass-shadow-y', element.classList.contains('glass-surface') ? '12px' : '8px');
    element.dataset.glassPointerActive = 'false';
  };

  const setGroupActive = (group) => {
    if (activeGroup === group) return;
    if (activeGroup) activeGroup.dataset.glassGroupActive = 'false';
    activeGroup = group;
    if (activeGroup) activeGroup.dataset.glassGroupActive = 'true';
  };

  const applyPointerFrame = () => {
    animationFrame = 0;
    if (!activeElement || !pendingPointer || document.hidden) return;
    const rect = activeElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const normalizedX = Math.max(-1, Math.min(1, ((pendingPointer.x - rect.left) / rect.width) * 2 - 1));
    const normalizedY = Math.max(-1, Math.min(1, ((pendingPointer.y - rect.top) / rect.height) * 2 - 1));
    const maxTilt = activeElement.classList.contains('glass-button') ? 1.15 : 0;
    activeElement.style.setProperty('--glass-rotate-x', `${(-normalizedY * maxTilt).toFixed(2)}deg`);
    activeElement.style.setProperty('--glass-rotate-y', `${(normalizedX * maxTilt).toFixed(2)}deg`);
    activeElement.style.setProperty('--glass-shadow-x', `${(normalizedX * 5).toFixed(2)}px`);
    activeElement.style.setProperty('--glass-shadow-y', `${(10 + normalizedY * 3).toFixed(2)}px`);
    activeElement.dataset.glassPointerActive = 'true';
  };

  if (!preferences.pointerMotion) {
    root.dataset.glassInteractions = 'reduced-motion';
    window.__glassInteractionController = controller;
    return;
  }

  document.addEventListener('pointermove', (event) => {
    if (event.pointerType === 'touch') return;
    const target = event.target.closest?.('.glass-button,.glass-surface');
    if (target !== activeElement) {
      resetElement(activeElement);
      activeElement = target;
      setGroupActive(target?.closest('.glass-group') || null);
    }
    if (!activeElement) return;
    pendingPointer = { x: event.clientX, y: event.clientY };
    if (!animationFrame) animationFrame = requestAnimationFrame(applyPointerFrame);
  }, { passive: true, signal });

  document.addEventListener('pointerout', (event) => {
    if (!activeElement || activeElement.contains(event.relatedTarget)) return;
    const leaving = event.target.closest?.('.glass-button,.glass-surface');
    if (leaving !== activeElement) return;
    resetElement(activeElement);
    activeElement = null;
    pendingPointer = null;
    setGroupActive(null);
  }, { passive: true, signal });

  document.addEventListener('pointerdown', (event) => {
    const target = event.target.closest?.('.glass-button');
    if (!target || target.disabled || target.getAttribute('aria-disabled') === 'true') return;
    pressedElement = target;
    pressedElement.dataset.glassPressed = 'true';
  }, { passive: true, signal });

  const releasePressed = () => {
    if (pressedElement) pressedElement.dataset.glassPressed = 'false';
    pressedElement = null;
  };
  document.addEventListener('pointerup', releasePressed, { passive: true, signal });
  document.addEventListener('pointercancel', releasePressed, { passive: true, signal });
  window.addEventListener('blur', releasePressed, { signal });

  const abort = () => {
    resetElement(activeElement);
    setGroupActive(null);
    releasePressed();
    if (animationFrame) cancelAnimationFrame(animationFrame);
    controller.abort();
  };
  window.addEventListener('pagehide', abort, { once: true, signal });
  window.__glassInteractionController = { abort };
  root.dataset.glassInteractions = 'pointer-elastic';
}

function bindLiquidPointerTracking(instances, preferences) {
  const lenses = (Array.isArray(instances) ? instances : [instances]).filter(Boolean);
  if (!lenses.length) return;
  if (!preferences.pointerMotion) {
    document.documentElement.dataset.liquidPointerTracking = 'reduced-motion';
    return;
  }

  let pointerX = -1;
  let pointerY = -1;
  let animationFrame = 0;
  const controller = new AbortController();
  document.addEventListener('pointermove', (event) => {
    pointerX = event.clientX;
    pointerY = event.clientY;
    lenses.forEach((lens) => {
      const rect = lens.el.getBoundingClientRect();
      const inside = pointerX >= rect.left && pointerX <= rect.right
        && pointerY >= rect.top && pointerY <= rect.bottom;
      if (inside) {
        lens._spotX = pointerX - rect.left;
        lens._spotY = pointerY - rect.top;
      }
      if (lens._pointerInside !== inside) {
        lens._pointerInside = inside;
        lens.el.dataset.liquidPointerActive = inside ? 'true' : 'false';
      }
    });
  }, { passive: true, signal: controller.signal });

  const animate = () => {
    if (!document.hidden) {
      lenses.forEach((lens) => {
        const rect = lens.el.getBoundingClientRect();
        const motionEnabled = document.documentElement.dataset.uiMotion !== 'off';
        const inside = motionEnabled && pointerX >= rect.left && pointerX <= rect.right
          && pointerY >= rect.top && pointerY <= rect.bottom;
        const targetX = inside ? -((pointerY - rect.top) / rect.height * 2 - 1) * 7 : 0;
        const targetY = inside ? ((pointerX - rect.left) / rect.width * 2 - 1) * 7 : 0;
        lens.tiltX += (targetX - lens.tiltX) * 0.14;
        lens.tiltY += (targetY - lens.tiltY) * 0.14;
      });
    }
    animationFrame = requestAnimationFrame(animate);
  };
  animationFrame = requestAnimationFrame(animate);
  window.addEventListener('pagehide', () => {
    controller.abort();
    cancelAnimationFrame(animationFrame);
  }, { once: true });
  document.documentElement.dataset.liquidPointerTracking = 'shader-uniforms';
}

function presentLiquidCanvasInLenses(instances) {
  const lenses = (Array.isArray(instances) ? instances : [instances]).filter(Boolean);
  const renderer = window.__liquidGLRenderer__;
  if (!renderer?.canvas || !lenses.length) return;

  const appShell = document.querySelector('body > .app-shell');
  if (appShell) {
    appShell.style.setProperty('position', 'relative');
    appShell.style.setProperty('z-index', '1');
  }
  renderer.canvas.classList.add('liquid-native-canvas');
  renderer.canvas.setAttribute('aria-hidden', 'true');
  renderer.canvas.style.setProperty('opacity', '1', 'important');
  renderer.canvas.style.setProperty('z-index', '0', 'important');
  renderer.canvas.style.pointerEvents = 'none';
  document.documentElement.dataset.liquidCanvasPresentation = 'background-webgl-canvas';
  document.documentElement.dataset.glinSpotlightPresentation = 'token-bridge';
}

function initOpenSourceGlassUI() {
  const preferences = configureGlassPreferences();
  ensureOpticalBackdropPattern();
  applyOpenSourceGlassClasses();
  initDynamicGlassInteractions(preferences);
  const sampler = initLiveGlassSampling();
  if (!preferences.allowWebGL) {
    document.documentElement.dataset.liquidGl = 'css-fallback';
    document.documentElement.dataset.glassMode = 'css';
    document.documentElement.dataset.glassControlRefraction = 'css-fallback';
    return;
  }

  const liquidTargetSelector = '.liquid-webgl-lens,.glass-control-lens';
  const liquidTargets = document.querySelectorAll(liquidTargetSelector);
  if (window.html2canvas && window.liquidGL && liquidTargets.length) {
    try {
      window.__liquidGLNoWebGL__ = false;
      document.documentElement.dataset.liquidGl = 'webgl-initializing';
      window.__stockLiquidGL = window.liquidGL({
        target: liquidTargetSelector,
        snapshot: '.glass-sample-layer',
        resolution: preferences.quality,
        refraction: 0.031,
        bevelDepth: 0.138,
        bevelWidth: 0.064,
        chromaticAberration: 0.0036,
        frost: 0,
        shadow: true,
        specular: true,
        reveal: 'fade',
        tilt: false,
        magnify: 1.026,
        on: {
          init() {
            document.documentElement.dataset.liquidGl = 'webgl-ready';
            document.documentElement.dataset.glassMode = 'webgl';
            document.documentElement.dataset.glassSampleTexture = 'uploaded';
          },
        },
      });
      bindLiquidPointerTracking(window.__stockLiquidGL, preferences);
      presentLiquidCanvasInLenses(window.__stockLiquidGL);
      const lensCount = (Array.isArray(window.__stockLiquidGL) ? window.__stockLiquidGL : [window.__stockLiquidGL]).filter(Boolean).length;
      document.documentElement.dataset.glassLensCount = String(lensCount);
      document.documentElement.dataset.glassControlRefraction = 'webgl-realtime-uv-sampling';
      sampler?.scheduleCapture();
    } catch (err) {
      console.error('liquidGL init failed', err);
      document.documentElement.dataset.liquidGl = 'failed';
      document.documentElement.dataset.glassMode = 'css';
      document.documentElement.dataset.glassControlRefraction = 'css-fallback';
    }
  } else {
    document.documentElement.dataset.liquidGl = 'missing';
    document.documentElement.dataset.glassMode = 'css';
    document.documentElement.dataset.glassControlRefraction = 'css-fallback';
  }
}
