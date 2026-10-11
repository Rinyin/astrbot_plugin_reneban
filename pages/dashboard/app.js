const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const elements = {
  enabled: document.getElementById("enabled"),
  llmBanEnabled: document.getElementById("llm-ban-enabled"),
  cacheTtl: document.getElementById("cache-ttl"),
  adminUmo: document.getElementById("admin-umo"),
  recordsBody: document.getElementById("records-body"),
  recordCount: document.getElementById("record-count"),
  saveStatus: document.getElementById("save-status"),
};

function setStatus(message, isError = false) {
  elements.saveStatus.textContent = message;
  elements.saveStatus.classList.toggle("error", isError);
}

function renderRecords(records) {
  elements.recordCount.textContent = String(records.length);
  elements.recordsBody.replaceChildren();
  if (!records.length) {
    const row = document.createElement("tr");
    row.innerHTML = '<td class="empty" colspan="5">暂无有效拉黑记录</td>';
    elements.recordsBody.appendChild(row);
    return;
  }
  for (const record of records) {
    const row = document.createElement("tr");
    for (const value of [
      record.uid,
      `${record.scope} · ${record.scope_id}`,
      record.time,
      record.remaining,
      record.reason,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    elements.recordsBody.appendChild(row);
  }
}

async function loadData() {
  setStatus("加载中…");
  try {
    const data = await bridge.apiGet("dashboard/data");
    elements.enabled.checked = Boolean(data.enabled);
    elements.llmBanEnabled.checked = Boolean(data.llm_ban_enabled);
    elements.cacheTtl.value = String(data.cache_ttl);
    elements.adminUmo.value = data.admin_umo || "";
    renderRecords(data.bans || []);
    setStatus("");
  } catch (error) {
    setStatus(error.message || "加载失败", true);
  }
}

async function saveConfig() {
  setStatus("保存中…");
  try {
    await bridge.apiPost("dashboard/config", {
      enabled: elements.enabled.checked,
      llm_ban_enabled: elements.llmBanEnabled.checked,
      cache_ttl: Number(elements.cacheTtl.value),
      admin_umo: elements.adminUmo.value.trim(),
    });
    setStatus("已保存");
    await loadData();
  } catch (error) {
    setStatus(error.message || "保存失败", true);
  }
}

await bridge.ready();
document.getElementById("refresh").addEventListener("click", loadData);
document.getElementById("save").addEventListener("click", saveConfig);
await loadData();
