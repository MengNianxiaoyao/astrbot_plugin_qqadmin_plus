import { createApi } from "./api.js";
import {
  collectFormData,
  renderSchemaFields,
} from "./form-renderer.js";
import {
  renderGroupCards,
  renderGroupDetailHeader,
} from "./group-view.js";
import { createThemeController } from "./theme.js";

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const DEFAULT_GROUP_ID = "__default__";
const COLLAPSED_GROUP_OBJECT_PATHS = new Set(["perms"]);
const EXPANDED_GROUP_OBJECT_PATHS = new Set(["vote_ban"]);
const FOLLOW_DEFAULT_KEY = "follow_default";

let api = null;
let bootstrapData = null;
let currentGroup = null;
let allGroups = [];
let groupListError = null;
let detachContextHandler = null;
let themeController = null;
let groupRoleSyncToken = 0;
let formDirty = false;

const els = {
  groupForm: document.getElementById("groupForm"),
  groupList: document.getElementById("groupList"),
  groupSearchInput: document.getElementById("groupSearchInput"),
  currentGroupName: document.getElementById("currentGroupName"),
  currentGroupMeta: document.getElementById("currentGroupMeta"),
  groupListCount: document.getElementById("groupListCount"),
  toastLayer: document.getElementById("toastLayer"),
  toggleThemeBtn: document.getElementById("toggleThemeBtn"),
  refreshGroupsBtn: document.getElementById("refreshGroupsBtn"),
  saveGroupBtn: document.getElementById("saveGroupBtn"),
  resetGroupBtn: document.getElementById("resetGroupBtn"),
  globalListPanel: document.getElementById("globalListPanel"),
  globalListContent: document.getElementById("globalListContent"),
  globalBanWordsPanel: document.getElementById("globalBanWordsPanel"),
  globalListDisplay: document.getElementById("globalListDisplay"),
  globalListSearchInput: document.getElementById("globalListSearchInput"),
  globalListBatchInput: document.getElementById("globalListBatchInput"),
  overwriteGlobalListBtn: document.getElementById("overwriteGlobalListBtn"),
  appendGlobalListBtn: document.getElementById("appendGlobalListBtn"),
  deleteSelectedListBtn: document.getElementById("deleteSelectedListBtn"),
  groupActions: document.getElementById("groupActions"),
  globalBanWordsDisplay: document.getElementById("globalBanWordsDisplay"),
  builtinBanWordsDisplay: document.getElementById("builtinBanWordsDisplay"),
  builtinBanWordsVersion: document.getElementById("builtinBanWordsVersion"),
  globalBanWordsBatchInput: document.getElementById("globalBanWordsBatchInput"),
  appendGlobalBanWordsBtn: document.getElementById("appendGlobalBanWordsBtn"),
  overwriteGlobalBanWordsBtn: document.getElementById("overwriteGlobalBanWordsBtn"),
  deleteSelectedBanWordsBtn: document.getElementById("deleteSelectedBanWordsBtn"),
  restoreBuiltinBanWordsBtn: document.getElementById("restoreBuiltinBanWordsBtn"),
  importSelectedBanWordsBtn: document.getElementById("importSelectedBanWordsBtn"),
  groupListPanel: document.getElementById("groupListPanel"),
  groupSearchWrap: document.getElementById("groupSearchWrap"),
  globalSidePanel: document.getElementById("globalSidePanel"),
  globalListSideActions: document.getElementById("globalListSideActions"),
  globalBanWordsSideActions: document.getElementById("globalBanWordsSideActions"),
  workspaceGrid: document.querySelector(".workspace-grid"),
  viewTabs: document.querySelectorAll(".view-tab"),
  globalListTabs: document.querySelectorAll(".global-list-tab"),
};

let currentGlobalType = "allow";
let currentView = "group";
let globalListKeyword = "";
let globalListData = { allow: [], block: [] };
let globalBanWordsData = { global: [], builtin: [] };

function updateThemeButton() {
  if (els.toggleThemeBtn && themeController) {
    els.toggleThemeBtn.textContent = themeController.getButtonLabel();
  }
}

