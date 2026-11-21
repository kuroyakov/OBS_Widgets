// ========================================================
// 図形描画共通ヘルパー（矢印・矩形・楕円） (js/shapes.js)
// ========================================================
'use strict';

  /* ========================================================
   * ★ 美しく尖った矢印の描画関数（先端突き抜け完全防止） ★
   * ======================================================== */
  function drawArrow(context, x1, y1, x2, y2, color, width) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const dist = Math.sqrt(dx * dx + dy * dy);
    if (dist < 4) return;

    const angle = Math.atan2(dy, dx);
    const cosA = Math.cos(angle);
    const sinA = Math.sin(angle);

    // 矢尻のプロポーション（太さに応じて綺麗にスケール）
    const headLength = Math.max(20, width * 2.8);
    const headWidth = Math.max(16, width * 2.4);

    // ★ 軸線は先端(x2,y2)ではなく、矢尻三角形の底辺内部で止める！ ★
    const shaftEndX = x2 - (headLength - 3) * cosA;
    const shaftEndY = y2 - (headLength - 3) * sinA;

    // 1. 軸線
    context.save();
    context.beginPath();
    context.strokeStyle = color;
    context.lineWidth = width;
    context.lineCap = 'round';
    context.moveTo(x1, y1);
    context.lineTo(shaftEndX, shaftEndY);
    context.stroke();
    context.restore();

    // 2. 三角形の矢尻（先端が確実に尖る）
    const baseX = x2 - headLength * cosA;
    const baseY = y2 - headLength * sinA;
    const perpX = -sinA * (headWidth / 2);
    const perpY = cosA * (headWidth / 2);

    context.save();
    context.beginPath();
    context.fillStyle = color;
    context.strokeStyle = color;
    context.lineWidth = 1;
    context.lineJoin = 'round';

    context.moveTo(x2, y2);                   // 先端
    context.lineTo(baseX + perpX, baseY + perpY); // 底辺左
    context.lineTo(baseX - perpX, baseY - perpY); // 底辺右
    context.closePath();
    context.fill();
    context.stroke();
    context.restore();
  }

  function drawRect(context, x1, y1, x2, y2, color, width) {
    context.beginPath();
    context.strokeStyle = color;
    context.lineWidth = width;
    context.lineCap = 'round';
    context.lineJoin = 'round';
    const minX = Math.min(x1, x2);
    const minY = Math.min(y1, y2);
    const w = Math.abs(x2 - x1);
    const h = Math.abs(y2 - y1);
    context.strokeRect(minX, minY, w, h);
  }

  function drawEllipse(context, x1, y1, x2, y2, color, width) {
    const rx = Math.abs(x2 - x1) / 2;
    const ry = Math.abs(y2 - y1) / 2;
    if (rx === 0 || ry === 0) return;
    const cx = Math.min(x1, x2) + rx;
    const cy = Math.min(y1, y2) + ry;

    context.beginPath();
    context.strokeStyle = color;
    context.lineWidth = width;
    context.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    context.stroke();
  }

