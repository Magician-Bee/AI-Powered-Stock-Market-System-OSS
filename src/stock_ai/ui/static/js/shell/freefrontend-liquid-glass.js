/*
 * Thin production adapter for the complete FreeFrontend/CodePen examples.
 * Optical definitions and component values are not regenerated here.
 */
const FREEFRONTEND_SURFACE_SELECTOR = [
  '.sidebar',
  '.topbar',
  '.auth-surface',
  '.side-card',
  '.panel',
  '.metric',
  '.mini-card',
  '.event',
  '.row',
  '.process-step',
  '.answer',
  '.json-box',
  '.linkage-result',
  '.rt-card',
  '.rt-book',
  '.decision-lane',
  '.decision-row',
  '.home-action-brief',
  '.settings-integration-card',
  '.broker-connection-card',
  '.broker-safety-banner',
  '.settings-status-row',
  '.settings-overview-strip',
  '.theme-choice',
  '.backdrop-choice',
  '.agent-task-pane',
  '.agent-activity-pane',
  '.agent-activity-item',
  '.agent-runtime-answer',
  '.global-agent-popover',
  '.codex-composer',
  '.codex-answer',
  '.technical-details',
  '.paper-broker-card',
  '.paper-account-card',
  '.paper-order-card',
].join(',');

const FREEFRONTEND_COMPACT_SELECTOR = [
  '.metric',
  '.mini-card',
  '.event',
  '.row',
  '.process-step',
  '.decision-row',
  '.settings-status-row',
  '.agent-activity-item',
].join(',');

function isFreeFrontendPreserved(element) {
  return Boolean(element?.closest?.('[data-liquid-preserve]'));
}

function ensureFreeFrontendFilterDefs() {
  if (document.getElementById('freeFrontendLiquidFilterDefs')) return;
  const host = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  host.id = 'freeFrontendLiquidFilterDefs';
  host.setAttribute('width', '0');
  host.setAttribute('height', '0');
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'position:fixed;inset:0;pointer-events:none;visibility:hidden';
  host.innerHTML = `
    <defs>
      <filter id="glass-distortion" x="0%" y="0%" width="100%" height="100%" filterUnits="objectBoundingBox">
        <feTurbulence type="fractalNoise" baseFrequency="0.01 0.01" numOctaves="1" seed="5" result="turbulence"/>
        <feComponentTransfer in="turbulence" result="mapped">
          <feFuncR type="gamma" amplitude="1" exponent="10" offset="0.5"/>
          <feFuncG type="gamma" amplitude="0" exponent="1" offset="0"/>
          <feFuncB type="gamma" amplitude="0" exponent="1" offset="0.5"/>
        </feComponentTransfer>
        <feGaussianBlur in="turbulence" stdDeviation="3" result="softMap"/>
        <feSpecularLighting in="softMap" surfaceScale="5" specularConstant="1" specularExponent="100" lighting-color="white" result="specLight">
          <fePointLight x="-200" y="-200" z="300"/>
        </feSpecularLighting>
        <feComposite in="specLight" operator="arithmetic" k1="0" k2="1" k3="1" k4="0" result="litImage"/>
        <feDisplacementMap in="SourceGraphic" in2="softMap" scale="150" xChannelSelector="R" yChannelSelector="G"/>
      </filter>
      <filter id="container-glass" x="0%" y="0%" width="100%" height="100%">
        <feTurbulence type="fractalNoise" baseFrequency="0.008 0.008" numOctaves="2" seed="92" result="noise"/>
        <feGaussianBlur in="noise" stdDeviation="0.02" result="blur"/>
        <feDisplacementMap in="SourceGraphic" in2="blur" scale="77" xChannelSelector="R" yChannelSelector="G"/>
      </filter>
      <filter id="mini-liquid-lens" x="-50%" y="-50%" width="200%" height="200%">
        <feImage x="0" y="0" result="normalMap" href="data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='300'%3E%3CradialGradient id='invmap' cx='50%25' cy='50%25' r='75%25'%3E%3Cstop offset='0%25' stop-color='rgb(128,128,255)'/%3E%3Cstop offset='90%25' stop-color='rgb(255,255,255)'/%3E%3C/radialGradient%3E%3Crect width='100%25' height='100%25' fill='url(%23invmap)'/%3E%3C/svg%3E"/>
        <feDisplacementMap in="SourceGraphic" in2="normalMap" scale="-252" xChannelSelector="R" yChannelSelector="G" result="displaced"/>
        <feMerge><feMergeNode in="displaced"/></feMerge>
      </filter>
    </defs>`;
  document.body.appendChild(host);
}