function showToast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  els.toastLayer.appendChild(node);
  setTimeout(() => {
    // 锁定当前实际高度再塌缩：否则 max-height 从 240px 起算，
    // 布局高度要到动画末尾才开始收缩，后续 toast 会先卡住再跳变，
    // 无法平滑补位到第一条的位置。
    node.style.maxHeight = `${node.offsetHeight}px`;
    void node.offsetHeight;
    node.classList.add("is-leaving");
    node.style.maxHeight = "0px";
    setTimeout(() => node.remove(), 350);
  }, 2600);
}

function buildGroupFormValues(groupPayload) {
  const currentValues = groupPayload?.config || {};
  const followDefault = Boolean(currentValues[FOLLOW_DEFAULT_KEY]);
  if (!followDefault || groupPayload?.is_default_group) {
    return currentValues;
  }
  const defaultGroup = bootstrapData?.groups?.find(
    (g) => g.group_id === DEFAULT_GROUP_ID
  );
  return {
    ...(defaultGroup?.config || {}),
    [FOLLOW_DEFAULT_KEY]: true,
  };
}

function isGroupFieldDisabled(path) {
  if (!currentGroup || currentGroup.is_default_group) {
    return false;
  }
  if (!currentGroup.config?.[FOLLOW_DEFAULT_KEY]) {
    return false;
  }
  return path !== FOLLOW_DEFAULT_KEY;
}

function updateGroupActionState() {
  const isDefaultGroup = Boolean(currentGroup?.is_default_group);
  const isFollowingDefault = Boolean(currentGroup?.config?.[FOLLOW_DEFAULT_KEY]);

  els.resetGroupBtn.disabled = isDefaultGroup || isFollowingDefault;
  els.resetGroupBtn.textContent = isDefaultGroup
    ? "默认群不支持重置"
    : isFollowingDefault
      ? "当前正在跟随默认配置"
      : "恢复当前项默认值";
  els.saveGroupBtn.textContent = isDefaultGroup
    ? "保存默认群模板"
    : "保存当前项配置";
}

function applyGroupList(groups) {
  if (!bootstrapData) {
    return;
  }
  allGroups = Array.isArray(groups) ? groups : [];
  bootstrapData.groups = allGroups;
  filterAndRenderGroups();
}

function scheduleGroupRoleSync(options = {}) {
  const requestToken = ++groupRoleSyncToken;
  syncGroupRoles(requestToken, options);
}

function filterAndRenderGroups() {
  const keyword = String(els.groupSearchInput.value || "")
    .trim()
    .toLowerCase();
  const groups = keyword
    ? allGroups.filter((group) => {
        const groupId = String(group.group_id || "").toLowerCase();
        const groupName = String(group.group_name || "").toLowerCase();
        return groupId.includes(keyword) || groupName.includes(keyword);
      })
    : allGroups;

  els.groupListCount.textContent = `${groups.length} 个群`;
  renderGroupCards({
    root: els.groupList,
    groups,
    currentGroupId: currentGroup?.group_id || "",
    emptyText: groupListError && !groups.length ? groupListError : "当前没有可显示的群。",
    onSelect: async (groupId) => {
      try {
        // 全局视图下选群视为回到群配置，避免群名覆盖全局标题
        if (currentView === "global") {
          switchView("group");
        }
        await switchGroup(groupId);
      } catch (error) {
        showToast(error.message, "error");
      }
    },
  });
}

function markFormDirty() {
  formDirty = true;
}

function updateActiveGroupCard() {
  const activeId = String(currentGroup?.group_id || "");
  els.groupList.querySelectorAll(".group-card").forEach((card) => {
    card.classList.toggle("is-active", card.dataset.groupId === activeId);
  });
}

function setFieldsDisabled(disabled) {
  els.groupForm.querySelectorAll("[data-path]").forEach((node) => {
    if (node.dataset.path === FOLLOW_DEFAULT_KEY) {
      return;
    }
    node.disabled = disabled;
  });
  els.groupForm.querySelectorAll(".field, .form-object").forEach((field) => {
    field.classList.toggle("is-disabled", disabled);
  });
}

