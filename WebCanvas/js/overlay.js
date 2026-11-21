(() => {
  'use strict';
  const canvas = document.getElementById('canvas');
  const ctx = canvas.getContext('2d');

  function renderHistory(history) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!Array.isArray(history)) return;

    history.forEach(item => {
      if (item.type === 'pen' && Array.isArray(item.points)) {
        if (item.points.length < 2) return;
        ctx.beginPath();
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.strokeStyle = item.color;
        ctx.lineWidth = item.width;

        const first = item.points.at(0);
        ctx.moveTo(first.normX * canvas.width, first.normY * canvas.height);

        for (let i = 1; i < item.points.length; i++) {
          const pt = item.points.at(i);
          ctx.lineTo(pt.normX * canvas.width, pt.normY * canvas.height);
        }
        ctx.stroke();
      } else if (item.type === 'arrow') {
        drawArrow(ctx, item.x1 * canvas.width, item.y1 * canvas.height, item.x2 * canvas.width, item.y2 * canvas.height, item.color, item.width);
      } else if (item.type === 'rect') {
        drawRect(ctx, item.x1 * canvas.width, item.y1 * canvas.height, item.x2 * canvas.width, item.y2 * canvas.height, item.color, item.width);
      } else if (item.type === 'ellipse') {
        drawEllipse(ctx, item.x1 * canvas.width, item.y1 * canvas.height, item.x2 * canvas.width, item.y2 * canvas.height, item.color, item.width);
      }
    });
  }

  function handleCustomEvent(raw) {
    const data = (raw && raw.eventData) || raw;
    if (!data) return;

    if (data.type === 'draw_line') {
      ctx.beginPath();
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.strokeStyle = data.color;
      ctx.lineWidth = data.width;
      ctx.moveTo(data.x1 * canvas.width, data.y1 * canvas.height);
      ctx.lineTo(data.x2 * canvas.width, data.y2 * canvas.height);
      ctx.stroke();
    } else if (data.type === 'sync_history') {
      renderHistory(data.history);
    } else if (data.type === 'clear') {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
  }

  const client = window.OBSWidgetsClient.createManagedObsClient({
    reconnectDelayMs: 3000,
    setupListeners: (obs) => {
      obs.on('CustomEvent', handleCustomEvent);
    }
  });

  client.connect();
})();
