(() => {
  'use strict';

  const DEFAULT_PORT = 8080;

  function parseValidPort(raw) {
    const str = String(raw == null ? '' : raw).trim();
    if (!/^[0-9]+$/.test(str)) return null;
    const port = Number(str);
    if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
    return port;
  }

  document.addEventListener('DOMContentLoaded', () => {
    const portInput = document.getElementById('server-port');
    const btnSave = document.getElementById('btn-save');
    const btnReset = document.getElementById('btn-reset');
    const statusEl = document.getElementById('status');

    function showStatus(msg, isError) {
      statusEl.textContent = msg;
      statusEl.className = isError ? 'status-err' : 'status-ok';
    }

    chrome.storage.local.get({ serverPort: DEFAULT_PORT }, (items) => {
      const port = parseValidPort(items.serverPort) || DEFAULT_PORT;
      portInput.value = String(port);
    });

    btnSave.addEventListener('click', () => {
      const port = parseValidPort(portInput.value);
      if (port === null) {
        showStatus('1〜65535 の整数を入力してください。', true);
        return;
      }
      chrome.storage.local.set({ serverPort: port }, () => {
        if (chrome.runtime && chrome.runtime.lastError) {
          showStatus(`保存に失敗しました: ${chrome.runtime.lastError.message}`, true);
          return;
        }
        portInput.value = String(port);
        showStatus(`保存しました（ポート: ${port}）`, false);
      });
    });

    btnReset.addEventListener('click', () => {
      portInput.value = String(DEFAULT_PORT);
      chrome.storage.local.set({ serverPort: DEFAULT_PORT }, () => {
        if (chrome.runtime && chrome.runtime.lastError) {
          showStatus(`リセットに失敗しました: ${chrome.runtime.lastError.message}`, true);
          return;
        }
        showStatus(`デフォルト (${DEFAULT_PORT}) に戻しました。`, false);
      });
    });
  });
})();