function renderGroupForm(groupPayload) {
  currentGroup = groupPayload;

  renderGroupDetailHeader(els, groupPayload);
  const rawSchema = bootstrapData.schema.group || {};
  // 默认群模板本身就是被跟随的对象，不需要"跟随默认配置"开关
  const schema = groupPayload?.is_default_group
    ? Object.fromEntries(
        Object.entries(rawSchema).filter(([key]) => key !== FOLLOW_DEFAULT_KEY)
      )
    : rawSchema;
  renderSchemaFields(
    els.groupForm,
    schema,
    buildGroupFormValues(groupPayload),
    {
      singleColumn: true,
      collapsedObjectPaths: COLLAPSED_GROUP_OBJECT_PATHS,
      expandedObjectPaths: EXPANDED_GROUP_OBJECT_PATHS,
      isFieldDisabled: isGroupFieldDisabled,
      // 跟随开关置顶；投票禁言/权限管理等无分组项沉底
      leadingPaths: [FOLLOW_DEFAULT_KEY],
      // 面板分组定义表（组名/组说明/成员/排序），来自 schema
      groups: bootstrapData.schema.groups || {},
    }
  );
  bindFollowDefaultToggle();
  updateGroupActionState();
  updateActiveGroupCard();
}

async function loadBootstrapData() {
  const data = await api.safeGet("settings/bootstrap");
  bootstrapData = data;
  groupListError = data.refresh_error || null;
  applyGroupList(data.groups || []);
  if (groupListError) {
    showToast(groupListError, "error");
  }
}

async function syncGroupRoles(requestToken, options = {}) {
  const { force = false, prune = true } = options;
  try {
    const groups = await api.safePost("settings/groups/roles", {
      force: force ? "1" : "0",
      prune: prune ? "1" : "0",
    });
    if (requestToken !== groupRoleSyncToken) {
      return;
    }
    applyGroupList(groups || []);
  } catch (error) {
    if (requestToken === groupRoleSyncToken) {
      console.debug?.("Failed to sync group bot roles", error);
    }
  }
}

async function refreshGroupsListOnly() {
  const result = await api.safePost("settings/groups/refresh", { details: "0" });
  groupListError = result.refresh_error || null;
  applyGroupList(result.groups || []);
  if (groupListError) {
    showToast(groupListError, "error");
  }
  return groupListError;
}

async function calibrateGroups() {
  const result = await api.safePost("settings/groups/calibrate", {});
  applyGroupList(result.groups || []);
  const failed = Array.isArray(result.failed) ? result.failed : [];
  if (failed.length) {
    const names = failed
      .map((item) => item.group_name || item.group_id)
      .join("、")
      .slice(0, 120);
    showToast(`人数校准失败（${failed.length} 个群）：${names}`, "error");
  } else {
    showToast("人数已校准");
  }
  return failed;
}

async function loadGroupConfig(groupId, force = false) {
  const target = String(groupId || currentGroup?.group_id || DEFAULT_GROUP_ID).trim();
  if (!target) {
    showToast("先从左侧选择一个群", "error");
    return;
  }

  const data = await api.safeGet("settings/group", {
    group_id: target,
    force: force ? "1" : "0",
  });
  renderGroupForm(data);
  formDirty = false;
}

function bindFollowDefaultToggle() {
  const followDefaultInput = els.groupForm.querySelector(
    `[data-path="${FOLLOW_DEFAULT_KEY}"]`
  );
  if (!followDefaultInput) {
    return;
  }

  followDefaultInput.addEventListener("change", () => {
    if (!currentGroup?.config) {
      return;
    }
    const following = Boolean(followDefaultInput.checked);
    currentGroup.config[FOLLOW_DEFAULT_KEY] = following;
    // 轻量切换：仅翻转禁用态，不重建整个表单
    setFieldsDisabled(following);
    updateGroupActionState();
    updateActiveGroupCard();
  });
}

