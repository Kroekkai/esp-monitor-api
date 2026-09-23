const API_BASE = "/iot/api";
const TOKEN_KEY = "iot_token";

let tempChart, humidChart;
let myDevices = []; // [{device_id, name}]

// --- auto-refresh (keeps the chart/table "live" without a manual reload) ---
let autoRefreshTimer = null;
let isOnDashboard = false;

function stopAutoRefresh() {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  const select = document.getElementById("autoRefreshInterval");
  const ms = select ? Number(select.value) : 0;
  // Don't poll while the tab/screen is in the background - saves battery
  // and avoids piling up requests nobody is looking at.
  if (ms > 0 && isOnDashboard && !document.hidden) {
    autoRefreshTimer = setInterval(() => {
      loadData().catch((err) => console.error("auto-refresh failed:", err));
    }, ms);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopAutoRefresh();
  } else {
    startAutoRefresh();
  }
});

// Deep link support: a Telegram alert message includes a link like
// /iot/?device=esp32-01&token=<view-token>. The token is a short-lived,
// read-only, single-device token (see app/auth.py create_device_view_token)
// so tapping it opens straight to that device's graph with NO login prompt.
// It only ever works for that one device - everything else still requires
// a real login. Read once at page load.
const urlParams = new URLSearchParams(window.location.search);
let pendingDeviceId = urlParams.get("device");
const urlViewToken = urlParams.get("token");

// In-memory fallback for the token, used when localStorage is blocked or
// restricted (some in-app browsers, e.g. Telegram's built-in browser, do
// this). Without this fallback, tapping an alert link inside Telegram's
// own browser would silently lose the token right after page load and
// bounce back to the login screen.
let inMemoryToken = null;

function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || inMemoryToken;
  } catch {
    return inMemoryToken;
  }
}

function setToken(token) {
  inMemoryToken = token;
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch (err) {
    console.warn("localStorage unavailable, continuing with in-memory token only:", err);
  }
}

function clearToken() {
  inMemoryToken = null;
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore - nothing to clear if storage was never writable
  }
}

// Decode the JWT payload client-side just to read the username/role for the
// UI (e.g. showing the admin panel). The server independently re-checks the
// signature on every request, so this is display-only, not a trust boundary.
function decodeToken(token) {
  try {
    const payload = token.split(".")[1];
    let base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) base64 += "="; // atob() is strict about padding, JWTs omit it
    const json = atob(base64);
    return JSON.parse(json);
  } catch {
    return null;
  }
}

async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = Object.assign({}, options.headers || {});
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    let detail = "";
    try {
      const data = await res.clone().json();
      detail = data.detail || "";
    } catch {
      // response wasn't JSON - ignore, we still show the generic message below
    }
    showError(
      `เรียก API ไม่สำเร็จ (401 Unauthorized)\npath: ${path}\ntoken ที่ใช้: ${token ? token.slice(0, 24) + "..." : "(ไม่มี token เลย)"}\n${detail ? "เหตุผลจาก server: " + detail : ""}`
    );
    clearToken();
    showLogin();
    throw new Error("Unauthorized");
  }
  return res;
}

function showLogin() {
  isOnDashboard = false;
  stopAutoRefresh();
  document.getElementById("loginSection").classList.remove("hidden");
  document.getElementById("dashboardSection").classList.add("hidden");
  document.getElementById("userBox").classList.add("hidden");
  // Always land back on the login form specifically, not mid-registration.
  document.getElementById("registerForm").classList.add("hidden");
  document.getElementById("showLoginWrap").classList.add("hidden");
  document.getElementById("loginForm").classList.remove("hidden");
  document.getElementById("showRegisterWrap").classList.remove("hidden");
}

