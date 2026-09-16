(() => {
  const PREFERENCE_KEY = 'stock-ai.chart-workbench.v1';
  const ANNOTATION_KEY = 'stock-ai.chart-annotations.v1';
  const MIN_VISIBLE_BARS = 1;
  const DEFAULT_VISIBLE_BARS = 250;
  const VALID_TYPES = new Set(['candles', 'bars', 'line', 'area', 'heikin']);
  const VALID_TOOLS = new Set(['cursor', 'trend', 'horizontal', 'rectangle', 'text', 'marker']);
  const INDICATOR_KEYS = Object.freeze(['ma5', 'ma10', 'ma20', 'ma60', 'boll', 'volume', 'macd']);
  const DEFAULT_INDICATORS = Object.freeze(Object.fromEntries(INDICATOR_KEYS.map(key => [key, true])));
  const WHEEL_ZOOM_THRESHOLD = 72;

  const readStorage = (key, fallback) => {
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || '');
      return parsed && typeof parsed === 'object' ? parsed : fallback;
    } catch (_) {
      return fallback;
    }
  };

  const writeStorage = (key, value) => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (_) {
      // Private browsing or a full storage quota must not disable the chart.
    }
  };

  const preferences = readStorage(PREFERENCE_KEY, {});
  let chartType = VALID_TYPES.has(preferences.chartType) ? preferences.chartType : 'candles';
  let activeTool = VALID_TOOLS.has(preferences.activeTool) ? preferences.activeTool : 'cursor';
  let selectedRange = preferences.selectedRange === 'all'
    ? 'all'
    : Math.max(MIN_VISIBLE_BARS, Number(preferences.selectedRange) || DEFAULT_VISIBLE_BARS);
  let annotationsByScope = readStorage(ANNOTATION_KEY, {});
  let indicators = {
    ...DEFAULT_INDICATORS,
    ...Object.fromEntries(INDICATOR_KEYS.map(key => [key, preferences.indicators?.[key] !== false])),
  };
  const viewports = new Map();
  const undoByScope = new Map();
  let renderModel = null;
  let hoverGlobalIndex = null;
  let hoverPrice = null;
  let trendDraft = null;
  let panGesture = null;
  let scheduledPanFrame = 0;
  let wheelZoomAccumulator = 0;
  let wheelZoomEndTimer = 0;
  let lastWheelAt = 0;
  const activeTouchPointers = new Map();
  let pinchGesture = null;

  function scopeKey() {
    const rawSymbol = String(state.currentEntity?.symbol || state.symbol || 'unselected').trim().toUpperCase();
    const timeframe = String(state.intradayTimeframe || 'D');
    return `${rawSymbol || 'unselected'}::${timeframe}`;
  }

  function viewportFor(total) {
    const key = scopeKey();
    if (!viewports.has(key)) {
      viewports.set(key, {
        span: selectedRange === 'all' ? total : selectedRange,
        all: selectedRange === 'all',
        offset: 0,
      });
    }
    const viewport = viewports.get(key);
    const safeTotal = Math.max(0, Number(total) || 0);
    viewport.span = viewport.all
      ? safeTotal
      : Math.min(safeTotal, Math.max(Math.min(MIN_VISIBLE_BARS, safeTotal), Number(viewport.span) || DEFAULT_VISIBLE_BARS));
    viewport.offset = Math.min(
      Math.max(0, Number(viewport.offset) || 0),
      Math.max(0, safeTotal - Math.max(1, viewport.span)),
    );
    return viewport;
  }

  function resolveViewport(total) {
    const viewport = viewportFor(total);
    const span = Math.max(1, Math.min(total, viewport.span || total));
    const end = Math.max(span, total - viewport.offset);
    return {
      visibleStart: Math.max(0, end - span),
      visibleEnd: Math.min(total, end),
    };
  }

  function savePreferences() {
    writeStorage(PREFERENCE_KEY, { chartType, activeTool, selectedRange, indicators });
  }

  function currentAnnotations() {
    const annotations = annotationsByScope[scopeKey()];
    return Array.isArray(annotations) ? annotations : [];
  }

  function replaceAnnotations(next, { remember = true } = {}) {
    const key = scopeKey();
    const previous = currentAnnotations().map(item => ({ ...item }));
    if (remember) {
      const stack = undoByScope.get(key) || [];
      stack.push(previous);
      if (stack.length > 30) stack.shift();
      undoByScope.set(key, stack);
    }
    annotationsByScope = { ...annotationsByScope, [key]: next };
    writeStorage(ANNOTATION_KEY, annotationsByScope);
    syncAnnotationCount();
    renderOverlay();
  }

  function annotationLabel() {
    return String(document.getElementById('chartAnnotationLabel')?.value || '').trim().slice(0, 40);
  }

  function setStatus(message, stateName = '') {
    const status = document.getElementById('chartInteractionStatus');
    if (!status) return;
    status.textContent = message;
    if (stateName) status.dataset.state = stateName;
    else delete status.dataset.state;
  }

  function syncAnnotationCount() {
    const count = currentAnnotations().length;
    const node = document.getElementById('chartAnnotationCount');
    if (node) node.textContent = `${count} 個標註`;
    const undo = document.getElementById('chartUndoAnnotation');
    if (undo) undo.disabled = !(undoByScope.get(scopeKey()) || []).length;
    const clear = document.getElementById('chartClearAnnotations');
    if (clear) clear.disabled = count === 0;
  }

  function syncControls() {
    document.querySelectorAll('[data-chart-type]').forEach(button => {
      const active = button.dataset.chartType === chartType;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('button[data-chart-tool]').forEach(button => {
      const active = button.dataset.chartTool === activeTool;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('[data-chart-range]').forEach(button => {
      const value = button.dataset.chartRange === 'all' ? 'all' : Number(button.dataset.chartRange);
      button.classList.toggle('is-active', value === selectedRange);
    });
    document.querySelectorAll('[data-chart-indicator]').forEach(button => {
      const active = indicators[button.dataset.chartIndicator] !== false;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    const surface = document.getElementById('chartInteractionSurface');
    if (surface) surface.dataset.chartTool = activeTool;
    syncAnnotationCount();
  }

  function resizeOverlay() {
    const overlay = document.getElementById('priceChartOverlay');
    if (!overlay || !renderModel) return;
    const dpr = window.devicePixelRatio || 1;
    const { cssW, cssH } = renderModel.bounds;
    overlay.width = Math.floor(cssW * dpr);
    overlay.height = Math.floor(cssH * dpr);
    overlay.style.height = `${cssH}px`;
    const ctx = overlay.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function pointX(globalIndex) {
    if (!renderModel) return null;
    const { visibleStart, visibleEnd, bounds } = renderModel;
    if (globalIndex < visibleStart || globalIndex >= visibleEnd) return null;
    return bounds.left + (globalIndex - visibleStart) * bounds.candleSlot + bounds.candleSlot / 2;
  }

  function priceY(price) {
    if (!renderModel || !Number.isFinite(Number(price))) return null;
    const { priceMin, priceMax, priceBottom, top } = renderModel.bounds;
    return priceBottom - (Number(price) - priceMin) / Math.max(1e-9, priceMax - priceMin) * (priceBottom - top);
  }

  function dateIndex(date) {
    if (!renderModel) return -1;
    const exact = renderModel.points.findIndex(point => String(point.date) === String(date));
    if (exact >= 0) return exact;
    const timestamp = Date.parse(String(date));
    if (!Number.isFinite(timestamp)) return -1;
    let nearest = -1;
    let distance = Infinity;
    renderModel.points.forEach((point, index) => {
      const candidate = Date.parse(String(point.date));
      if (!Number.isFinite(candidate)) return;
      const nextDistance = Math.abs(candidate - timestamp);
      if (nextDistance < distance) {
        nearest = index;
        distance = nextDistance;
      }
    });
    return nearest;
  }

  function accentColor() {
    return getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()
      || CHART_THEME.ma10
      || '#4da3ff';
  }

  function drawLabel(ctx, text, x, y, { align = 'left', color = accentColor() } = {}) {
    const label = String(text || '').trim();
    if (!label) return;
    ctx.save();
    ctx.font = '700 11px Segoe UI';
    const width = Math.min(260, ctx.measureText(label).width + 14);
    const left = align === 'right' ? x - width : x;
    ctx.fillStyle = 'rgba(5,12,22,.82)';
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(left, y - 18, width, 20, 7);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = '#f6f8fb';
    ctx.textAlign = align;
    ctx.textBaseline = 'middle';
    ctx.fillText(label, align === 'right' ? x - 7 : x + 7, y - 8, width - 14);
    ctx.restore();
  }

  function drawAnnotations(ctx) {
    if (!renderModel) return;
    const { left, plotW, top, priceBottom } = renderModel.bounds;
    const accent = accentColor();
    currentAnnotations().forEach(annotation => {
      ctx.save();
      ctx.strokeStyle = annotation.color || accent;
      ctx.fillStyle = annotation.color || accent;
      ctx.lineWidth = 1.8;
      if (annotation.kind === 'horizontal') {
        const y = priceY(annotation.price);
        if (y !== null && y >= top && y <= priceBottom) {
          ctx.setLineDash([7, 5]);
          ctx.beginPath();
          ctx.moveTo(left, y);
          ctx.lineTo(left + plotW, y);
          ctx.stroke();
          drawLabel(ctx, annotation.label || `價位 ${niceNumber(annotation.price)}`, left + plotW - 4, y - 3, { align: 'right' });
        }
      } else if (annotation.kind === 'trend') {
        const startX = pointX(dateIndex(annotation.start?.date));
        const endX = pointX(dateIndex(annotation.end?.date));
        const startY = priceY(annotation.start?.price);
        const endY = priceY(annotation.end?.price);
        if ([startX, endX, startY, endY].every(value => value !== null)) {
          ctx.beginPath();
          ctx.moveTo(startX, startY);
          ctx.lineTo(endX, endY);
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(startX, startY, 4, 0, Math.PI * 2);
          ctx.arc(endX, endY, 4, 0, Math.PI * 2);
          ctx.fill();
          drawLabel(ctx, annotation.label || '趨勢線', Math.min(startX, endX) + 6, Math.min(startY, endY) - 3);
        }
      } else if (annotation.kind === 'rectangle') {
        const startX = pointX(dateIndex(annotation.start?.date));
        const endX = pointX(dateIndex(annotation.end?.date));
        const startY = priceY(annotation.start?.price);
        const endY = priceY(annotation.end?.price);
        if ([startX, endX, startY, endY].every(value => value !== null)) {
          ctx.strokeRect(Math.min(startX, endX), Math.min(startY, endY), Math.abs(endX - startX), Math.abs(endY - startY));
          ctx.globalAlpha = .13;
          ctx.fillRect(Math.min(startX, endX), Math.min(startY, endY), Math.abs(endX - startX), Math.abs(endY - startY));
          drawLabel(ctx, annotation.label || '區間', Math.min(startX, endX) + 5, Math.min(startY, endY) - 3);
        }
      } else if (annotation.kind === 'text') {
        const x = pointX(dateIndex(annotation.date));
        const y = priceY(annotation.price);
        if (x !== null && y !== null) drawLabel(ctx, annotation.label || niceNumber(annotation.price), x + 7, y - 2);
      } else if (annotation.kind === 'marker') {
        const x = pointX(dateIndex(annotation.date));
        const y = priceY(annotation.price);
        if (x !== null && y !== null && y >= top && y <= priceBottom) {
          ctx.beginPath();
          ctx.arc(x, y, 7, 0, Math.PI * 2);
          ctx.fill();
          ctx.strokeStyle = CHART_THEME.text;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(x, y, 10, 0, Math.PI * 2);
          ctx.stroke();
          drawLabel(ctx, annotation.label || niceNumber(annotation.price), x + 12, y - 6);
        }
      }
      ctx.restore();
    });
  }

  function drawTrendDraft(ctx) {
    if (!trendDraft || !renderModel || !['trend', 'rectangle'].includes(activeTool)) return;
    const startX = pointX(trendDraft.globalIndex);
    const startY = priceY(trendDraft.price);
    const endX = pointX(hoverGlobalIndex);
    const endY = priceY(hoverPrice);
    if ([startX, startY, endX, endY].some(value => value === null)) return;
    ctx.save();
    ctx.strokeStyle = accentColor();
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 5]);
    if (activeTool === 'rectangle') ctx.strokeRect(Math.min(startX, endX), Math.min(startY, endY), Math.abs(endX - startX), Math.abs(endY - startY));
    else { ctx.beginPath(); ctx.moveTo(startX, startY); ctx.lineTo(endX, endY); ctx.stroke(); }
    ctx.restore();
  }

  function updateHoverCard(point, pointerPrice) {
    const card = document.getElementById('chartHoverCard');
    if (!card) return;
    if (!point) {
      card.hidden = true;
      card.setAttribute('aria-hidden', 'true');
      card.replaceChildren();
      return;
    }
    const title = document.createElement('strong');
    title.textContent = String(point.date || '未標示時間');
    const values = document.createElement('span');
    values.textContent = `O ${niceNumber(point.open)}　H ${niceNumber(point.high)}　L ${niceNumber(point.low)}　C ${niceNumber(point.close)}　量 ${Number(point.volume || 0).toLocaleString()}`;
    const cursor = document.createElement('span');
    cursor.textContent = `游標價位 ${niceNumber(pointerPrice)}`;
    card.replaceChildren(title, values, cursor);
    card.hidden = false;
    card.setAttribute('aria-hidden', 'false');
  }

  function renderOverlay() {
    const overlay = document.getElementById('priceChartOverlay');
    if (!overlay) return;
    const ctx = overlay.getContext('2d');
    const cssW = renderModel?.bounds.cssW || overlay.clientWidth || 1040;
    const cssH = renderModel?.bounds.cssH || overlay.clientHeight || 620;
    ctx.clearRect(0, 0, cssW, cssH);
    if (!renderModel) {
      updateHoverCard(null);
      return;
    }
    drawAnnotations(ctx);
    drawTrendDraft(ctx);
    if (hoverGlobalIndex === null || hoverGlobalIndex < renderModel.visibleStart || hoverGlobalIndex >= renderModel.visibleEnd) {
      updateHoverCard(null);
      return;
    }
    const point = renderModel.points[hoverGlobalIndex];
    const x = pointX(hoverGlobalIndex);
    const y = priceY(hoverPrice);
    if (!point || x === null || y === null) return;
    const { left, plotW, top, priceBottom, macdBottom } = renderModel.bounds;
    const accent = accentColor();
    ctx.save();
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, macdBottom);
    ctx.moveTo(left, y);
    ctx.lineTo(left + plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = accent;
    ctx.beginPath();
    ctx.arc(x, priceY(point.close), 5, 0, Math.PI * 2);
    ctx.fill();
    drawLabel(ctx, niceNumber(hoverPrice), left + plotW - 4, Math.min(priceBottom, Math.max(top + 20, y)), { align: 'right', color: accent });
    const timeLabel = String(point.date || '');
    drawLabel(ctx, timeLabel, Math.min(left + plotW - 8, Math.max(left + 8, x - 45)), macdBottom + 22, { color: accent });
    ctx.restore();
    updateHoverCard(point, hoverPrice);
  }

  function setRenderModel(model) {
    renderModel = model;
    resizeOverlay();
    if (hoverGlobalIndex !== null && (hoverGlobalIndex < model.visibleStart || hoverGlobalIndex >= model.visibleEnd)) {
      hoverGlobalIndex = null;
      hoverPrice = null;
    }
    syncControls();
    renderOverlay();
  }

  function clearRenderModel() {
    renderModel = null;
    hoverGlobalIndex = null;
    hoverPrice = null;
    trendDraft = null;
    renderOverlay();
  }

  function eventChartPoint(event) {
    const overlay = document.getElementById('priceChartOverlay');
    if (!overlay || !renderModel) return null;
    const rect = overlay.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const { left, plotW, top, priceBottom, candleSlot, priceMin, priceMax } = renderModel.bounds;
    if (x < left || x > left + plotW || y < top || y > priceBottom) return null;
    const visibleIndex = Math.max(
      0,
      Math.min(renderModel.visibleEnd - renderModel.visibleStart - 1, Math.round((x - left - candleSlot / 2) / candleSlot)),
    );
    const globalIndex = renderModel.visibleStart + visibleIndex;
    const price = priceMax - (y - top) / Math.max(1, priceBottom - top) * (priceMax - priceMin);
    return { x, y, globalIndex, price, point: renderModel.points[globalIndex] };
  }

  function updateHover(point) {
    if (!point) {
      hoverGlobalIndex = null;
      hoverPrice = null;
    } else {
      hoverGlobalIndex = point.globalIndex;
      hoverPrice = point.price;
    }
    renderOverlay();
  }

  function schedulePan(nextOffset) {
    if (!renderModel) return;
    const viewport = viewportFor(renderModel.points.length);
    viewport.offset = Math.max(0, Math.min(renderModel.points.length - viewport.span, nextOffset));
    cancelAnimationFrame(scheduledPanFrame);
    scheduledPanFrame = requestAnimationFrame(() => renderCurrentChart());
  }

  function handlePointerMove(event) {
    if (event.pointerType === 'touch') {
      activeTouchPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      // WebKit may end pointer capture while the canvas is redrawn during a
      // pinch. If both fingers are still present, begin a fresh pinch session
      // instead of leaving the chart in a dead gesture state.
      if (!pinchGesture && activeTouchPointers.size >= 2) beginPinchGesture();
      if (pinchGesture) {
        updatePinchGesture();
        event.preventDefault();
        return;
      }
    }
    const point = eventChartPoint(event);
    if (panGesture && renderModel) {
      const delta = event.clientX - panGesture.startX;
      const bars = Math.round(delta / Math.max(1, renderModel.bounds.candleSlot));
      if (Math.abs(delta) > 3) panGesture.moved = true;
      schedulePan(panGesture.startOffset + bars);
    }
    updateHover(point);
  }

  function createAnnotationFromPoint(point) {
    if (!point?.point) return;
    const label = annotationLabel();
    if (activeTool === 'trend' || activeTool === 'rectangle') {
      if (!trendDraft) {
        trendDraft = { globalIndex: point.globalIndex, date: point.point.date, price: point.price };
        setStatus(activeTool === 'rectangle' ? '矩形第一個角已設定；請再點選對角。' : '趨勢線起點已設定；請再點選第二個日期與價位。', 'warning');
        renderOverlay();
        return;
      }
      replaceAnnotations([
        ...currentAnnotations(),
        {
          id: `${activeTool}-${Date.now()}`,
          kind: activeTool,
          label,
          start: { date: trendDraft.date, price: trendDraft.price },
          end: { date: point.point.date, price: point.price },
        },
      ]);
      trendDraft = null;
      setStatus(activeTool === 'rectangle' ? '矩形區間已保存；縮放、平移或重新整理後仍會保留。' : '趨勢線已保存；縮放、平移或重新整理後仍會保留。', 'success');
    } else if (activeTool === 'horizontal') {
      replaceAnnotations([
        ...currentAnnotations(),
        { id: `horizontal-${Date.now()}`, kind: 'horizontal', label, price: point.price },
      ]);
      setStatus(`水平價位 ${niceNumber(point.price)} 已保存。`, 'success');
    } else if (activeTool === 'marker') {
      replaceAnnotations([
        ...currentAnnotations(),
        { id: `marker-${Date.now()}`, kind: 'marker', label, date: point.point.date, price: point.price },
      ]);
      setStatus(`${point.point.date} 的圖表標記已保存。`, 'success');
    } else if (activeTool === 'text') {
      replaceAnnotations([
        ...currentAnnotations(),
        { id: `text-${Date.now()}`, kind: 'text', label: label || `註記 ${niceNumber(point.price)}`, date: point.point.date, price: point.price },
      ]);
      setStatus(`${point.point.date} 的文字標註已保存。`, 'success');
    }
  }

  function handlePointerDown(event) {
    const overlay = event.currentTarget;
    if (event.pointerType === 'touch') {
      activeTouchPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      overlay.setPointerCapture?.(event.pointerId);
      if (activeTouchPointers.size >= 2) {
        beginPinchGesture();
        panGesture = null;
        document.getElementById('chartInteractionSurface')?.removeAttribute('data-panning');
        event.preventDefault();
        return;
      }
    }
    const point = eventChartPoint(event);
    if (!point) return;
    overlay.setPointerCapture?.(event.pointerId);
    if (activeTool === 'cursor') {
      panGesture = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startOffset: viewportFor(renderModel.points.length).offset,
        moved: false,
      };
      document.getElementById('chartInteractionSurface')?.setAttribute('data-panning', 'true');
    } else {
      createAnnotationFromPoint(point);
    }
    event.preventDefault();
  }

  function endPointerGesture(event) {
    if (event?.pointerType === 'touch') {
      activeTouchPointers.delete(event.pointerId);
      if (activeTouchPointers.size < 2) pinchGesture = null;
    }
    if (!panGesture || (event.pointerId !== undefined && panGesture.pointerId !== event.pointerId)) return;
    const moved = panGesture.moved;
    panGesture = null;
    document.getElementById('chartInteractionSurface')?.removeAttribute('data-panning');
    if (moved) setStatus('圖表已平移；按「重設視圖」可回到最新資料。');
  }

  function resetGestureState() {
    activeTouchPointers.clear();
    pinchGesture = null;
    panGesture = null;
    wheelZoomAccumulator = 0;
    window.clearTimeout(wheelZoomEndTimer);
    document.getElementById('chartInteractionSurface')?.removeAttribute('data-panning');
  }

  function finishWheelSession() {
    wheelZoomAccumulator = 0;
    lastWheelAt = 0;
    window.clearTimeout(wheelZoomEndTimer);
  }

  function clampViewportOffset(viewport, total) {
    viewport.offset = Math.max(0, Math.min(total - viewport.span, Number(viewport.offset) || 0));
  }

  function applyZoomToSpan(rawSpan, anchorPoint = null) {
    if (!renderModel) return false;
    const total = renderModel.points.length;
    const viewport = viewportFor(total);
    viewport.all = false;
    const previousSpan = Math.max(1, viewport.span);
    const nextSpan = Math.max(MIN_VISIBLE_BARS, Math.min(total, Math.round(rawSpan)));
    if (nextSpan === previousSpan) return false;
    const visibleStart = Math.max(0, total - viewport.offset - previousSpan);
    const anchorRatio = anchorPoint
      ? Math.max(0, Math.min(1, (anchorPoint.globalIndex - visibleStart) / Math.max(1, previousSpan - 1)))
      : 0.5;
    const anchorIndex = anchorPoint?.globalIndex ?? Math.round(visibleStart + (previousSpan - 1) * anchorRatio);
    const nextStart = Math.round(anchorIndex - (nextSpan - 1) * anchorRatio);
    viewport.span = nextSpan;
    viewport.offset = total - (nextStart + nextSpan);
    clampViewportOffset(viewport, total);
    selectedRange = viewport.span;
    savePreferences();
    syncControls();
    renderCurrentChart();
    setStatus(`目前顯示 ${viewport.span} 根 K 線；滾輪或雙指可繼續縮放。`);
    return true;
  }

  function applyZoom(direction, steps, anchorPoint = null) {
    if (!renderModel || !steps) return false;
    let nextSpan = viewportFor(renderModel.points.length).span;
    for (let step = 0; step < steps; step += 1) {
      nextSpan = direction > 0
        ? Math.max(nextSpan + 1, Math.ceil(nextSpan * 1.16))
        : Math.floor(nextSpan / 1.16);
    }
    return applyZoomToSpan(nextSpan, anchorPoint);
  }

  function normalizedWheelDelta(event) {
    if (event.deltaMode === 1) return event.deltaY * 24;
    if (event.deltaMode === 2) return event.deltaY * Math.max(240, event.currentTarget?.clientHeight || 0);
    return event.deltaY;
  }

  function handleWheel(event) {
    if (!renderModel) return;
    event.preventDefault();
    const now = performance.now();
    if (now - lastWheelAt > 180) wheelZoomAccumulator = 0;
    lastWheelAt = now;
    const delta = normalizedWheelDelta(event);
    if (!Number.isFinite(delta) || delta === 0) return;
    window.clearTimeout(wheelZoomEndTimer);
    wheelZoomEndTimer = window.setTimeout(finishWheelSession, 180);
    if (wheelZoomAccumulator && Math.sign(wheelZoomAccumulator) !== Math.sign(delta)) wheelZoomAccumulator = 0;
    wheelZoomAccumulator += delta;
    const threshold = event.ctrlKey ? 12 : WHEEL_ZOOM_THRESHOLD;
    const steps = Math.min(event.ctrlKey ? 1 : 2, Math.floor(Math.abs(wheelZoomAccumulator) / threshold));
    if (!steps) return;
    const direction = Math.sign(wheelZoomAccumulator);
    wheelZoomAccumulator -= direction * steps * threshold;
    applyZoom(direction, steps, eventChartPoint(event));
  }

  function pinchDistance(points) {
    return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
  }

  function beginPinchGesture() {
    const points = [...activeTouchPointers.values()].slice(0, 2);
    if (points.length < 2 || !renderModel) return;
    const viewport = viewportFor(renderModel.points.length);
    pinchGesture = {
      distance: Math.max(1, pinchDistance(points)),
      span: viewport.span,
      lastSpan: viewport.span,
      anchor: { globalIndex: Math.round((renderModel.visibleStart + renderModel.visibleEnd - 1) / 2) },
    };
    setStatus('雙指縮放中；放開手指後可再次縮放或拖曳。');
  }

  function updatePinchGesture() {
    const points = [...activeTouchPointers.values()].slice(0, 2);
    if (!pinchGesture || points.length < 2 || !renderModel) return;
    const scale = pinchDistance(points) / pinchGesture.distance;
    const targetSpan = Math.max(MIN_VISIBLE_BARS, Math.min(renderModel.points.length, Math.round(pinchGesture.span / Math.max(scale, .1))));
    if (targetSpan === pinchGesture.lastSpan) return;
    if (applyZoomToSpan(targetSpan, pinchGesture.anchor)) pinchGesture.lastSpan = targetSpan;
  }

  function moveKeyboardHover(delta) {
    if (!renderModel) return;
    const start = renderModel.visibleStart;
    const end = renderModel.visibleEnd - 1;
    hoverGlobalIndex = hoverGlobalIndex === null ? end : Math.max(start, Math.min(end, hoverGlobalIndex + delta));
    hoverPrice = renderModel.points[hoverGlobalIndex]?.close ?? hoverPrice;
    renderOverlay();
  }

  function handleKeydown(event) {
    if (event.key === 'ArrowLeft') {
      moveKeyboardHover(-1);
      event.preventDefault();
    } else if (event.key === 'ArrowRight') {
      moveKeyboardHover(1);
      event.preventDefault();
    } else if (event.key === '+' || event.key === '=') {
      applyZoom(-1, 1);
      event.preventDefault();
    } else if (event.key === '-' || event.key === '_') {
      applyZoom(1, 1);
      event.preventDefault();
    } else if (event.key === 'Escape' && trendDraft) {
      trendDraft = null;
      renderOverlay();
      setStatus('已取消尚未完成的趨勢線。');
      event.preventDefault();
    }
  }

  function setType(nextType) {
    if (!VALID_TYPES.has(nextType)) return;
    chartType = nextType;
    savePreferences();
    syncControls();
    renderCurrentChart();
    const names = { candles: 'K 線', bars: '美國線', line: '折線', area: '面積圖', heikin: 'Heikin-Ashi' };
    setStatus(`已切換為${names[chartType]}；游標仍可查看每根資料的完整 OHLC。`, 'success');
  }

  function setTool(nextTool) {
    if (!VALID_TOOLS.has(nextTool)) return;
    activeTool = nextTool;
    trendDraft = null;
    savePreferences();
    syncControls();
    renderOverlay();
    const messages = {
      cursor: '游標模式：移到圖表查看價格；按住拖曳可平移。',
      trend: '趨勢線工具：依序點選兩個日期與價位。',
      horizontal: '水平線工具：點選要持續追蹤的價位。',
      rectangle: '矩形工具：依序點選區間的兩個對角。',
      text: '文字工具：可先輸入標註文字，再點選圖表位置。',
      marker: '標記工具：點選 K 線交點，可搭配標註文字。',
    };
    setStatus(messages[activeTool]);
  }

  function setIndicator(key) {
    if (!INDICATOR_KEYS.includes(key)) return;
    indicators = { ...indicators, [key]: indicators[key] === false };
    savePreferences();
    syncControls();
    renderCurrentChart();
    const labels = { ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', ma60: 'MA60', boll: '布林通道', volume: '成交量', macd: 'MACD' };
    setStatus(`${labels[key]}已${indicators[key] ? '顯示' : '隱藏'}。`, 'success');
  }

  function execute(actionId, input = {}) {
    if (actionId === 'chart.type.set') return setType(String(input.type || ''));
    if (actionId === 'chart.range.set') return setRange(input.range);
    if (actionId === 'chart.indicator.toggle') {
      const key = String(input.indicator || '').toLowerCase();
      if (!INDICATOR_KEYS.includes(key)) throw new Error(`未知圖表指標：${key}`);
      if (typeof input.visible !== 'boolean' || indicators[key] !== input.visible) setIndicator(key);
      return;
    }
    if (actionId === 'chart.annotation.undo') return undoAnnotation();
    if (actionId === 'chart.annotation.clear') return currentAnnotations().length && replaceAnnotations([]);
    if (actionId === 'chart.viewport.reset') return resetViewport();
    if (actionId === 'chart.focus.enter' || actionId === 'chart.focus.exit') {
      const enabled = actionId.endsWith('.enter');
      document.documentElement.dataset.chartFocus = String(enabled);
      document.dispatchEvent(new CustomEvent('stock-ai:chart-focus-change', { detail: { enabled } }));
      const focusButton = document.getElementById('chartFocusMode');
      if (focusButton) focusButton.textContent = enabled ? '離開聚焦' : '聚焦圖表';
      return;
    }
    throw new Error(`尚未連接的圖表動作：${actionId}`);
  }

  function setRange(rawRange) {
    if (!renderModel) return;
    const total = renderModel.points.length;
    selectedRange = rawRange === 'all' ? 'all' : Math.max(MIN_VISIBLE_BARS, Number(rawRange) || DEFAULT_VISIBLE_BARS);
    const viewport = viewportFor(total);
    viewport.all = selectedRange === 'all';
    viewport.span = viewport.all ? total : Math.min(total, selectedRange);
    viewport.offset = 0;
    savePreferences();
    syncControls();
    renderCurrentChart();
    setStatus(`已切換顯示區間：${rawRange === 'all' ? '全部可用資料' : `最近 ${Math.min(total, selectedRange)} 根 K 線`}。`);
  }

  function undoAnnotation() {
    const key = scopeKey();
    const stack = undoByScope.get(key) || [];
    if (!stack.length) return;
    const previous = stack.pop();
    undoByScope.set(key, stack);
    replaceAnnotations(previous, { remember: false });
    setStatus('已復原上一個標註動作。', 'success');
  }

  function resetViewport() {
    if (!renderModel) return;
    const viewport = viewportFor(renderModel.points.length);
    selectedRange = DEFAULT_VISIBLE_BARS;
    viewport.all = false;
    viewport.span = Math.min(renderModel.points.length, DEFAULT_VISIBLE_BARS);
    viewport.offset = 0;
    hoverGlobalIndex = null;
    hoverPrice = null;
    savePreferences();
    syncControls();
    renderCurrentChart();
    setStatus('視圖已回到最新資料與預設縮放。', 'success');
  }

  function init() {
    const overlay = document.getElementById('priceChartOverlay');
    if (!overlay || overlay.dataset.interactionBound === 'true') return;
    overlay.dataset.interactionBound = 'true';
    overlay.addEventListener('pointermove', handlePointerMove);
    overlay.addEventListener('pointerdown', handlePointerDown);
    overlay.addEventListener('pointerup', endPointerGesture);
    overlay.addEventListener('pointercancel', endPointerGesture);
    overlay.addEventListener('lostpointercapture', endPointerGesture);
    overlay.addEventListener('pointerleave', event => {
      if (!panGesture) updateHover(null);
      endPointerGesture(event);
      resetGestureState();
    });
    overlay.addEventListener('pointerenter', event => {
      if (!event.buttons) resetGestureState();
    });
    overlay.addEventListener('wheel', handleWheel, { passive: false });
    overlay.addEventListener('keydown', handleKeydown);
    document.querySelectorAll('[data-chart-type]').forEach(button => {
      button.addEventListener('click', () => setType(button.dataset.chartType));
    });
    document.querySelectorAll('button[data-chart-tool]').forEach(button => {
      button.addEventListener('click', () => setTool(button.dataset.chartTool));
    });
    document.querySelectorAll('[data-chart-range]').forEach(button => {
      button.addEventListener('click', () => setRange(button.dataset.chartRange));
    });
    document.querySelectorAll('[data-chart-indicator]').forEach(button => {
      button.addEventListener('click', () => setIndicator(button.dataset.chartIndicator));
    });
    document.getElementById('chartUndoAnnotation')?.addEventListener('click', undoAnnotation);
    document.getElementById('chartClearAnnotations')?.addEventListener('click', () => {
      if (!currentAnnotations().length) return;
      replaceAnnotations([]);
      setStatus('目前股票與週期的標註已清除；仍可按「復原」取回。', 'success');
    });
    document.getElementById('chartResetViewport')?.addEventListener('click', resetViewport);
    window.addEventListener('blur', resetGestureState);
    document.addEventListener('visibilitychange', () => { if (document.hidden) resetGestureState(); });
    syncControls();
  }

  window.StockChartInteractions = {
    init,
    getChartType: () => chartType,
    getIndicators: () => ({ ...indicators }),
    resolveViewport,
    setRenderModel,
    clearRenderModel,
    refreshOverlay: renderOverlay,
    execute,
  };
})();