function getCurrentGroupFormPayload() {
  return collectFormData(els.groupForm);
}

async function persistGroupConfig(groupId, options = {}) {
  const {
    refreshList = true,
    rerenderCurrent = true,
    successMessage = "",
  } = options;
  const target = String(groupId || currentGroup?.group_id || "").trim();
  if (!target) {
    showToast("先加载群配置再保存", "error");
    return null;
  }
  const payload = getCurrentGroupFormPayload();
  const data = await api.safePost("settings/group", {
    group_id: target,
    config: payload,
  });
  if (rerenderCurrent) {
    renderGroupForm(data);
  }
  formDirty = false;
  if (refreshList) {
    await refreshGroupsListOnly();
  }
  if (successMessage) {
    showToast(successMessage);
  }
  return data;
}

async function saveCurrentGroupBeforeSwitch(nextGroupId) {
  const currentGroupId = String(currentGroup?.group_id || "").trim();
  const targetGroupId = String(nextGroupId || "").trim();
  if (!currentGroupId || !targetGroupId || currentGroupId === targetGroupId) {
    return;
  }
  if (!formDirty) {
    return;
  }
  if (!els.groupForm.querySelector("[data-path]")) {
    return;
  }

  await persistGroupConfig(currentGroupId, {
    refreshList: false,
    rerenderCurrent: false,
  });
}

async function switchGroup(groupId) {
  const target = String(groupId || "").trim();
  if (!target) {
    return;
  }

  await saveCurrentGroupBeforeSwitch(target);
  await loadGroupConfig(target);
}

async function saveGroupConfig() {
  const target = String(currentGroup?.group_id || "").trim();
  const data = await persistGroupConfig(target, {
    successMessage: `群 ${target} 配置已保存`,
  });
  return data;
}

async function resetGroupConfig() {
  const target = String(currentGroup?.group_id || "").trim();
  if (!target) {
    showToast("先加载群配置再重置", "error");
    return;
  }
  const data = await api.safePost("settings/group/reset", { group_id: target });
  renderGroupForm(data);
  formDirty = false;
  await refreshGroupsListOnly();
  showToast(`群 ${target} 已恢复默认群配置`);
}

function switchView(view) {
  const isGlobal = view === "global";
  currentView = view;

  els.workspaceGrid.classList.toggle("global-list-mode", isGlobal);
  els.globalListPanel.classList.toggle("is-hidden", !isGlobal);
  // 侧边栏按视图切换：群视图显示搜索+群导航，全局视图显示全局控制面板
  els.groupSearchWrap?.classList.toggle("is-hidden", isGlobal);
  els.groupListPanel.classList.toggle("is-hidden", isGlobal);
  els.globalSidePanel?.classList.toggle("is-hidden", !isGlobal);
  updateGlobalSideActions();
  els.groupForm.style.display = isGlobal ? "none" : "";
  els.groupActions.style.display = isGlobal ? "none" : "";
  els.currentGroupName.textContent = isGlobal
    ? "全局配置"
    : currentGroup?.group_info?.group_name || "未选择群";
  if (els.currentGroupMeta) {
    if (!isGlobal && currentGroup) {
      renderGroupDetailHeader(els, currentGroup);
    } else {
      els.currentGroupMeta.textContent = "";
    }
  }

  els.viewTabs.forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });

  if (isGlobal) {
    switchGlobalTab(currentGlobalType);
  }
}

function updateGlobalSideActions() {
  // 仅全局视图显示侧边栏操作按钮：黑白名单页签显示名单按钮，禁词页签显示禁词按钮
  const isListTab = currentGlobalType === "allow" || currentGlobalType === "block";
  const isBanWordsTab = currentGlobalType === "ban-words";
  els.globalListSideActions?.classList.toggle("is-hidden", currentView !== "global" || !isListTab);
  els.globalBanWordsSideActions?.classList.toggle("is-hidden", currentView !== "global" || !isBanWordsTab);
}

