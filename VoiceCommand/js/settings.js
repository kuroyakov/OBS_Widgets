// VoiceCommand settings UI (Issue #29)
(function () {
  'use strict';

  var fetcher = window.authFetch || fetch;
  var obsResources = { connected: false, inputs: [], filters_by_source: {}, hotkeys: [] };
  var currentConfig = null;

  function $(id) { return document.getElementById(id); }

  function showStatus(msg, isError) {
    var banner = $('status-banner');
    banner.textContent = msg;
    banner.className = 'status-banner ' + (isError ? 'is-error' : 'is-ok');
    banner.hidden = false;
  }

  function apiJson(url, options) {
    var f = window.authFetch || fetcher;
    return f(url, options).then(function (res) {
      if (res.status === 401) {
        $('token-panel').hidden = false;
        throw new Error('API トークンが必要です');
      }
      return res.json().then(function (data) {
        if (!res.ok) {
          throw new Error((data && data.error) || ('HTTP ' + res.status));
        }
        return data;
      });
    });
  }

  function parseCsv(text) {
    return String(text || '')
      .split(/[,\u3001]/)
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
  }

  function renderAudioTargets(targets) {
    var container = $('audio-targets-list');
    while (container.firstChild) container.removeChild(container.firstChild);
    var filterText = ($('audio-input-filter').value || '').trim().toLowerCase();

    targets.forEach(function (item, idx) {
      var row = document.createElement('div');
      row.className = 'item-row';

      var gAlias = document.createElement('div');
      gAlias.className = 'field-group';
      var lAlias = document.createElement('label');
      lAlias.textContent = 'エイリアス（カンマ区切り）';
      var inAlias = document.createElement('input');
      inAlias.type = 'text';
      inAlias.value = (item.aliases || []).join(', ');
      inAlias.placeholder = '例: ゲーム音, ゲーム';
      inAlias.addEventListener('input', function () {
        item.aliases = parseCsv(inAlias.value);
      });
      gAlias.appendChild(lAlias);
      gAlias.appendChild(inAlias);

      var gInput = document.createElement('div');
      gInput.className = 'field-group';
      var lInput = document.createElement('label');
      lInput.textContent = 'OBS 音声入力ソース名';
      gInput.appendChild(lInput);

      if (obsResources.connected && obsResources.inputs.length > 0) {
        var sel = document.createElement('select');
        var matchedInputs = obsResources.inputs.filter(function (inp) {
          return !filterText || inp.name.toLowerCase().indexOf(filterText) >= 0 || inp.name === item.input_name;
        });
        if (item.input_name && !matchedInputs.some(function (i) { return i.name === item.input_name; })) {
          var customOpt = document.createElement('option');
          customOpt.value = item.input_name;
          customOpt.textContent = item.input_name + ' (設定済み)';
          sel.appendChild(customOpt);
        }
        matchedInputs.forEach(function (inp) {
          var opt = document.createElement('option');
          opt.value = inp.name;
          opt.textContent = inp.name;
          sel.appendChild(opt);
        });
        sel.value = item.input_name || (matchedInputs[0] ? matchedInputs[0].name : '');
        item.input_name = sel.value;
        sel.addEventListener('change', function () {
          item.input_name = sel.value;
        });
        gInput.appendChild(sel);
      } else {
        var inName = document.createElement('input');
        inName.type = 'text';
        inName.value = item.input_name || '';
        inName.placeholder = '例: Game Audio';
        inName.addEventListener('input', function () {
          item.input_name = inName.value.trim();
        });
        gInput.appendChild(inName);
      }

      var btnDel = document.createElement('button');
      btnDel.type = 'button';
      btnDel.className = 'btn btn-danger';
      btnDel.textContent = '削除';
      btnDel.addEventListener('click', function () {
        currentConfig.audio_targets.splice(idx, 1);
        renderAudioTargets(currentConfig.audio_targets);
      });

      row.appendChild(gAlias);
      row.appendChild(gInput);
      row.appendChild(btnDel);
      container.appendChild(row);
    });
  }

  function renderCustomCommands(commands) {
    var container = $('custom-commands-list');
    while (container.firstChild) container.removeChild(container.firstChild);

    commands.forEach(function (cmd, idx) {
      var row = document.createElement('div');
      row.className = 'item-row';

      var gAlias = document.createElement('div');
      gAlias.className = 'field-group';
      var lAlias = document.createElement('label');
      lAlias.textContent = 'エイリアス（カンマ区切り）';
      var inAlias = document.createElement('input');
      inAlias.type = 'text';
      inAlias.value = (cmd.aliases || []).join(', ');
      inAlias.placeholder = '例: 離席, 離席オン';
      inAlias.addEventListener('input', function () {
        cmd.aliases = parseCsv(inAlias.value);
      });
      gAlias.appendChild(lAlias);
      gAlias.appendChild(inAlias);

      var gType = document.createElement('div');
      gType.className = 'field-group';
      var lType = document.createElement('label');
      lType.textContent = '種別';
      var selType = document.createElement('select');
      [
        ['filter', 'OBS フィルタ'],
        ['hotkey', 'OBS ホットキー'],
        ['away', '離席中モード']
      ].forEach(function (pair) {
        var opt = document.createElement('option');
        opt.value = pair[0];
        opt.textContent = pair[1];
        selType.appendChild(opt);
      });
      selType.value = cmd.type || 'filter';
      selType.addEventListener('change', function () {
        cmd.type = selType.value;
        renderCustomCommands(currentConfig.custom_commands);
      });
      gType.appendChild(lType);
      gType.appendChild(selType);

      row.appendChild(gAlias);
      row.appendChild(gType);

      if (cmd.type === 'filter') {
        var gSrc = document.createElement('div');
        gSrc.className = 'field-group';
        var lSrc = document.createElement('label');
        lSrc.textContent = 'ソース名';
        var inSrc = document.createElement('input');
        inSrc.type = 'text';
        inSrc.value = cmd.source_name || '';
        inSrc.placeholder = 'OBS ソース名';
        inSrc.addEventListener('input', function () { cmd.source_name = inSrc.value.trim(); });
        gSrc.appendChild(lSrc);
        gSrc.appendChild(inSrc);

        var gFlt = document.createElement('div');
        gFlt.className = 'field-group';
        var lFlt = document.createElement('label');
        lFlt.textContent = 'フィルタ名';
        var inFlt = document.createElement('input');
        inFlt.type = 'text';
        inFlt.value = cmd.filter_name || '';
        inFlt.placeholder = 'OBS フィルタ名';
        inFlt.addEventListener('input', function () { cmd.filter_name = inFlt.value.trim(); });
        gFlt.appendChild(lFlt);
        gFlt.appendChild(inFlt);

        var gState = document.createElement('div');
        gState.className = 'field-group';
        var lState = document.createElement('label');
        lState.textContent = '動作';
        var selState = document.createElement('select');
        [['true', 'ON にする'], ['false', 'OFF にする']].forEach(function (p) {
          var o = document.createElement('option');
          o.value = p[0];
          o.textContent = p[1];
          selState.appendChild(o);
        });
        selState.value = (cmd.enabled === false) ? 'false' : 'true';
        selState.addEventListener('change', function () { cmd.enabled = selState.value === 'true'; });
        gState.appendChild(lState);
        gState.appendChild(selState);

        var gDis = document.createElement('div');
        gDis.className = 'field-group';
        var lDis = document.createElement('label');
        lDis.textContent = '事前 OFF フィルタ（任意・カンマ区切り）';
        var inDis = document.createElement('input');
        inDis.type = 'text';
        inDis.value = (cmd.disable_filters || []).join(', ');
        inDis.addEventListener('input', function () { cmd.disable_filters = parseCsv(inDis.value); });
        gDis.appendChild(lDis);
        gDis.appendChild(inDis);

        row.appendChild(gSrc);
        row.appendChild(gFlt);
        row.appendChild(gState);
        row.appendChild(gDis);
      } else if (cmd.type === 'hotkey') {
        var gHk = document.createElement('div');
        gHk.className = 'field-group';
        var lHk = document.createElement('label');
        lHk.textContent = 'OBS ホットキー名';
        var inHk = document.createElement('input');
        inHk.type = 'text';
        inHk.value = cmd.hotkey_name || '';
        inHk.placeholder = '例: OBSBasic.StartRecording';
        inHk.addEventListener('input', function () { cmd.hotkey_name = inHk.value.trim(); });
        gHk.appendChild(lHk);
        gHk.appendChild(inHk);
        row.appendChild(gHk);
      } else {
        var gMode = document.createElement('div');
        gMode.className = 'field-group';
        var lMode = document.createElement('label');
        lMode.textContent = '離席モード';
        var selMode = document.createElement('select');
        [['on', 'ON（離席文面へ切替）'], ['off', 'OFF（元文面へ復帰）']].forEach(function (p) {
          var o = document.createElement('option');
          o.value = p[0];
          o.textContent = p[1];
          selMode.appendChild(o);
        });
        selMode.value = cmd.mode || 'on';
        selMode.addEventListener('change', function () { cmd.mode = selMode.value; });
        gMode.appendChild(lMode);
        gMode.appendChild(selMode);
        row.appendChild(gMode);
      }

      var btnDel = document.createElement('button');
      btnDel.type = 'button';
      btnDel.className = 'btn btn-danger';
      btnDel.textContent = '削除';
      btnDel.addEventListener('click', function () {
        currentConfig.custom_commands.splice(idx, 1);
        renderCustomCommands(currentConfig.custom_commands);
      });
      row.appendChild(btnDel);

      container.appendChild(row);
    });
  }

  function ensureSubtitleMuteLink(cfg) {
    var raw = (cfg && typeof cfg.subtitle_mute_link === 'object' && cfg.subtitle_mute_link) || {};
    var sources = Array.isArray(raw.source_names)
      ? raw.source_names.map(function (s) { return String(s || '').trim(); }).filter(Boolean)
      : [];
    cfg.subtitle_mute_link = {
      enabled: Boolean(raw.enabled),
      input_name: typeof raw.input_name === 'string' ? raw.input_name.trim() : '',
      source_names: sources
    };
    return cfg.subtitle_mute_link;
  }

  function renderSubtitleMuteLinkInput(sml) {
    var container = $('sml-input-container');
    while (container.firstChild) container.removeChild(container.firstChild);

    if (obsResources.connected && obsResources.inputs.length > 0) {
      var sel = document.createElement('select');
      sel.id = 'cfg-sml-input';

      var emptyOpt = document.createElement('option');
      emptyOpt.value = '';
      emptyOpt.textContent = '（未選択）';
      sel.appendChild(emptyOpt);

      if (sml.input_name && !obsResources.inputs.some(function (i) { return i.name === sml.input_name; })) {
        var savedOpt = document.createElement('option');
        savedOpt.value = sml.input_name;
        savedOpt.textContent = sml.input_name + ' (設定済み)';
        sel.appendChild(savedOpt);
      }

      obsResources.inputs.forEach(function (inp) {
        var opt = document.createElement('option');
        opt.value = inp.name;
        opt.textContent = inp.name;
        sel.appendChild(opt);
      });

      sel.value = sml.input_name || '';
      sel.addEventListener('change', function () {
        sml.input_name = sel.value;
      });
      container.appendChild(sel);
    } else {
      var inText = document.createElement('input');
      inText.id = 'cfg-sml-input';
      inText.type = 'text';
      inText.value = sml.input_name || '';
      inText.placeholder = '例: Mic A（OBS 接続後にドロップダウンから選択可）';
      inText.addEventListener('input', function () {
        sml.input_name = inText.value.trim();
      });
      container.appendChild(inText);
    }
  }

  function renderSubtitleMuteLinkSources(sml) {
    var container = $('sml-sources-list');
    while (container.firstChild) container.removeChild(container.firstChild);

    if (obsResources.connected && obsResources.inputs.length > 0) {
      var filterText = ($('sml-sources-filter').value || '').trim().toLowerCase();
      var candidates = [];
      var knownNames = new Set();

      obsResources.inputs.forEach(function (inp) {
        knownNames.add(inp.name);
        candidates.push({ name: inp.name, label: inp.name });
      });

      (sml.source_names || []).forEach(function (savedName) {
        if (!knownNames.has(savedName)) {
          knownNames.add(savedName);
          candidates.unshift({ name: savedName, label: savedName + ' (設定済み)' });
        }
      });

      var visible = candidates.filter(function (c) {
        return !filterText || c.name.toLowerCase().indexOf(filterText) >= 0;
      });

      if (visible.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'hint';
        empty.textContent = '該当する OBS ソースがありません。';
        container.appendChild(empty);
        return;
      }

      visible.forEach(function (c) {
        var label = document.createElement('label');
        label.className = 'checkbox-item';
        label.title = c.label;

        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = c.name;
        cb.checked = (sml.source_names || []).indexOf(c.name) >= 0;
        cb.addEventListener('change', function () {
          var list = (sml.source_names || []).slice();
          var pos = list.indexOf(c.name);
          if (cb.checked && pos < 0) {
            list.push(c.name);
          } else if (!cb.checked && pos >= 0) {
            list.splice(pos, 1);
          }
          sml.source_names = list;
        });

        var span = document.createElement('span');
        span.textContent = c.label;

        label.appendChild(cb);
        label.appendChild(span);
        container.appendChild(label);
      });
    } else {
      var fallback = document.createElement('input');
      fallback.type = 'text';
      fallback.value = (sml.source_names || []).join(', ');
      fallback.placeholder = '例: Subtitle Overlay（カンマ区切り・OBS 接続後に一覧から選択可）';
      fallback.addEventListener('input', function () {
        sml.source_names = parseCsv(fallback.value);
      });
      container.appendChild(fallback);
    }
  }

  function renderSubtitleMuteLink(sml) {
    $('cfg-sml-enabled').checked = Boolean(sml.enabled);
    renderSubtitleMuteLinkInput(sml);
    renderSubtitleMuteLinkSources(sml);
  }

  function renderConfig(cfg) {
    currentConfig = cfg;
    $('cfg-enabled').checked = Boolean(cfg.enabled);
    $('cfg-wake-words').value = (cfg.wake_words || []).join(', ');
    $('cfg-cooldown').value = String(cfg.cooldown_sec != null ? cfg.cooldown_sec : 1.5);
    $('cfg-step-db').value = String(cfg.volume_step_db != null ? cfg.volume_step_db : 2.0);
    $('cfg-min-db').value = String(cfg.volume_min_db != null ? cfg.volume_min_db : -60.0);
    $('cfg-max-db').value = String(cfg.volume_max_db != null ? cfg.volume_max_db : 0.0);
    $('cfg-away-msg').value = cfg.away_message || '';
    $('cfg-normal-msg').value = cfg.normal_message || '';
    renderAudioTargets(cfg.audio_targets || []);
    renderCustomCommands(cfg.custom_commands || []);
    renderSubtitleMuteLink(ensureSubtitleMuteLink(cfg));
  }

  function renderHistory(items) {
    var list = $('history-list');
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!items || items.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'hint';
      empty.textContent = 'まだ実行履歴はありません。';
      list.appendChild(empty);
      return;
    }
    items.forEach(function (item) {
      var row = document.createElement('div');
      row.className = 'history-item' + (item.ok ? '' : ' is-fail');
      var left = document.createElement('span');
      left.textContent = '[' + (item.timestamp || '') + '] ' + (item.summary || item.utterance || '');
      var right = document.createElement('span');
      right.textContent = item.is_test ? '(テスト)' : '';
      row.appendChild(left);
      row.appendChild(right);
      list.appendChild(row);
    });
  }

  function refreshObsResources() {
    return apiJson('/api/voice-command/obs-resources', { cache: 'no-store' })
      .then(function (data) {
        obsResources = data;
        var badge = $('conn-badge');
        if (data.connected) {
          badge.textContent = 'OBS 接続済み';
          badge.className = 'badge badge-ok';
        } else {
          badge.textContent = 'OBS 未接続';
          badge.className = 'badge badge-warn';
        }
        if (currentConfig) {
          renderAudioTargets(currentConfig.audio_targets || []);
          renderSubtitleMuteLink(ensureSubtitleMuteLink(currentConfig));
        }
      })
      .catch(function () {
        $('conn-badge').textContent = 'OBS 未確認';
        $('conn-badge').className = 'badge badge-muted';
      });
  }

  function refreshHistory() {
    return apiJson('/api/voice-command/history', { cache: 'no-store' })
      .then(function (data) {
        renderHistory(data.history || []);
      })
      .catch(function () {});
  }

  function loadAll() {
    return apiJson('/api/voice-command/config', { cache: 'no-store' })
      .then(function (cfg) {
        renderConfig(cfg);
        return Promise.all([refreshObsResources(), refreshHistory()]);
      })
      .catch(function (err) {
        showStatus('設定の読み込みに失敗しました: ' + err.message, true);
      });
  }

  function saveConfig() {
    if (!currentConfig) return;
    var sml = ensureSubtitleMuteLink(currentConfig);
    var payload = {
      enabled: $('cfg-enabled').checked,
      wake_words: parseCsv($('cfg-wake-words').value),
      cooldown_sec: parseFloat($('cfg-cooldown').value),
      volume_step_db: parseFloat($('cfg-step-db').value),
      volume_min_db: parseFloat($('cfg-min-db').value),
      volume_max_db: parseFloat($('cfg-max-db').value),
      away_message: $('cfg-away-msg').value,
      normal_message: $('cfg-normal-msg').value,
      audio_targets: currentConfig.audio_targets || [],
      custom_commands: currentConfig.custom_commands || [],
      subtitle_mute_link: {
        enabled: $('cfg-sml-enabled').checked,
        input_name: sml.input_name || '',
        source_names: sml.source_names || []
      }
    };
    apiJson('/api/voice-command/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (data) {
      if (data && data.config) renderConfig(data.config);
      showStatus('設定を保存しました。', false);
    }).catch(function (err) {
      showStatus('保存に失敗しました: ' + err.message, true);
    });
  }

  function runTest() {
    var text = $('test-utterance-input').value || '';
    var box = $('test-result');
    apiJson('/api/voice-command/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    }).then(function (res) {
      box.hidden = false;
      if (!res.matched) {
        box.className = 'test-result is-error';
        box.textContent = '一致するコマンドがありませんでした (' + (res.reason || 'no_match') + ')';
      } else if (res.ok) {
        box.className = 'test-result is-ok';
        box.textContent = '実行成功: ' + (res.summary || '');
      } else {
        box.className = 'test-result is-error';
        box.textContent = '実行エラー: ' + (res.error || res.summary || '');
      }
      refreshHistory();
    }).catch(function (err) {
      box.hidden = false;
      box.className = 'test-result is-error';
      box.textContent = 'テスト失敗: ' + err.message;
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('btn-save-token').addEventListener('click', function () {
      var tok = ($('api-token-input').value || '').trim();
      if (window.setWidgetApiToken) window.setWidgetApiToken(tok);
      $('token-panel').hidden = true;
      loadAll();
    });
    $('btn-refresh-obs').addEventListener('click', refreshObsResources);
    $('btn-refresh-history').addEventListener('click', refreshHistory);
    $('audio-input-filter').addEventListener('input', function () {
      if (currentConfig) renderAudioTargets(currentConfig.audio_targets || []);
    });
    $('cfg-sml-enabled').addEventListener('change', function () {
      if (currentConfig) ensureSubtitleMuteLink(currentConfig).enabled = $('cfg-sml-enabled').checked;
    });
    $('sml-sources-filter').addEventListener('input', function () {
      if (currentConfig) renderSubtitleMuteLinkSources(ensureSubtitleMuteLink(currentConfig));
    });
    $('btn-add-audio').addEventListener('click', function () {
      if (!currentConfig) return;
      currentConfig.audio_targets.push({ aliases: [], input_name: '' });
      renderAudioTargets(currentConfig.audio_targets);
    });
    $('btn-add-custom').addEventListener('click', function () {
      if (!currentConfig) return;
      currentConfig.custom_commands.push({
        aliases: [],
        type: 'filter',
        source_name: '',
        filter_name: '',
        enabled: true,
        disable_filters: []
      });
      renderCustomCommands(currentConfig.custom_commands);
    });
    $('btn-save-config').addEventListener('click', saveConfig);
    $('btn-run-test').addEventListener('click', runTest);

    loadAll();
  });
})();