function showError(msg) {
  const el = document.getElementById("globalError");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function clearError() {
  document.getElementById("globalError").classList.add("hidden");
}

function selectedDeviceId() {
  return document.getElementById("deviceSelect").value; // "" = all my devices
}

// Landing for a Telegram alert-link view token: shows just that one
// device's chart/table plus its alert-settings form (the token is scoped
// to exactly this device - can view its data and edit its alert
// thresholds, nothing else; enforced server-side too).
async function showGuestDashboard(deviceId) {
  isOnDashboard = true;
  document.getElementById("loginSection").classList.add("hidden");
  document.getElementById("dashboardSection").classList.remove("hidden");
  document.getElementById("userBox").classList.remove("hidden");
  document.getElementById("whoami").textContent = `ลิงก์แจ้งเตือน (${deviceId})`;
  document.getElementById("adminSection").classList.add("hidden");
  document.getElementById("deviceAdminSection").classList.add("hidden");
  document.getElementById("dataTableCard").classList.add("hidden");

  const select = document.getElementById("deviceSelect");
  select.innerHTML = "";
  const opt = document.createElement("option");
  opt.value = deviceId;
  opt.textContent = deviceId;
  select.appendChild(opt);
  select.value = deviceId;
  select.disabled = true;

  try {
    await loadData();
  } catch (err) {
    console.error("loadData failed:", err);
  }
  try {
    await refreshAlertPanel();
  } catch (err) {
    console.error("refreshAlertPanel failed:", err);
  }
  startAutoRefresh();
}

async function showDashboard(claims) {
  isOnDashboard = true;
  document.getElementById("loginSection").classList.add("hidden");
  document.getElementById("dashboardSection").classList.remove("hidden");
  document.getElementById("userBox").classList.remove("hidden");
  document.getElementById("whoami").textContent = `${claims.sub} (${claims.role})`;
  const isAdmin = claims.role === "admin";
  document.getElementById("adminSection").classList.toggle("hidden", !isAdmin);
  document.getElementById("deviceAdminSection").classList.toggle("hidden", !isAdmin);
  document.getElementById("dataTableCard").classList.toggle("hidden", !isAdmin);

  if (isAdmin) {
    try {
      await loadUsersForDropdown();
    } catch (err) {
      console.error("loadUsersForDropdown failed:", err);
    }
    try {
      await loadDevicesAdmin();
    } catch (err) {
      console.error("loadDevicesAdmin failed:", err);
    }
  }
  try {
    await loadMyDevices();
  } catch (err) {
    console.error("loadMyDevices failed:", err);
  }
  startAutoRefresh();
}

function initCharts() {
  const tempCtx = document.getElementById("tempChart");
  const humidCtx = document.getElementById("humidChart");

  // Build a fresh options object per chart (not shared) so each gets its
  // own y-axis range without affecting the other.
  function buildOptions(yMin, yMax, lineColor) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          type: "time",
          time: {
            unit: "hour",
            stepSize: 1,
            displayFormats: { hour: "HH:mm", minute: "HH:mm" },
            tooltipFormat: "d/M HH:mm",
          },
          ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
          grid: { color: "#1e2a3f" },
        },
        y: { min: yMin, max: yMax, ticks: { color: "#94a3b8" } },
      },
      plugins: { legend: { labels: { color: "#e2e8f0" } } },
    };
  }

  tempChart = new Chart(tempCtx, {
    type: "line",
    data: { datasets: [{ label: "อุณหภูมิ (°C)", data: [], borderColor: "#f97316", backgroundColor: "rgba(249, 115, 22, 0.2)", fill: "origin", tension: 0.3, pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 10 }] },
    options: buildOptions(0, 60, "#f97316"),
  });

  humidChart = new Chart(humidCtx, {
    type: "line",
    data: { datasets: [{ label: "ความชื้น (%)", data: [], borderColor: "#38bdf8", backgroundColor: "rgba(56, 189, 248, 0.2)", fill: "origin", tension: 0.3, pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 10 }] },
    options: buildOptions(0, 100, "#38bdf8"),
  });
}

// --- devices (mine) / selector ---

async function loadMyDevices() {
  const res = await apiFetch("/devices/mine");
  if (!res.ok) return;
  myDevices = await res.json();

  const select = document.getElementById("deviceSelect");
  const previous = select.value;
  select.innerHTML = "";

  const allOpt = document.createElement("option");
  allOpt.value = "";
  allOpt.textContent = "ทั้งหมด (ทุกอุปกรณ์ของฉัน)";
  select.appendChild(allOpt);

  myDevices.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d.device_id;
    opt.textContent = d.name ? `${d.name} (${d.device_id})` : d.device_id;
    select.appendChild(opt);
  });

  document.getElementById("noDeviceMsg").classList.toggle("hidden", myDevices.length > 0);

  // deep-link from a Telegram alert wins over "keep previous selection"
  if (pendingDeviceId && [...select.options].some((o) => o.value === pendingDeviceId)) {
    select.value = pendingDeviceId;
    pendingDeviceId = null; // only force this once, not on every reload
  } else if ([...select.options].some((o) => o.value === previous)) {
    select.value = previous;
  } else if (myDevices.length > 0) {
    select.value = myDevices[0].device_id;
  } else {
    select.value = "";
  }

  await onDeviceSelectionChanged();
}