function switchGlobalTab(type) {
  const isBanWords = type === "ban-words";
  currentGlobalType = type;
  const titles = { allow: "全局白名单", block: "全局黑名单", "ban-words": "全局禁词" };
  els.currentGroupName.textContent = titles[type] || "全局配置";
  if (els.currentGroupMeta) {
    els.currentGroupMeta.textContent = "";
  }
  els.globalListContent.classList.toggle("is-hidden", isBanWords);
  els.globalBanWordsPanel.classList.toggle("is-hidden", !isBanWords);
  updateGlobalSideActions();
  // 切换页签时清空名单搜索
  globalListKeyword = "";
  if (els.globalListSearchInput) {
    els.globalListSearchInput.value = "";
  }
  if (isBanWords) {
    loadGlobalBanWords();
  } else {
    loadGlobalLists();
  }
}

async function loadGlobalBanWords() {
  try {
    globalBanWordsData = await api.safeGet("settings/global-ban-words");
    renderGlobalBanWords();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderItemList({ container, items, emptyText, checkClass = "", actionLabel, onAction, countText = "" }) {
  container.innerHTML = "";

  const count = document.createElement("div");
  count.className = "global-list-count";
  count.textContent = countText || `共 ${items.length} 个`;
  container.appendChild(count);

  const body = document.createElement("div");
  body.className = "global-list-scroll";
  container.appendChild(body);

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "global-list-empty";
    empty.textContent = emptyText;
    body.appendChild(empty);
    return;
  }

  if (checkClass) {
    const selectAll = document.createElement("label");
    selectAll.className = "ban-word-select-all";
    const selectAllInput = document.createElement("input");
    selectAllInput.type = "checkbox";
    selectAllInput.addEventListener("change", () => {
      container.querySelectorAll(`.${checkClass}`).forEach((input) => {
        input.checked = selectAllInput.checked;
      });
    });
    selectAll.append(selectAllInput, document.createTextNode("全选"));
    body.appendChild(selectAll);
  }

  const list = document.createElement("div");
  list.className = "global-list-rows";
  items.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "global-list-row";
    let label;
    if (checkClass) {
      label = document.createElement("label");
      label.className = "ban-word-row-label";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = item;
      input.className = checkClass;
      label.append(input, document.createTextNode(item));
    } else {
      label = document.createElement("span");
      label.className = "global-list-row-label";
      label.textContent = item;
    }
    const action = document.createElement("button");
    action.type = "button";
    action.className = "global-list-row-del";
    action.textContent = actionLabel;
    action.dataset.actionIndex = String(index);
    row.append(label, action);
    list.appendChild(row);
  });
  body.appendChild(list);
  // 事件委托：整表共用一个监听器，避免逐行绑定
  list.addEventListener("click", (e) => {
    const btn = e.target instanceof Element ? e.target.closest("[data-action-index]") : null;
    if (!btn) {
      return;
    }
    const index = Number(btn.dataset.actionIndex);
    onAction(items[index], index, items);
  });
}

function renderGlobalBanWords() {
  els.builtinBanWordsVersion.textContent = `当前版本：${globalBanWordsData.builtin_version || "未知"}`;
  renderItemList({
    container: els.globalBanWordsDisplay,
    items: globalBanWordsData.global || [],
    emptyText: "当前全局禁词为空。",
    checkClass: "global-ban-word-check",
    actionLabel: "删除",
    onAction: (word) => removeGlobalBanWord(word),
  });
  renderItemList({
    container: els.builtinBanWordsDisplay,
    items: globalBanWordsData.builtin || [],
    emptyText: "所有内置禁词均已导入。",
    checkClass: "builtin-ban-word-check",
    actionLabel: "导入",
    onAction: (word) => importBuiltinBanWord(word),
  });
}

function getBatchItems(input) {
  return [...new Set(
    input.value
      .split(/\n+/)
      .map((item) => item.trim())
      .filter(Boolean)
  )];
}

function clearBatchInput(input) {
  input.value = "";
}