async function ensureDaftPlugButtonFilter() {
  if (document.getElementById('btn-glass')) return;
  try {
    const response = await fetch('/static/vendor/freefrontend-liquid-glass/pens/QwbaYGO-fullpage.html');
    if (!response.ok) return;
    const fullPage = new DOMParser().parseFromString(await response.text(), 'text/html');
    const source = fullPage.querySelector('iframe#result')?.getAttribute('srcdoc');
    if (!source) return;
    const pen = new DOMParser().parseFromString(source, 'text/html');
    const originalFilter = pen.getElementById('btn-glass');
    const defs = document.querySelector('#freeFrontendLiquidFilterDefs defs');
    if (originalFilter && defs) defs.appendChild(document.importNode(originalFilter, true));
  } catch (_error) {
    document.documentElement.dataset.ffButtonFilter = 'container-fallback';
  }
}

async function ensureFooonticSwitcherFilter() {
  if (document.getElementById('stock-ai-nav-switcher')) return;
  try {
    const response = await fetch('/static/vendor/freefrontend-liquid-glass/pens/KwpRaGr-fullpage.html');
    if (!response.ok) return;
    const fullPage = new DOMParser().parseFromString(await response.text(), 'text/html');
    const source = fullPage.querySelector('iframe#result')?.getAttribute('srcdoc');
    if (!source) return;
    const pen = new DOMParser().parseFromString(source, 'text/html');
    const originalFilter = pen.getElementById('switcher');
    const defs = document.querySelector('#freeFrontendLiquidFilterDefs defs');
    if (!originalFilter || !defs) return;
    const importedFilter = document.importNode(originalFilter, true);
    importedFilter.id = 'stock-ai-nav-switcher';
    importedFilter.dataset.sourcePen = 'KwpRaGr';
    defs.appendChild(importedFilter);
    document.documentElement.dataset.navSwitcherFilter = 'KwpRaGr-original';
  } catch (_error) {
    document.documentElement.dataset.navSwitcherFilter = 'css-fallback';
  }
}

function initFooonticNavigationSwitcher() {
  window.__fooonticNavigationController?.abort?.();
  const nav = document.querySelector('.sidebar .nav-menu.stock-nav-switcher');
  if (!nav) return null;

  const controller = new AbortController();
  const { signal } = controller;

  const buttons = [...nav.querySelectorAll('.nav-btn[data-view]')];
  buttons.forEach((button, index) => {
    let radio = button.querySelector(':scope > .switcher__input');
    if (!radio) {
      radio = document.createElement('input');
      radio.className = 'switcher__input';
      radio.type = 'radio';
      radio.name = 'stock-ai-navigation';
      button.prepend(radio);
    }
    radio.value = button.dataset.view;
    radio.setAttribute('c-option', String(index + 1));
    radio.setAttribute('aria-hidden', 'true');
    radio.tabIndex = -1;
    radio.checked = button.matches('.active,[data-selected="true"],[aria-current="page"]');
  });

  /*
   * Source: fooontic KwpRaGr. Keep its state machine intact: CSS receives both
   * the checked c-option and the previous c-option, instead of coordinates
   * calculated from the rendered buttons.
   */
  const trackPrevious = (el) => {
    const radios = el.querySelectorAll('input[type="radio"]');
    let previousValue = null;

    const initiallyChecked = el.querySelector('input[type="radio"]:checked');
    if (initiallyChecked) {
      previousValue = initiallyChecked.getAttribute("c-option");
      el.setAttribute('c-previous', previousValue);
    }

    radios.forEach(radio => {
      radio.addEventListener('change', () => {
        if (radio.checked) {
          el.setAttribute('c-previous', previousValue ?? '');
          previousValue = radio.getAttribute("c-option");
        }
      });
    });
  };

  trackPrevious(nav);

  const selectButton = (button) => {
    const radio = button?.querySelector(':scope > .switcher__input');
    if (!radio || radio.checked) return;
    radio.checked = true;
    radio.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const sync = () => {
    const active = nav.querySelector('.nav-btn.active,[data-selected="true"],[aria-current="page"]')
      || nav.querySelector('.nav-btn');
    selectButton(active);
  };

  nav.addEventListener('click', (event) => {
    selectButton(event.target.closest('.nav-btn[data-view]'));
  }, { signal });

  const observer = new MutationObserver(sync);
  observer.observe(nav, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['class', 'data-selected', 'aria-current'],
  });
  const resizeObserver = window.ResizeObserver ? new ResizeObserver(sync) : null;
  resizeObserver?.observe(nav);
  sync();

  const abort = () => {
    observer.disconnect();
    resizeObserver?.disconnect();
    controller.abort();
  };
  window.addEventListener('pagehide', abort, { once: true, signal });
  window.__fooonticNavigationController = { abort, sync };
  document.documentElement.dataset.navigationGlass = 'KwpRaGr-radio-state-machine';
  return window.__fooonticNavigationController;
}