async function onDeviceSelectionChanged() {
  try {
    await loadData();
  } catch (err) {
    console.error("loadData failed:", err);
  }
  try {
    await refreshAlertPanel();
  } catch (err) {
    console.error("refreshAlertPanel failed:", err);
  }
}

// --- sensor data / charts ---

// If two consecutive readings are more than this apart, treat it as a real
// gap (device offline, restarted, etc.) rather than draw a straight line
// implying continuous data across the missing stretch.
const GAP_BREAK_MS = 10 * 60 * 1000; // 10 minutes

function withGapBreaks(points) {
  const result = [];
  for (let i = 0; i < points.length; i++) {
    if (i > 0 && points[i].x.getTime() - points[i - 1].x.getTime() > GAP_BREAK_MS) {
      // a null y-value breaks the line here (Chart.js does not span gaps
      // by default) without needing to touch the main data otherwise
      result.push({ x: new Date(points[i - 1].x.getTime() + 1), y: null });
    }
    result.push(points[i]);
  }
  return result;
}

const THRESHOLD_COLOR = "#ef4444"; // red - distinct from both data lines

function thresholdDataset(label, value, start, end, dashed) {
  return {
    label,
    data: [
      { x: start, y: value },
      { x: end, y: value },
    ],
    borderColor: THRESHOLD_COLOR,
    borderWidth: 1.5,
    borderDash: dashed ? [6, 4] : [],
    pointRadius: 0,
    pointHoverRadius: 0,
    fill: false,
    tension: 0,
  };
}

// Draws the alert threshold ("เกินกำหนด", solid) and recovery ("กลับปกติ",
// dashed) lines on top of the data, using that device's current alert
// settings - skipped entirely when no single device is selected (an
// aggregate "all my devices" view has no one threshold to show).
// Cached alert settings + range for the currently selected device, so the
// "show line" checkboxes can redraw instantly without a network round trip.
let lastThresholdSettings = null;
let lastThresholdRange = null;

async function updateThresholdLines(deviceId, rangeStart, rangeEnd) {
  lastThresholdSettings = null;
  lastThresholdRange = { rangeStart, rangeEnd };

  if (deviceId) {
    try {
      const res = await apiFetch(`/settings/alerts/${encodeURIComponent(deviceId)}`);
      if (res.ok) {
        lastThresholdSettings = await res.json();
      }
    } catch (err) {
      console.error("updateThresholdLines failed:", err);
    }
  }
  renderThresholdLines();
}

// Rebuilds just the threshold/clear reference-line datasets from the last
// fetched settings, honoring the two "show line" checkboxes - instant, no
// network call, safe to call directly from the checkbox change handlers.
function renderThresholdLines() {
  tempChart.data.datasets = tempChart.data.datasets.slice(0, 1);
  humidChart.data.datasets = humidChart.data.datasets.slice(0, 1);

  const s = lastThresholdSettings;
  if (!s || !lastThresholdRange) return;
  const { rangeStart, rangeEnd } = lastThresholdRange;
  const showSolid = document.getElementById("showThresholdLine").checked;
  const showDashed = document.getElementById("showClearLine").checked;

  if (showSolid && s.temp_high !== null && s.temp_high !== undefined) {
    tempChart.data.datasets.push(
      thresholdDataset("อุณหภูมิสูงกว่า", s.temp_high, rangeStart, rangeEnd, false)
    );
  }
  if (showDashed && s.temp_high_clear !== null && s.temp_high_clear !== undefined) {
    tempChart.data.datasets.push(
      thresholdDataset("กลับปกติเมื่อต่ำกว่า", s.temp_high_clear, rangeStart, rangeEnd, true)
    );
  }
  if (showSolid && s.humid_high !== null && s.humid_high !== undefined) {
    humidChart.data.datasets.push(
      thresholdDataset("ความชื้นสูงกว่า", s.humid_high, rangeStart, rangeEnd, false)
    );
  }
  if (showDashed && s.humid_high_clear !== null && s.humid_high_clear !== undefined) {
    humidChart.data.datasets.push(
      thresholdDataset("กลับปกติเมื่อต่ำกว่า", s.humid_high_clear, rangeStart, rangeEnd, true)
    );
  }
}

