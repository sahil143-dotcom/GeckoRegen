/* GeckoRegen — Dashboard SSE client + render logic.
   Vanilla JS. No frameworks. Connects to /api/events (SSE),
   fetches /api/health, /api/changes, /api/heals on load. */

(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────
  var regulators = {};      // id -> {name, jurisdiction, status, ...}
  var prevStatus = {};      // id -> previous status (for pulse animation)
  var evtSource = null;
  var reconnectTimer = null;

  // ── DOM refs ───────────────────────────────────────────────
  var $grid = document.getElementById('health-grid');
  var $feed = document.getElementById('change-feed');
  var $heals = document.getElementById('heal-timeline');
  var $log = document.getElementById('event-log');
  var $live = document.getElementById('live-indicator');
  var $scanBtn = document.getElementById('scan-btn');

  var $statTotal = document.getElementById('stat-total');
  var $statHealthy = document.getElementById('stat-healthy');
  var $statDegraded = document.getElementById('stat-degraded');
  var $statBroken = document.getElementById('stat-broken');
  var $statHeals = document.getElementById('stat-heals');
  var $statChanges = document.getElementById('stat-changes');

  // ── Helpers ────────────────────────────────────────────────

  function fmtPct(v) {
    if (v == null) return '—';
    return (v * 100).toFixed(0) + '%';
  }

  function fmtTime(ts) {
    if (!ts) return '';
    // keep it short: HH:MM:SS from the timestamp string
    var parts = String(ts).split(' ');
    if (parts.length >= 2) return parts[1].slice(0, 8);
    return String(ts).slice(11, 19) || ts;
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  function statusClass(status) {
    if (!status) return 'degraded';
    if (status === 'healthy') return 'healthy';
    if (status === 'degraded') return 'degraded';
    if (status === 'broken') return 'broken';
    if (status === 'healing') return 'healing';
    return 'degraded';
  }

  function statusLabel(status) {
    if (!status) return 'UNKNOWN';
    return status.toUpperCase();
  }

  // ── SSE connection ─────────────────────────────────────────

  function connectSSE() {
    if (evtSource) {
      try { evtSource.close(); } catch (e) {}
    }
    setLiveStatus('connecting');

    try {
      evtSource = new EventSource('/api/events');
    } catch (e) {
      setLiveStatus('error');
      scheduleReconnect();
      return;
    }

    evtSource.onopen = function () {
      setLiveStatus('connected');
    };

    evtSource.onerror = function () {
      setLiveStatus('error');
      try { evtSource.close(); } catch (e) {}
      scheduleReconnect();
    };

    evtSource.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      handleEvent(msg.type, msg.data);
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connectSSE();
    }, 5000);
  }

  function setLiveStatus(state) {
    if (!$live) return;
    $live.classList.remove('live-connected', 'live-error');
    var label = $live.querySelector('.live-label');
    if (state === 'connected') {
      $live.classList.add('live-connected');
      if (label) label.textContent = 'LIVE';
    } else if (state === 'error') {
      $live.classList.add('live-error');
      if (label) label.textContent = 'DISCONNECTED';
    } else {
      if (label) label.textContent = 'CONNECTING…';
    }
  }

  // ── Event handler ──────────────────────────────────────────

  function handleEvent(type, data) {
    logEvent(type, data);

    if (!data) data = {};

    switch (type) {
      case 'scan_started':
        // mark card as healing if we know the regulator
        if (data.regulator_id != null) {
          updateCardStatus(data.regulator_id, 'healing');
        }
        break;

      case 'scan_completed':
        if (data.regulator_id != null && data.result) {
          var res = data.result;
          var newStatus = res.status || 'healthy';
          updateCardStatus(data.regulator_id, newStatus, {
            field_population_rate: res.field_population_rate,
            record_count: res.record_count,
            healed: res.healed
          });
          // refresh feeds since scan may have produced changes/heals
          fetchChanges();
          fetchHeals();
        }
        break;

      case 'scan_failed':
        if (data.regulator_id != null) {
          updateCardStatus(data.regulator_id, 'broken', {
            error_details: data.error
          });
        }
        break;

      case 'health_changed':
        if (data.regulator_id != null) {
          updateCardStatus(data.regulator_id, data.status || 'degraded', data);
        }
        break;

      case 'heal_triggered':
        if (data.regulator_id != null) {
          updateCardStatus(data.regulator_id, 'healing');
        }
        fetchHeals();
        break;

      case 'heal_completed':
        if (data.regulator_id != null) {
          updateCardStatus(data.regulator_id, data.status || 'healthy');
        }
        fetchHeals();
        fetchHealth(); // full refresh to get latest stats
        break;

      case 'new_change':
        fetchChanges();
        break;

      default:
        // unknown event type — just logged
    }
  }

  // ── Event log ──────────────────────────────────────────────

  function logEvent(type, data) {
    if (!$log) return;
    var now = new Date();
    var ts = now.toTimeString().slice(0, 8);

    var line = document.createElement('div');
    line.className = 'event-line';

    var dataStr = '';
    if (data) {
      try {
        dataStr = JSON.stringify(data).slice(0, 200);
        if (JSON.stringify(data).length > 200) dataStr += '…';
      } catch (e) {}
    }

    line.innerHTML =
      '<span class="event-time">' + ts + '</span>' +
      '<span class="event-type ' + escapeHtml(type) + '">' + escapeHtml(type) + '</span>' +
      '<span class="event-data">' + escapeHtml(dataStr) + '</span>';

    $log.insertBefore(line, $log.firstChild);

    // cap at 100 lines
    while ($log.children.length > 100) {
      $log.removeChild($log.lastChild);
    }
  }

  // ── Health grid ────────────────────────────────────────────

  function fetchHealth() {
    fetch('/api/health')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!Array.isArray(data)) return;
        renderHealthGrid(data);
        updateSummary(data);
      })
      .catch(function (e) {
        console.error('fetchHealth error:', e);
      });
  }

  function renderHealthGrid(data) {
    if (!$grid) return;
    if (!data.length) {
      $grid.innerHTML = '<div class="empty-state">No regulators loaded. Trigger a scan or wait for monitor.</div>';
      return;
    }

    // Build/update cards
    var html = '';
    data.forEach(function (reg) {
      var rid = reg.regulator_id;
      var latest = reg.latest || {};
      var status = latest.status || 'unknown';
      var prev = prevStatus[rid];
      var pulseClass = '';

      // Determine pulse animation
      if (prev && prev !== status) {
        if (status === 'broken') pulseClass = ' pulse-broken';
        else if (status === 'healthy' && (prev === 'broken' || prev === 'degraded' || prev === 'healing'))
          pulseClass = ' pulse-healed';
      }

      regulators[rid] = {
        name: reg.regulator_name,
        jurisdiction: reg.jurisdiction,
        status: status,
        latest: latest
      };
      prevStatus[rid] = status;

      var cls = statusClass(status);
      var popRate = latest.field_population_rate;
      var recCount = latest.record_count;
      var missing = latest.missing_fields;
      var missingStr = (Array.isArray(missing) && missing.length) ? missing.join(', ') : 'none';
      var lastScan = latest.timestamp || reg.last_scanned_at;

      html +=
        '<div class="reg-card ' + cls + pulseClass + '" data-rid="' + rid + '">' +
          '<div class="reg-header">' +
            '<span class="reg-name">' + escapeHtml(reg.regulator_name) + '</span>' +
            (reg.jurisdiction ? '<span class="reg-juris">' + escapeHtml(reg.jurisdiction) + '</span>' : '') +
          '</div>' +
          '<div class="reg-status">' +
            '<span class="status-dot"></span>' +
            '<span class="status-text">' + statusLabel(status) + '</span>' +
          '</div>' +
          '<div class="reg-meta">' +
            '<span>pop rate <span class="meta-val">' + fmtPct(popRate) + '</span></span>' +
            '<span>records <span class="meta-val">' + (recCount != null ? recCount : '—') + '</span></span>' +
            '<span>missing <span class="meta-val">' + escapeHtml(missingStr) + '</span></span>' +
            (lastScan ? '<span>last scan <span class="meta-val">' + escapeHtml(fmtTime(lastScan)) + '</span></span>' : '') +
          '</div>' +
        '</div>';
    });

    $grid.innerHTML = html;
  }

  function updateCardStatus(rid, status, extra) {
    var card = $grid && $grid.querySelector('.reg-card[data-rid="' + rid + '"]');
    if (!card) {
      // card doesn't exist yet — do a full refresh
      fetchHealth();
      return;
    }

    var prev = prevStatus[rid] || regulators[rid] && regulators[rid].status;
    var cls = statusClass(status);
    var pulseClass = '';

    if (prev && prev !== status) {
      if (status === 'broken') pulseClass = ' pulse-broken';
      else if (status === 'healthy' && (prev === 'broken' || prev === 'degraded' || prev === 'healing'))
        pulseClass = ' pulse-healed';
    }

    // Remove old status classes and pulse classes
    card.classList.remove('healthy', 'degraded', 'broken', 'healing', 'pulse-broken', 'pulse-healed');
    // Force reflow to restart animation
    if (pulseClass) void card.offsetWidth;
    card.classList.add(cls);
    if (pulseClass) card.classList.add(pulseClass.trim());

    // Update status text
    var stText = card.querySelector('.status-text');
    if (stText) stText.textContent = statusLabel(status);

    // Update meta if extra data provided
    if (extra) {
      var meta = card.querySelector('.reg-meta');
      if (meta && extra.field_population_rate != null) {
        var popSpan = meta.children[0] && meta.children[0].querySelector('.meta-val');
        if (popSpan) popSpan.textContent = fmtPct(extra.field_population_rate);
      }
      if (meta && extra.record_count != null) {
        var recSpan = meta.children[1] && meta.children[1].querySelector('.meta-val');
        if (recSpan) recSpan.textContent = extra.record_count;
      }
    }

    prevStatus[rid] = status;
    if (regulators[rid]) regulators[rid].status = status;

    // Update summary counts
    refreshSummaryFromCards();
  }

  // ── Summary strip ──────────────────────────────────────────

  function updateSummary(healthData) {
    if (!healthData || !Array.isArray(healthData)) return;
    var total = healthData.length;
    var healthy = 0, degraded = 0, broken = 0;
    healthData.forEach(function (reg) {
      var s = (reg.latest && reg.latest.status) || 'unknown';
      if (s === 'healthy') healthy++;
      else if (s === 'degraded') degraded++;
      else if (s === 'broken') broken++;
    });
    if ($statTotal) $statTotal.textContent = total;
    if ($statHealthy) $statHealthy.textContent = healthy;
    if ($statDegraded) $statDegraded.textContent = degraded;
    if ($statBroken) $statBroken.textContent = broken;
  }

  function refreshSummaryFromCards() {
    if (!$grid) return;
    var cards = $grid.querySelectorAll('.reg-card');
    var total = cards.length;
    var healthy = 0, degraded = 0, broken = 0;
    cards.forEach(function (c) {
      if (c.classList.contains('healthy')) healthy++;
      else if (c.classList.contains('degraded')) degraded++;
      else if (c.classList.contains('broken')) broken++;
    });
    if ($statTotal) $statTotal.textContent = total;
    if ($statHealthy) $statHealthy.textContent = healthy;
    if ($statDegraded) $statDegraded.textContent = degraded;
    if ($statBroken) $statBroken.textContent = broken;
  }

  // ── Change feed ────────────────────────────────────────────

  function fetchChanges() {
    fetch('/api/changes?limit=30')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!Array.isArray(data)) return;
        renderChangeFeed(data);
        if ($statChanges) $statChanges.textContent = data.length;
      })
      .catch(function (e) {
        console.error('fetchChanges error:', e);
      });
  }

  function renderChangeFeed(changes) {
    if (!$feed) return;
    if (!changes.length) {
      $feed.innerHTML = '<div class="empty-state">No changes detected yet.</div>';
      return;
    }

    var html = '';
    changes.forEach(function (ch) {
      var sev = ch.severity || 'info';
      var sevClass = 'badge-' + sev;
      var regName = regulators[ch.regulator_id]
        ? regulators[ch.regulator_id].name
        : ('#' + ch.regulator_id);
      var isNew = ch.is_new === 1 || ch.is_new === true;

      html +=
        '<div class="change-item">' +
          '<div class="change-top">' +
            '<span class="change-reg">' + escapeHtml(regName) + '</span>' +
            (isNew ? '<span class="badge badge-new">NEW</span>' : '') +
            '<span class="change-title">' + escapeHtml(ch.title || 'Untitled') + '</span>' +
            '<span class="badge ' + sevClass + '">' + escapeHtml(sev) + '</span>' +
          '</div>' +
          (ch.summary ? '<div class="change-summary">' + escapeHtml(ch.summary) + '</div>' : '') +
          '<div class="change-meta">' +
            (ch.publish_date ? '<span>' + escapeHtml(ch.publish_date) + '</span>' : '') +
            (ch.category ? '<span>' + escapeHtml(ch.category) + '</span>' : '') +
            (ch.article_url ? '<a class="change-link" href="' + escapeHtml(ch.article_url) + '" target="_blank" rel="noopener">link →</a>' : '') +
          '</div>' +
        '</div>';
    });

    $feed.innerHTML = html;
  }

  // ── Healing timeline ───────────────────────────────────────

  function fetchHeals() {
    fetch('/api/heals?limit=30')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!Array.isArray(data)) return;
        renderHealTimeline(data);
        if ($statHeals) $statHeals.textContent = data.length;
      })
      .catch(function (e) {
        console.error('fetchHeals error:', e);
      });
  }

  function renderHealTimeline(heals) {
    if (!$heals) return;
    if (!heals.length) {
      $heals.innerHTML = '<div class="empty-state">No healing events yet.</div>';
      return;
    }

    var html = '';
    heals.forEach(function (h) {
      var status = h.status || 'pending';
      var dotClass = 'pending';
      var symbol = '⋯';
      if (status === 'done' || (h.validation_passed === 1 || h.validation_passed === true)) {
        dotClass = 'success';
        symbol = '✓';
      } else if (status === 'failed' || (h.validation_passed === 0 || h.validation_passed === false)) {
        dotClass = 'failed';
        symbol = '✗';
      }

      var regName = regulators[h.regulator_id]
        ? regulators[h.regulator_id].name
        : ('#' + h.regulator_id);

      var brokenFields = h.broken_fields;
      var fieldsStr = '';
      if (Array.isArray(brokenFields)) fieldsStr = brokenFields.join(', ');
      else if (typeof brokenFields === 'string') {
        try { fieldsStr = JSON.parse(brokenFields).join(', '); } catch (e) { fieldsStr = brokenFields; }
      }

      var durationStr = h.duration_seconds != null ? h.duration_seconds + 's' : '';
      var attemptsStr = h.attempts != null ? h.attempts + ' attempt' + (h.attempts > 1 ? 's' : '') : '';

      html +=
        '<div class="heal-item">' +
          '<div class="heal-dot ' + dotClass + '">' + symbol + '</div>' +
          '<div class="heal-body">' +
            '<div class="heal-reg">' + escapeHtml(regName) + ' — ' + escapeHtml(status) + '</div>' +
            '<div class="heal-detail">' + escapeHtml(fieldsStr ? 'Broken: ' + fieldsStr : 'Healing event') + '</div>' +
            '<div class="heal-meta">' +
              (attemptsStr ? '<span>' + escapeHtml(attemptsStr) + '</span>' : '') +
              (durationStr ? '<span>' + escapeHtml(durationStr) + '</span>' : '') +
              (h.triggered_at ? '<span>' + escapeHtml(fmtTime(h.triggered_at)) + '</span>' : '') +
            '</div>' +
          '</div>' +
        '</div>';
    });

    $heals.innerHTML = html;
  }

  // ── Scan button ────────────────────────────────────────────

  function initScanButton() {
    if (!$scanBtn) return;

    $scanBtn.addEventListener('click', function () {
      $scanBtn.disabled = true;
      var originalText = $scanBtn.innerHTML;
      $scanBtn.innerHTML = '<span class="btn-icon">⟳</span> Scanning…';

      // Find first regulator id, or default to 1
      var regIds = Object.keys(regulators).map(Number).sort(function(a,b){return a-b;});
      var targetId = regIds.length ? regIds[0] : 1;

      fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ regulator_id: targetId })
      })
        .then(function (r) {
          if (r.ok) {
            // SSE events will drive the UI updates; just re-enable the button
            setTimeout(function () {
              $scanBtn.disabled = false;
              $scanBtn.innerHTML = originalText;
            }, 2000);
          } else {
            $scanBtn.disabled = false;
            $scanBtn.innerHTML = originalText;
            return r.json().then(function (d) {
              console.error('Scan failed:', d.error || r.status);
              alert('Scan failed: ' + (d.error || r.status));
            });
          }
        })
        .catch(function (e) {
          $scanBtn.disabled = false;
          $scanBtn.innerHTML = originalText;
          console.error('Scan network error:', e);
          alert('Network error: ' + e);
        });
    });
  }

  // ── Init ───────────────────────────────────────────────────

  function init() {
    // Fetch all data on load
    fetchHealth();
    fetchChanges();
    fetchHeals();

    // Connect SSE
    connectSSE();

    // Wire up scan button
    initScanButton();

    // Periodic refresh as fallback (every 30s) in case SSE drops
    setInterval(function () {
      fetchHealth();
      fetchChanges();
      fetchHeals();
    }, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