function addLiquidGlassLayers(element) {
  if (
    element.dataset.ffLiquidGlass
    || isFreeFrontendPreserved(element)
    || element.parentElement?.closest?.('[data-ff-liquid-glass]')
  ) return;
  const effect = document.createElement('div');
  effect.className = 'liquidGlass-effect';
  effect.setAttribute('aria-hidden', 'true');
  const tint = document.createElement('div');
  tint.className = 'liquidGlass-tint';
  tint.setAttribute('aria-hidden', 'true');
  const shine = document.createElement('div');
  shine.className = 'liquidGlass-shine';
  shine.setAttribute('aria-hidden', 'true');
  element.prepend(effect, tint, shine);
  element.classList.add('liquidGlass-wrapper');
  if (element.matches('.sidebar,.topbar')) {
    element.dataset.ffLiquidGlass = 'navigation';
  } else if (element.matches(FREEFRONTEND_COMPACT_SELECTOR)) {
    element.dataset.ffLiquidGlass = 'compact';
  } else {
    element.dataset.ffLiquidGlass = 'surface';
  }
}

function applyFreeFrontendComponents(root = document) {
  const surfaces = [];
  if (root instanceof Element && root.matches(FREEFRONTEND_SURFACE_SELECTOR)) surfaces.push(root);
  root.querySelectorAll?.(FREEFRONTEND_SURFACE_SELECTOR).forEach(element => surfaces.push(element));
  surfaces.forEach(addLiquidGlassLayers);

  const buttons = [];
  if (root instanceof Element && root.matches('button')) buttons.push(root);
  root.querySelectorAll?.('button').forEach(element => buttons.push(element));
  buttons.forEach((button) => {
    if (button.classList.contains('nav-btn')) {
      button.classList.remove('ff-glass-button');
    } else if (!isFreeFrontendPreserved(button)) {
      button.classList.add('ff-glass-button');
    }
  });
}

function initMaxuiuxFontSlider() {
  ['uiFontScale', 'uiGlassTransparency'].forEach((id) => {
    const input = document.getElementById(id);
    if (!input || input.closest('.ff-slider-shell')) return;
    const shell = document.createElement('span');
    shell.className = 'ff-slider-shell';
    shell.dataset.sourcePen = 'VYLQJoy';
    const slider = document.createElement('span');
    slider.className = 'slider-container';
    slider.innerHTML = `
      <span class="slider-progress"></span>
      <span class="slider-thumb-glass">
        <span class="slider-thumb-glass-filter"></span>
        <span class="slider-thumb-glass-overlay"></span>
        <span class="slider-thumb-glass-specular"></span>
      </span>`;
    input.before(shell);
    shell.append(slider, input);
    const progress = slider.querySelector('.slider-progress');
    const thumb = slider.querySelector('.slider-thumb-glass');
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const min = Number(input.min || 0);
        const max = Number(input.max || 100);
        const percent = Math.max(0, Math.min(100, ((Number(input.value) - min) / (max - min || 1)) * 100));
        progress.style.width = `${percent}%`;
        thumb.style.left = `${percent}%`;
      });
    };
    input.addEventListener('input', update, { passive: true });
    input.addEventListener('change', update);
    input.addEventListener('pointerdown', () => thumb.classList.add('active'));
    input.addEventListener('pointerup', () => thumb.classList.remove('active'));
    input.addEventListener('pointercancel', () => thumb.classList.remove('active'));
    input.addEventListener('blur', () => thumb.classList.remove('active'));
    update();
  });
}

function initFreeFrontendLiquidGlass() {
  document.documentElement.dataset.freeFrontendLiquidGlass = 'exact-source-v2';
  document.documentElement.dataset.freeFrontendPens = 'vEOpqMa,QwbaYGO,VYLQJoy,KwpRaGr,jEPxMgW';
  ensureFreeFrontendFilterDefs();
  ensureDaftPlugButtonFilter();
  ensureFooonticSwitcherFilter();
  applyFreeFrontendComponents();
  initMaxuiuxFontSlider();
  initFooonticNavigationSwitcher();

  const target = document.querySelector('body > .app-shell') || document.body;
  if (typeof window.MutationObserver !== 'function') return;
  const observer = new MutationObserver((records) => {
    records.forEach(record => record.addedNodes.forEach((node) => {
      if (node instanceof Element && !node.matches('.liquidGlass-effect,.liquidGlass-tint,.liquidGlass-shine')) {
        applyFreeFrontendComponents(node);
      }
    }));
  });
  observer.observe(target, { childList: true, subtree: true });
  window.addEventListener('pagehide', () => observer.disconnect(), { once: true });
  window.__freeFrontendLiquidObserver = observer;
}