async function loadData() {
  const deviceId = selectedDeviceId();
  // Falls back to today if this is ever empty (it shouldn't be - the field
  // always defaults to today's date and "ล้างวันที่" resets to today too,
  // never blank - but this keeps loadData() safe either way).
  const dateStr = document.getElementById("dateFilter").value || todayDateStr();
  const isToday = dateStr === todayDateStr();
  const qs = new URLSearchParams();

  // Build the full calendar day in the VIEWER's own local timezone, then
  // convert to UTC for the query - so "today" means today where the person
  // is looking, not where the server happens to run.
  const rangeStart = new Date(`${dateStr}T00:00:00`);
  const rangeEnd = new Date(rangeStart.getTime() + 24 * 60 * 60 * 1000);
  qs.set("start", rangeStart.toISOString().replace(/\.\d{3}Z$/, "Z"));
  qs.set("stop", rangeEnd.toISOString().replace(/\.\d{3}Z$/, "Z"));
  if (deviceId) qs.set("device_id", deviceId);

  const res = await apiFetch(`/sensor/data?${qs.toString()}`);
  if (!res.ok) return;
  const rows = await res.json();
  if (!Array.isArray(rows)) return;

  // rows come back newest-first, so rows[0] is the latest reading
  const latest = rows[0];
  document.getElementById("tempLatest").textContent =
    latest && latest.temperature !== null && latest.temperature !== undefined ? `${latest.temperature}°C` : "";
  document.getElementById("humidLatest").textContent =
    latest && latest.humidity !== null && latest.humidity !== undefined ? `${latest.humidity}%` : "";

  // rows come back newest-first; reverse for a left-to-right timeline
  const ordered = [...rows].reverse();

  // Today (still in progress, live/auto view): pin only the LEFT edge to
  // midnight and let the right edge auto-fit the actual data - so the
  // chart grows naturally as new readings come in, instead of showing a
  // big empty gap out to tonight's midnight in advance.
  // A completed day (anything not today - picked from the calendar, or a
  // day that has fully passed): pin BOTH edges to the full day so the
  // hourly ticks cover the whole 0 -> 24 span consistently.
  [tempChart, humidChart].forEach((chart) => {
    chart.options.scales.x.min = rangeStart;
    if (isToday) {
      delete chart.options.scales.x.max;
    } else {
      chart.options.scales.x.max = rangeEnd;
    }
  });

  tempChart.data.datasets[0].data = withGapBreaks(ordered.map((r) => ({ x: new Date(r.time), y: r.temperature })));
  humidChart.data.datasets[0].data = withGapBreaks(ordered.map((r) => ({ x: new Date(r.time), y: r.humidity })));

  await updateThresholdLines(deviceId, rangeStart, rangeEnd);

  tempChart.update();
  humidChart.update();

  const tbody = document.querySelector("#dataTable tbody");
  tbody.innerHTML = "";
  rows.slice(0, 50).forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${new Date(r.time).toLocaleString()}</td><td>${r.device_id ?? ""}</td><td>${r.temperature ?? ""}</td><td>${r.humidity ?? ""}</td>`;
    tbody.appendChild(tr);
  });

  document.getElementById("lastUpdated").textContent = `อัปเดตล่าสุด: ${new Date().toLocaleTimeString()}`;
}

// --- auth ---

async function login(username, password) {
  const res = await fetch(`${API_BASE}/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error("Invalid username or password");
  const data = await res.json();
  setToken(data.access_token);
  return decodeToken(data.access_token);
}

