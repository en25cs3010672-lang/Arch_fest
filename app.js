(() => {
  const state = {
    floor: null,
    cursor: 0,
    paused: false,
    ws: null,
    seek: null,
    seq: 0,
    timer: null,
    kpi: {},
  };

  const $ = (id) => document.getElementById(id);
  const tip = $("tip");

  function money(value) {
    return "₹" + Number(value || 0).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }
  function moneyExact(value) {
    return "₹" + Number(value || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function num(value, digits = 1) {
    return Number(value || 0).toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
  }

  function countTo(el, next, format) {
    const from = Number(el.dataset.value || 0);
    const target = Number(next || 0);
    el.dataset.value = String(target);
    const start = performance.now();
    const dur = 640;
    function frame(now) {
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = format(from + (target - from) * eased);
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function setSliderLabels() {
    $("val-off").textContent = moneyExact($("rate-off").value);
    $("val-shoulder").textContent = moneyExact($("rate-shoulder").value);
    $("val-peak").textContent = moneyExact($("rate-peak").value);
    $("val-demand").textContent = "₹" + Number($("rate-demand").value).toLocaleString("en-IN");
    $("val-window").textContent = $("window").value + " h";
    $("val-z").textContent = Number($("z-threshold").value).toFixed(2);
  }

  async function post(url, body) {
    const seq = ++state.seq;
    const response = await fetch(url, {
      method: "POST",
      headers: body instanceof FormData ? undefined : { "Content-Type": "application/json" },
      body: body instanceof FormData ? body : JSON.stringify(body || {}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed");
    if (seq !== state.seq && url !== "/api/inject") return null;
    return data;
  }

  function applyFloor(floor, animateBrief) {
    if (!floor) return;
    state.floor = floor;
    const meta = floor.meta || {};
    $("file-name").textContent = meta.source || "no file";
    $("source-pill").textContent = (meta.source || "NO TAPE").toUpperCase();
    $("map-line").textContent = meta.mapping
      ? `${meta.interval_count.toLocaleString("en-IN")} rows · ${meta.delimiter} · ${meta.mapping}`
      : "Waiting for ingest.";
    $("ingest-error").hidden = true;

    countTo($("kpi-energy"), floor.kpis.energy_kwh, (v) => num(v, 0));
    countTo($("kpi-cost"), floor.kpis.energy_cost, money);
    countTo($("kpi-demand"), floor.kpis.demand_cost, money);
    countTo($("kpi-alerts"), floor.kpis.anomaly_count, (v) => String(Math.round(v)));
    $("kpi-demand-sub").textContent = `peak ${num(floor.kpis.demand_kw, 1)} kW`;

    if (floor.tariff && !state.slidersReady) {
      $("rate-off").value = floor.tariff.off_peak;
      $("rate-shoulder").value = floor.tariff.shoulder;
      $("rate-peak").value = floor.tariff.peak;
      $("rate-demand").value = floor.tariff.demand;
      $("window").value = floor.anomaly_settings.window_hours;
      $("z-threshold").value = floor.anomaly_settings.z_threshold;
      setSliderLabels();
      state.slidersReady = true;
    }

    $("forecast-total").textContent = floor.forecast_day
      ? `${floor.forecast_day} · ${money(floor.forecast_total)}`
      : "";
    renderTable(floor);
    renderPreview(floor);
    renderBars(floor);
    renderAlerts(floor);
    renderBrief(floor.brief || [], animateBrief);
    drawAll();
    if (floor.injected) flashInject();
  }

  function renderTable(floor) {
    const table = floor.shift_table;
    const head = $("shift-table").querySelector("thead");
    const body = $("shift-table").querySelector("tbody");
    const depts = table.departments || [];
    head.innerHTML = `<tr><th>Shift</th>${depts.map((d) => `<th>${d}</th>`).join("")}<th>Total</th></tr>`;
    body.innerHTML = (table.shifts || []).map((shift) => {
      const cells = depts.map((dept) => {
        const cell = (table.cells[shift] || {})[dept] || { cost: 0 };
        return `<td>₹${Number(cell.cost).toLocaleString("en-IN", { maximumFractionDigits: 0 })}</td>`;
      }).join("");
      const total = (table.shift_totals || {})[shift] || 0;
      return `<tr><td class="shift">${shift}</td>${cells}<td class="hot">₹${Number(total).toLocaleString("en-IN", { maximumFractionDigits: 0 })}</td></tr>`;
    }).join("");
  }

  function renderPreview(floor) {
    const body = $("preview").querySelector("tbody");
    body.innerHTML = (floor.preview || []).map((row) =>
      `<tr><td>${row.timestamp}</td><td>${row.department}</td><td>${Number(row.kwh).toFixed(3)}</td></tr>`
    ).join("");
  }

  function renderBars(floor) {
    const root = $("dept-bars");
    root.innerHTML = (floor.departments || []).map((dept) => `
      <div class="bar-row" data-share="${dept.share}" data-name="${dept.name}" data-cost="${dept.cost}">
        <header><span>${dept.name}</span><b>${dept.share.toFixed(1)}%</b></header>
        <div class="track"><i style="width:${Math.max(2, dept.share)}%"></i></div>
      </div>
    `).join("");
    root.querySelectorAll(".bar-row").forEach((row) => {
      row.addEventListener("mousemove", (event) => {
        showTip(event, `${row.dataset.name}<br>${money(row.dataset.cost)} · ${row.dataset.share}% of energy cost`);
      });
      row.addEventListener("mouseleave", hideTip);
    });
  }

  function renderAlerts(floor) {
    const rail = $("alert-rail");
    const alerts = floor.alerts || [];
    $("rail-count").textContent = String(floor.kpis.anomaly_count || alerts.length);
    rail.innerHTML = alerts.map((alert) => `
      <button type="button" class="alert" data-ts="${alert.timestamp}">
        <b>z ${alert.z.toFixed(2)} · ${alert.department}</b>
        <span>${alert.timestamp} · ${alert.shift} · ${alert.magnitude_kwh > 0 ? "+" : ""}${alert.magnitude_kwh.toFixed(2)} kWh</span>
      </button>
    `).join("") || `<p class="hint">Rail clear at this threshold.</p>`;
    rail.querySelectorAll(".alert").forEach((button) => {
      button.addEventListener("click", () => {
        rail.querySelectorAll(".alert").forEach((node) => node.classList.remove("active"));
        button.classList.add("active");
        state.seek = button.dataset.ts;
        if (state.ws && state.ws.readyState === 1) {
          state.ws.send(JSON.stringify({ cmd: "seek", timestamp: button.dataset.ts }));
        }
        drawAll();
      });
    });
  }

  function renderBrief(steps, animate) {
    const items = [...$("brief").querySelectorAll("li")];
    steps.forEach((step) => {
      const item = $("brief").querySelector(`[data-step="${step.id}"]`);
      if (!item) return;
      item.querySelector("span").textContent = step.body;
    });
    if (!animate) {
      items.forEach((item) => item.classList.add("on"));
      return;
    }
    items.forEach((item) => item.classList.remove("on"));
    items.forEach((item, index) => {
      setTimeout(() => item.classList.add("on"), 240 * index);
    });
  }

  function flashInject() {
    $("live-kw").classList.add("hit");
    document.querySelector(".hero").classList.add("flash");
    document.querySelector(".rail-panel").classList.add("flash");
    setTimeout(() => {
      $("live-kw").classList.remove("hit");
      document.querySelector(".hero").classList.remove("flash");
      document.querySelector(".rail-panel").classList.remove("flash");
    }, 900);
  }

  function showTip(event, html) {
    tip.hidden = false;
    tip.innerHTML = html;
    tip.style.left = Math.min(window.innerWidth - 220, event.clientX + 14) + "px";
    tip.style.top = Math.min(window.innerHeight - 60, event.clientY + 14) + "px";
  }
  function hideTip() { tip.hidden = true; }

  function canvasBox(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(10, rect.width * dpr);
    canvas.height = Math.max(10, rect.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: rect.width, h: rect.height };
  }

  function drawAll() {
    drawTape();
    drawHeat();
    drawForecast();
  }

  function drawTape() {
    const canvas = $("tape");
    const { ctx, w, h } = canvasBox(canvas);
    ctx.clearRect(0, 0, w, h);
    const tape = (state.floor && state.floor.tape) || [];
    if (!tape.length) return;
    const windowSize = 72;
    const end = Math.max(0, Math.min(tape.length - 1, state.cursor));
    const start = Math.max(0, end - windowSize + 1);
    const slice = tape.slice(start, end + 1);
    const max = Math.max(...slice.map((p) => p.kw), 1);
    const pad = { l: 42, r: 12, t: 16, b: 24 };
    ctx.strokeStyle = "rgba(46,230,199,0.12)";
    ctx.lineWidth = 1;
    for (let i = 0; i < 4; i++) {
      const y = pad.t + ((h - pad.t - pad.b) * i) / 3;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(w - pad.r, y);
      ctx.stroke();
    }
    ctx.fillStyle = "#8b9790";
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText(max.toFixed(0), 4, pad.t + 8);
    ctx.fillText("0", 4, h - pad.b);
    ctx.beginPath();
    slice.forEach((point, index) => {
      const x = pad.l + (index / Math.max(1, slice.length - 1)) * (w - pad.l - pad.r);
      const y = pad.t + (1 - point.kw / max) * (h - pad.t - pad.b);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#2ee6c7";
    ctx.lineWidth = 1.6;
    ctx.shadowColor = "rgba(46,230,199,0.65)";
    ctx.shadowBlur = 8;
    ctx.stroke();
    ctx.shadowBlur = 0;
    const last = slice[slice.length - 1];
    if (last) {
      const x = pad.l + ((slice.length - 1) / Math.max(1, slice.length - 1)) * (w - pad.l - pad.r);
      const y = pad.t + (1 - last.kw / max) * (h - pad.t - pad.b);
      ctx.fillStyle = "#2ee6c7";
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
    slice.forEach((point, index) => {
      if (!point.anomaly && point.timestamp !== state.seek) return;
      const x = pad.l + (index / Math.max(1, slice.length - 1)) * (w - pad.l - pad.r);
      const y = pad.t + (1 - point.kw / max) * (h - pad.t - pad.b);
      ctx.fillStyle = "#ff3b30";
      ctx.beginPath();
      ctx.arc(x, y, point.timestamp === state.seek ? 5 : 3.2, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.fillStyle = "#8b9790";
    ctx.fillText(slice[0].timestamp.slice(5, 16), pad.l, h - 6);
    ctx.fillText(slice[slice.length - 1].timestamp.slice(5, 16), w - 118, h - 6);
    $("tape-range").textContent = `${slice[0].timestamp.slice(5, 16)} → ${slice[slice.length - 1].timestamp.slice(5, 16)}`;
  }

  function drawHeat() {
    const canvas = $("heatmap");
    const { ctx, w, h } = canvasBox(canvas);
    ctx.clearRect(0, 0, w, h);
    const heat = state.floor && state.floor.heatmap;
    if (!heat || !heat.values.length) return;
    const rows = heat.values.length;
    const cols = 24;
    const pad = { l: 72, r: 8, t: 8, b: 18 };
    const cw = (w - pad.l - pad.r) / cols;
    const ch = (h - pad.t - pad.b) / rows;
    let max = 1;
    heat.values.forEach((row) => row.forEach((value) => { if (value > max) max = value; }));
    canvas._heat = { pad, cw, ch, rows, cols, max, days: heat.days };
    heat.values.forEach((row, r) => {
      row.forEach((value, c) => {
        const t = value / max;
        ctx.fillStyle = `rgba(46, 230, 199, ${0.08 + t * 0.92})`;
        ctx.fillRect(pad.l + c * cw + 1, pad.t + r * ch + 1, cw - 1.5, ch - 1.5);
      });
      ctx.fillStyle = "#8b9790";
      ctx.font = "10px ui-monospace, monospace";
      ctx.fillText(heat.days[r].slice(5), 4, pad.t + r * ch + ch * 0.7);
    });
    ctx.fillStyle = "#8b9790";
    ctx.font = "10px ui-monospace, monospace";
    for (let hour = 0; hour < 24; hour += 3) {
      ctx.fillText(String(hour).padStart(2, "0"), pad.l + hour * cw, h - 4);
    }
  }

  function drawForecast() {
    const canvas = $("forecast");
    const { ctx, w, h } = canvasBox(canvas);
    ctx.clearRect(0, 0, w, h);
    const bars = (state.floor && state.floor.forecast) || [];
    if (!bars.length) return;
    const max = Math.max(...bars.map((b) => b.cost), 1);
    const pad = { l: 8, r: 8, t: 12, b: 20 };
    const bw = (w - pad.l - pad.r) / bars.length;
    canvas._forecast = { pad, bw, bars };
    bars.forEach((bar, index) => {
      const bh = (bar.cost / max) * (h - pad.t - pad.b);
      const x = pad.l + index * bw + 2;
      const y = h - pad.b - bh;
      ctx.fillStyle = bar.band === "peak" ? "#f0a202" : "rgba(46,230,199,0.75)";
      ctx.fillRect(x, y, Math.max(2, bw - 4), bh);
    });
    ctx.fillStyle = "#8b9790";
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText("00", pad.l, h - 4);
    ctx.fillText("18", pad.l + 18 * bw, h - 4);
    ctx.fillText("23", w - 22, h - 4);
  }

  $("heatmap").addEventListener("mousemove", (event) => {
    const canvas = $("heatmap");
    const map = canvas._heat;
    if (!map) return;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const col = Math.floor((x - map.pad.l) / map.cw);
    const row = Math.floor((y - map.pad.t) / map.ch);
    if (row < 0 || col < 0 || row >= map.rows || col >= map.cols) {
      hideTip();
      return;
    }
    const value = state.floor.heatmap.values[row][col];
    showTip(event, `${map.days[row]} ${String(col).padStart(2, "0")}:00<br>${value.toFixed(2)} kWh`);
  });
  $("heatmap").addEventListener("mouseleave", hideTip);

  $("forecast").addEventListener("mousemove", (event) => {
    const canvas = $("forecast");
    const map = canvas._forecast;
    if (!map) return;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const index = Math.floor((x - map.pad.l) / map.bw);
    if (index < 0 || index >= map.bars.length) {
      hideTip();
      return;
    }
    const bar = map.bars[index];
    showTip(event, `${String(bar.hour).padStart(2, "0")}:00 · ${bar.band}<br>${bar.kwh.toFixed(2)} kWh · ${moneyExact(bar.cost)}`);
  });
  $("forecast").addEventListener("mouseleave", hideTip);

  function scheduleTariff() {
    setSliderLabels();
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      state.timer = null;
      try {
        const floor = await post("/api/tariff", {
          off_peak: Number($("rate-off").value),
          shoulder: Number($("rate-shoulder").value),
          peak: Number($("rate-peak").value),
          demand: Number($("rate-demand").value),
        });
        applyFloor(floor, false);
      } catch (err) {
        $("ingest-error").hidden = false;
        $("ingest-error").textContent = err.message;
      }
    }, 90);
  }

  function scheduleAnomaly() {
    setSliderLabels();
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      state.timer = null;
      try {
        const floor = await post("/api/anomaly", {
          window_hours: Number($("window").value),
          z_threshold: Number($("z-threshold").value),
        });
        applyFloor(floor, false);
      } catch (err) {
        $("ingest-error").hidden = false;
        $("ingest-error").textContent = err.message;
      }
    }, 90);
  }

  ["rate-off", "rate-shoulder", "rate-peak", "rate-demand"].forEach((id) => {
    $(id).addEventListener("input", scheduleTariff);
  });
  ["window", "z-threshold"].forEach((id) => {
    $(id).addEventListener("input", scheduleAnomaly);
  });

  $("load-sample").addEventListener("click", async () => {
    $("load-sample").disabled = true;
    try {
      const floor = await post("/api/load-sample");
      applyFloor(floor, true);
    } catch (err) {
      $("ingest-error").hidden = false;
      $("ingest-error").textContent = err.message;
    } finally {
      $("load-sample").disabled = false;
    }
  });

  $("upload").addEventListener("change", async () => {
    const file = $("upload").files[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    try {
      const floor = await post("/api/upload", body);
      applyFloor(floor, true);
    } catch (err) {
      $("ingest-error").hidden = false;
      $("ingest-error").textContent = err.message;
    }
  });

  $("inject").addEventListener("click", async () => {
    try {
      const floor = await post("/api/inject");
      applyFloor(floor, true);
    } catch (err) {
      $("ingest-error").hidden = false;
      $("ingest-error").textContent = err.message;
    }
  });

  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").textContent = state.paused ? "Resume tape" : "Pause tape";
    $("rec").classList.toggle("on", !state.paused);
    if (state.ws && state.ws.readyState === 1) {
      state.ws.send(JSON.stringify({ cmd: state.paused ? "pause" : "resume" }));
    }
  });

  $("export").addEventListener("click", () => {
    window.location = "/api/export";
  });

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const path = state.socketPath || "/ws/tape";
    const ws = new WebSocket(`${proto}://${location.host}${path}`);
    state.ws = ws;
    ws.onopen = () => $("link").classList.add("on");
    ws.onclose = () => {
      $("link").classList.remove("on");
      if (path === "/ws/tape") state.socketPath = "/ws/live";
      else state.socketPath = "/ws/tape";
      setTimeout(connect, 900);
    };
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type !== "tick") return;
      state.cursor = msg.index;
      if (state.floor && state.floor.version !== msg.version) {
        refresh();
      }
      const live = $("live-kw");
      live.textContent = Number(msg.kw).toFixed(1);
      $("live-ts").textContent = msg.timestamp + (msg.paused ? " · held" : " · rolling");
      $("rec").classList.toggle("on", !msg.paused);
      drawTape();
    };
  }

  async function refresh() {
    const response = await fetch("/api/floor");
    applyFloor(await response.json(), true);
  }

  function clock() {
    const now = new Date();
    $("clock").textContent = now.toLocaleTimeString("en-GB", { hour12: false });
  }
  setInterval(clock, 1000);
  clock();
  window.addEventListener("resize", drawAll);

  $("rec").classList.add("on");
  refresh().then(connect);
})();