async function saveGlobalBanWords(words, message) {
  globalBanWordsData.global = await api.safePost("settings/global-ban-words", { words });
  clearBatchInput(els.globalBanWordsBatchInput);
  await loadGlobalBanWords();
  showToast(message);
}

async function removeGlobalBanWord(word) {
  if (!(await showConfirm(`确定删除禁词“${word}”吗？`))) return;
  try {
    await saveGlobalBanWords(
      globalBanWordsData.global.filter((item) => item !== word),
      "全局禁词已删除"
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

function getCheckedValues(container, checkClass, emptyMessage) {
  const values = [...container.querySelectorAll(`.${checkClass}:checked`)]
    .map((input) => input.value);
  if (!values.length) {
    showToast(emptyMessage, "error");
    return null;
  }
  return values;
}

async function removeSelectedGlobalBanWords() {
  const words = getCheckedValues(els.globalBanWordsDisplay, "global-ban-word-check", "请先选择全局禁词");
  if (!words) {
    return;
  }
  if (!(await showConfirm(`确定删除选中的 ${words.length} 个禁词吗？`))) return;
  try {
    const removed = new Set(words);
    await saveGlobalBanWords(
      globalBanWordsData.global.filter((word) => !removed.has(word)),
      `已删除 ${words.length} 个全局禁词`
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function importBuiltinBanWord(word) {
  try {
    globalBanWordsData.global = await api.safePost("settings/global-ban-words/import", { words: [word] });
    await loadGlobalBanWords();
    showToast("内置禁词已导入");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function importSelectedBanWords() {
  const words = getCheckedValues(els.builtinBanWordsDisplay, "builtin-ban-word-check", "请先选择内置禁词");
  if (!words) {
    return;
  }
  try {
    await api.safePost("settings/global-ban-words/import", { words });
    await loadGlobalBanWords();
    showToast(`已导入 ${words.length} 个内置禁词`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function appendGlobalBanWords() {
  const words = getBatchItems(els.globalBanWordsBatchInput);
  if (!words.length) {
    showToast("输入框为空", "error");
    return;
  }
  await saveGlobalBanWords([...globalBanWordsData.global, ...words], "全局禁词已添加");
}

async function overwriteGlobalBanWords() {
  if (!(await showConfirm("确定覆写全局禁词吗？"))) return;
  await saveGlobalBanWords(getBatchItems(els.globalBanWordsBatchInput), "全局禁词已覆写");
}

async function restoreBuiltinBanWords() {
  if (!(await showConfirm("确定以当前内置禁词覆盖全局禁词吗？"))) return;
  try {
    globalBanWordsData.global = await api.safePost("settings/global-ban-words/restore", {});
    await loadGlobalBanWords();
    showToast("已恢复内置禁词");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function loadGlobalLists() {
  try {
    const data = await api.safeGet("settings/global-list");
    globalListData = data;
    renderGlobalList();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderGlobalList() {
  const all = globalListData[currentGlobalType] || [];
  const keyword = globalListKeyword.trim();
  const shown = all
    .map((uid, index) => ({ uid, index }))
    .filter((entry) => !keyword || String(entry.uid).includes(keyword));
  renderItemList({
    container: els.globalListDisplay,
    items: shown.map((entry) => entry.uid),
    emptyText: keyword ? "没有匹配的QQ号。" : "当前名单为空。",
    countText: keyword ? `共 ${all.length} 个 · 筛选出 ${shown.length} 个` : "",
    checkClass: "global-list-check",
    actionLabel: "删除",
    onAction: async (uid, shownIndex) => {
      const ok = await showConfirm(`确定删除 ${uid} 吗？`);
      if (!ok) return;
      const next = all.filter((_, i) => i !== shown[shownIndex].index);
      try {
        await api.safePost("settings/global-list", { type: currentGlobalType, items: next });
        globalListData[currentGlobalType] = next;
        renderGlobalList();
        showToast("已删除");
      } catch (error) {
        showToast(error.message, "error");
      }
    },
  });
}

async function removeSelectedGlobalList() {
  const values = getCheckedValues(els.globalListDisplay, "global-list-check", "请先选择要删除的QQ号");
  if (!values) {
    return;
  }
  const ok = await showConfirm(`确定删除选中的 ${values.length} 个QQ号吗？`);
  if (!ok) {
    return;
  }
  const del = new Set(values);
  const next = (globalListData[currentGlobalType] || []).filter((uid) => !del.has(uid));
  try {
    await api.safePost("settings/global-list", { type: currentGlobalType, items: next });
    globalListData[currentGlobalType] = next;
    renderGlobalList();
    showToast(`已删除 ${values.length} 个`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

function showConfirm(message) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) {
        overlay.remove();
        resolve(false);
      }
    });

    const box = document.createElement("div");
    box.className = "confirm-box";

    const msg = document.createElement("p");
    msg.className = "confirm-message";
    msg.textContent = message;

    const actions = document.createElement("div");
    actions.className = "confirm-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "ghost-button";
    cancelBtn.textContent = "取消";
    cancelBtn.addEventListener("click", () => {
      overlay.remove();
      resolve(false);
    });

    const confirmBtn = document.createElement("button");
    confirmBtn.className = "primary-button";
    confirmBtn.textContent = "确定";
    confirmBtn.addEventListener("click", () => {
      overlay.remove();
      resolve(true);
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(msg);
    box.appendChild(actions);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  });
}

async function overwriteGlobalList() {
  const items = getBatchItems(els.globalListBatchInput);
  const ok = await showConfirm(`确定覆写全局${currentGlobalType === "allow" ? "白名单" : "黑名单"}吗？`);
  if (!ok) return;
  try {
    await api.safePost("settings/global-list", {
      type: currentGlobalType,
      items,
    });
    globalListData[currentGlobalType] = items;
    clearBatchInput(els.globalListBatchInput);
    renderGlobalList();
    showToast(`全局${currentGlobalType === "allow" ? "白名单" : "黑名单"}已覆写`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function appendGlobalList() {
  const batchItems = getBatchItems(els.globalListBatchInput);
  if (!batchItems.length) {
    showToast("输入框为空", "error");
    return;
  }
  const existingItems = globalListData[currentGlobalType] || [];
  const existingSet = new Set(existingItems);
  const newItems = batchItems.filter((item) => !existingSet.has(item));
  if (!newItems.length) {
    showToast("所有数据均已存在，无需添加", "error");
    return;
  }
  const merged = [...existingItems, ...newItems];
  try {
    await api.safePost("settings/global-list", {
      type: currentGlobalType,
      items: merged,
    });
    globalListData[currentGlobalType] = merged;
    clearBatchInput(els.globalListBatchInput);
    renderGlobalList();
    const skipped = batchItems.length - newItems.length;
    const msg = skipped > 0 ? `已添加 ${newItems.length} 个（${skipped} 个重复已跳过）` : `已添加 ${newItems.length} 个`;
    showToast(msg);
  } catch (error) {
    showToast(error.message, "error");
  }
}

function bindEvents() {
  els.toggleThemeBtn.addEventListener("click", () => {
    themeController?.cyclePreference();
  });

  els.refreshGroupsBtn.addEventListener("click", async () => {
    if (els.refreshGroupsBtn.disabled) {
      return;
    }
    els.refreshGroupsBtn.disabled = true;
    const done = () => {
      els.refreshGroupsBtn.disabled = false;
    };
    try {
      const refreshError = await refreshGroupsListOnly();
      scheduleGroupRoleSync({ force: true, prune: false });
      // 全局视图下不重载群配置，避免群名覆盖全局标题
      if (currentView === "group" && currentGroup?.group_id) {
        await loadGroupConfig(currentGroup.group_id);
      }
      if (refreshError || !allGroups.some((group) => !group.is_default_group)) {
        done();
        return;
      }
      showToast("群列表已同步，人数校准中…");
      calibrateGroups()
        .catch((error) => showToast(error.message, "error"))
        .finally(done);
    } catch (error) {
      done();
      showToast(error.message, "error");
    }
  });

  els.saveGroupBtn.addEventListener("click", async () => {
    try {
      await saveGroupConfig();
    } catch (error) {
      showToast(error.message, "error");
    }
  });

  els.resetGroupBtn.addEventListener("click", async () => {
    try {
      if (currentGroup?.is_default_group) {
        showToast("默认群模板不支持重置", "error");
        return;
      }
      if (currentGroup?.config?.[FOLLOW_DEFAULT_KEY]) {
        showToast("当前群正在跟随默认配置，无需重置", "error");
        return;
      }
      await resetGroupConfig();
    } catch (error) {
      showToast(error.message, "error");
    }
  });

  let groupSearchTimer = null;
  els.groupSearchInput.addEventListener("input", () => {
    clearTimeout(groupSearchTimer);
    groupSearchTimer = setTimeout(filterAndRenderGroups, 150);
  });

  let globalListSearchTimer = null;
  els.globalListSearchInput?.addEventListener("input", () => {
    clearTimeout(globalListSearchTimer);
    globalListSearchTimer = setTimeout(() => {
      globalListKeyword = els.globalListSearchInput.value;
      renderGlobalList();
    }, 150);
  });

  els.groupForm.addEventListener("input", markFormDirty);
  els.groupForm.addEventListener("change", markFormDirty);

  els.viewTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      switchView(tab.dataset.view);
    });
  });

  els.globalListTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      els.globalListTabs.forEach((t) => {
        const active = t === tab;
        t.classList.toggle("is-active", active);
        t.setAttribute("aria-selected", String(active));
      });
      switchGlobalTab(tab.dataset.globalType);
    });
  });

  els.overwriteGlobalListBtn.addEventListener("click", async () => {
    try {
      await overwriteGlobalList();
    } catch (error) {
      showToast(error.message, "error");
    }
  });

  els.appendGlobalListBtn.addEventListener("click", async () => {
    try {
      await appendGlobalList();
    } catch (error) {
      showToast(error.message, "error");
    }
  });

  els.deleteSelectedListBtn.addEventListener("click", async () => {
    try {
      await removeSelectedGlobalList();
    } catch (error) {
      showToast(error.message, "error");
    }
  });

  els.appendGlobalBanWordsBtn.addEventListener("click", () => {
    appendGlobalBanWords().catch((error) => showToast(error.message, "error"));
  });
  els.overwriteGlobalBanWordsBtn.addEventListener("click", () => {
    overwriteGlobalBanWords().catch((error) => showToast(error.message, "error"));
  });
  els.deleteSelectedBanWordsBtn.addEventListener("click", removeSelectedGlobalBanWords);
  els.restoreBuiltinBanWordsBtn.addEventListener("click", restoreBuiltinBanWords);
  els.importSelectedBanWordsBtn.addEventListener("click", importSelectedBanWords);
}

async function init() {
  themeController = createThemeController({
    getContext: () => bridge?.getContext?.(),
    onModeChange: updateThemeButton,
  });
  themeController.bind();
  updateThemeButton();
  themeController.sync(null);

  if (!bridge) {
    return;
  }

  try {
    api = createApi(bridge);
  } catch (error) {
    return;
  }

  try {
    if (typeof bridge.ready === "function") {
      const context = await Promise.race([
        bridge.ready(),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error("Bridge ready timeout")), 5000)
        ),
      ]);
      themeController.sync(context);
    }

    if (typeof bridge.onContext === "function") {
      detachContextHandler = bridge.onContext((context) => {
        themeController.sync(context);
      });
    } else {
      themeController.sync(bridge.getContext?.());
    }

    bindEvents();
    await loadBootstrapData();
    scheduleGroupRoleSync();
    await loadGroupConfig(DEFAULT_GROUP_ID);
  } catch (error) {
    const message = error?.message || "页面初始化失败";
    showToast(message, "error");
  }
}

window.addEventListener("beforeunload", () => {
  detachContextHandler?.();
  themeController?.detach?.();
});

init();