async function register(username, password) {
  const res = await fetch(`${API_BASE}/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // Pydantic validation errors (422) come back as {"detail": [...]} not a
    // plain string - fall back to a generic message in that case.
    const msg = typeof data.detail === "string" ? data.detail : "สมัครสมาชิกไม่สำเร็จ ลองตรวจสอบข้อมูลอีกครั้ง";
    throw new Error(msg);
  }
  setToken(data.access_token);
  return decodeToken(data.access_token);
}

// --- user management (admin) ---

async function createUser(username, password, role) {
  const res = await apiFetch("/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "สร้าง user ไม่สำเร็จ");
  return data;
}

async function exportCsv() {
  const res = await apiFetch("/users/export");
  if (!res.ok) return;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "users.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function loadUsersForDropdown() {
  const res = await apiFetch("/users");
  if (!res.ok) return;
  const users = await res.json();
  const select = document.getElementById("devOwner");
  const previous = select.value;
  select.innerHTML = '<option value="">— ไม่ผูกกับ user —</option>';
  users.forEach((u) => {
    const opt = document.createElement("option");
    opt.value = u.username;
    opt.textContent = `${u.username} (${u.role})`;
    select.appendChild(opt);
  });
  select.value = previous;
}

// --- device management (admin) ---

async function loadDevicesAdmin() {
  const res = await apiFetch("/devices");
  if (!res.ok) return;
  const devices = await res.json();
  const tbody = document.querySelector("#deviceTable tbody");
  tbody.innerHTML = "";
  devices.forEach((d) => {
    const tr = document.createElement("tr");
    const owner = d.owner_username ? d.owner_username : "-";
    tr.innerHTML = `<td>${d.device_id}</td><td>${d.name ?? ""}</td><td>${owner}</td><td></td>`;
    const delBtn = document.createElement("button");
    delBtn.textContent = "ลบ";
    delBtn.addEventListener("click", () => deleteDevice(d.device_id));
    tr.lastElementChild.appendChild(delBtn);
    tbody.appendChild(tr);
  });
}

async function saveDevice(deviceId, name, ownerUsername) {
  const res = await apiFetch("/devices", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      device_id: deviceId,
      name: name || null,
      owner_username: ownerUsername || null,
    }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "บันทึกอุปกรณ์ไม่สำเร็จ");
  return data;
}

async function deleteDevice(deviceId) {
  const res = await apiFetch(`/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
  if (res.ok) {
    await loadDevicesAdmin();
    await loadMyDevices();
  }
}

// --- alert settings (scoped to selected device) ---

async function refreshAlertPanel() {
  const deviceId = selectedDeviceId();
  const form = document.getElementById("alertForm");
  const pickMsg = document.getElementById("alertPickDeviceMsg");
  if (!deviceId) {
    form.classList.add("hidden");
    pickMsg.classList.remove("hidden");
    return;
  }
  pickMsg.classList.add("hidden");
  form.classList.remove("hidden");
  await loadAlertSettings(deviceId);
}

async function loadAlertSettings(deviceId) {
  const res = await apiFetch(`/settings/alerts/${encodeURIComponent(deviceId)}`);
  if (!res.ok) return;
  const s = await res.json();
  document.getElementById("alertChatId").value = s.telegram_chat_id ?? "";
  document.getElementById("alertTempHigh").value = s.temp_high ?? "";
  document.getElementById("alertTempHighClear").value = s.temp_high_clear ?? "";
  document.getElementById("alertHumidHigh").value = s.humid_high ?? "";
  document.getElementById("alertHumidHighClear").value = s.humid_high_clear ?? "";
  document.getElementById("alertCooldown").value = s.cooldown_minutes ?? 15;
  document.getElementById("alertEnabled").checked = !!s.enabled;
  document.getElementById("alertMsg").textContent = "";
}

function numOrNull(id) {
  const v = document.getElementById(id).value;
  return v === "" ? null : Number(v);
}

async function saveAlertSettings() {
  const deviceId = selectedDeviceId();
  if (!deviceId) return;
  const payload = {
    telegram_chat_id: document.getElementById("alertChatId").value.trim() || null,
    temp_high: numOrNull("alertTempHigh"),
    temp_high_clear: numOrNull("alertTempHighClear"),
    // "ต่ำกว่า" ถูกเอาออกจากหน้าเว็บแล้ว - ส่ง null เสมอเพื่อล้างค่าเก่าที่อาจตั้งไว้ก่อนหน้านี้
    temp_low: null,
    temp_low_clear: null,
    humid_high: numOrNull("alertHumidHigh"),
    humid_high_clear: numOrNull("alertHumidHighClear"),
    humid_low: null,
    humid_low_clear: null,
    cooldown_minutes: Number(document.getElementById("alertCooldown").value) || 15,
    enabled: document.getElementById("alertEnabled").checked,
  };
  const res = await apiFetch(`/settings/alerts/${encodeURIComponent(deviceId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const msgEl = document.getElementById("alertMsg");
  if (res.ok) {
    msgEl.textContent = "บันทึกการตั้งค่าแล้ว";
  } else {
    const data = await res.json().catch(() => ({}));
    msgEl.textContent = data.detail || "บันทึกไม่สำเร็จ";
  }
}

async function testAlert() {
  const deviceId = selectedDeviceId();
  if (!deviceId) return;
  const msgEl = document.getElementById("alertMsg");
  msgEl.textContent = "กำลังส่ง...";
  const res = await apiFetch(`/settings/alerts/${encodeURIComponent(deviceId)}/test`, { method: "POST" });
  const data = await res.json().catch(() => ({}));
  msgEl.textContent = res.ok ? "ส่งข้อความทดสอบแล้ว เช็ค Telegram ได้เลย" : (data.detail || "ส่งไม่สำเร็จ");
}

// --- event bindings ---

document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("loginUsername").value;
  const password = document.getElementById("loginPassword").value;
  const errEl = document.getElementById("loginError");
  errEl.textContent = "";
  try {
    const claims = await login(username, password);
    clearError();
    await showDashboard(claims);
  } catch (err) {
    errEl.textContent = err.message;
  }
});

document.getElementById("registerForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("registerUsername").value;
  const password = document.getElementById("registerPassword").value;
  const errEl = document.getElementById("registerError");
  errEl.textContent = "";
  try {
    const claims = await register(username, password);
    clearError();
    await showDashboard(claims);
  } catch (err) {
    errEl.textContent = err.message;
  }
});

document.getElementById("showRegisterLink").addEventListener("click", (e) => {
  e.preventDefault();
  document.getElementById("loginForm").classList.add("hidden");
  document.getElementById("showRegisterWrap").classList.add("hidden");
  document.getElementById("registerForm").classList.remove("hidden");
  document.getElementById("showLoginWrap").classList.remove("hidden");
  document.getElementById("loginError").textContent = "";
});

document.getElementById("showLoginLink").addEventListener("click", (e) => {
  e.preventDefault();
  document.getElementById("registerForm").classList.add("hidden");
  document.getElementById("showLoginWrap").classList.add("hidden");
  document.getElementById("loginForm").classList.remove("hidden");
  document.getElementById("showRegisterWrap").classList.remove("hidden");
  document.getElementById("registerError").textContent = "";
});

document.getElementById("logoutBtn").addEventListener("click", () => {
  clearToken();
  showLogin();
});

// Local YYYY-MM-DD for <input type="date"> - NOT toISOString().slice(0,10),
// which would give the UTC date and could be off by one day depending on
// the viewer's timezone (e.g. still "yesterday" in UTC after 7pm in
// Bangkok).
function todayDateStr() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// The date field always shows a real date (defaults to today, never
// blank) - set once at page load, independent of login state.
document.getElementById("dateFilter").value = todayDateStr();

document.getElementById("refreshBtn").addEventListener("click", loadData);
document.getElementById("deviceSelect").addEventListener("change", onDeviceSelectionChanged);
document.getElementById("autoRefreshInterval").addEventListener("change", startAutoRefresh);

function toggleThresholdLines() {
  renderThresholdLines();
  tempChart.update();
  humidChart.update();
}
document.getElementById("showThresholdLine").addEventListener("change", toggleThresholdLines);
document.getElementById("showClearLine").addEventListener("change", toggleThresholdLines);

document.getElementById("dateFilter").addEventListener("change", () => {
  const dateEl = document.getElementById("dateFilter");
  if (!dateEl.value) dateEl.value = todayDateStr(); // never let it sit blank
  const isToday = dateEl.value === todayDateStr();
  document.getElementById("clearDateBtn").classList.toggle("hidden", isToday);
  // Picking a past day is a one-off look-back, not something to keep
  // auto-polling - switch auto-refresh off so the view doesn't jump back
  // to today on its own a few seconds later. Back on today, auto-refresh
  // makes sense again, so turn it back on.
  const autoSelect = document.getElementById("autoRefreshInterval");
  if (isToday) {
    if (autoSelect.value === "0") autoSelect.value = "30000";
    startAutoRefresh();
  } else {
    autoSelect.value = "0";
    stopAutoRefresh();
  }
  loadData().catch((err) => console.error("loadData failed:", err));
});

document.getElementById("clearDateBtn").addEventListener("click", () => {
  document.getElementById("dateFilter").value = todayDateStr();
  document.getElementById("clearDateBtn").classList.add("hidden");
  const autoSelect = document.getElementById("autoRefreshInterval");
  if (autoSelect.value === "0") autoSelect.value = "30000";
  startAutoRefresh();
  loadData().catch((err) => console.error("loadData failed:", err));
});

document.getElementById("createUserForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("newUsername").value;
  const password = document.getElementById("newPassword").value;
  const role = document.getElementById("newRole").value;
  const msgEl = document.getElementById("userMsg");
  try {
    await createUser(username, password, role);
    msgEl.textContent = `สร้าง user "${username}" สำเร็จ`;
    document.getElementById("createUserForm").reset();
    await loadUsersForDropdown();
  } catch (err) {
    msgEl.textContent = err.message;
  }
});

document.getElementById("exportCsvBtn").addEventListener("click", exportCsv);

document.getElementById("deviceForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const deviceId = document.getElementById("devDeviceId").value.trim();
  const name = document.getElementById("devName").value.trim();
  const owner = document.getElementById("devOwner").value;
  const msgEl = document.getElementById("deviceMsg");
  try {
    await saveDevice(deviceId, name, owner);
    msgEl.textContent = `บันทึกอุปกรณ์ "${deviceId}" แล้ว`;
    document.getElementById("deviceForm").reset();
    await loadDevicesAdmin();
    await loadMyDevices();
  } catch (err) {
    msgEl.textContent = err.message;
  }
});

document.getElementById("alertForm").addEventListener("submit", (e) => {
  e.preventDefault();
  saveAlertSettings();
});

document.getElementById("testAlertBtn").addEventListener("click", testAlert);

// --- boot ---
// Wrap EVERYTHING so any unexpected error (e.g. Chart.js failing to load
// from the CDN, which would otherwise throw before we ever reach the token
// check below and leave the page silently stuck on the login screen with
// no clue why) always surfaces as a visible message instead of failing
// silently.
try {
  initCharts();
} catch (err) {
  console.error("initCharts failed:", err);
  showError(
    `โหลดกราฟไม่สำเร็จ: ${err.message}\n(มักเกิดจากโหลดไลบรารี Chart.js จาก cdnjs.cloudflare.com ไม่สำเร็จ ลองเช็คการเชื่อมต่ออินเทอร์เน็ต หรือเปิดผ่านเบราว์เซอร์ปกติแทน in-app browser)`
  );
}

try {
  const existingToken = getToken();
  const existingClaims = existingToken ? decodeToken(existingToken) : null;
  const hasValidSession =
    existingClaims && existingClaims.exp * 1000 > Date.now() && existingClaims.scope !== "device_view";

  if (hasValidSession) {
    // Already logged in on this browser - a real session always wins over a
    // scoped alert-link token, since it gives full access to everything the
    // guest token can (and more).
    showDashboard(existingClaims);
  } else if (urlViewToken) {
    const guestClaims = decodeToken(urlViewToken);
    if (!guestClaims) {
      showError(
        `อ่าน token จากลิงก์ไม่ได้ (รูปแบบไม่ถูกต้อง) - ลิงก์อาจถูกตัดตอนคัดลอก/แชร์มา\ntoken length: ${urlViewToken.length} ตัวอักษร`
      );
      clearToken();
      showLogin();
    } else if (guestClaims.scope !== "device_view") {
      showError(`token นี้ไม่ใช่ประเภทลิงก์แจ้งเตือน (scope: ${guestClaims.scope || "ไม่มี"})`);
      clearToken();
      showLogin();
    } else if (guestClaims.exp * 1000 <= Date.now()) {
      showError(
        `ลิงก์นี้หมดอายุแล้ว (หมดอายุเมื่อ ${new Date(guestClaims.exp * 1000).toLocaleString()}) - กด "ส่งข้อความทดสอบ" ใหม่เพื่อได้ลิงก์สดๆ`
      );
      clearToken();
      showLogin();
    } else {
      clearError();
      setToken(urlViewToken);
      showGuestDashboard(guestClaims.device_id);
    }
  } else {
    clearToken();
    showLogin();
  }
} catch (err) {
  console.error("boot failed:", err);
  showError(`เกิดข้อผิดพลาดตอนโหลดหน้าเว็บ: ${err.message}`);
  showLogin();
}
