const ROLE_BADGE_META = {
  owner: { icon: "主", text: "群主" },
  admin: { icon: "管", text: "管理员" },
  member: { icon: "员", text: "成员" },
  unknown: { icon: "？", text: "未知" },
};

function buildGroupRoleBadge(group) {
  // 默认群是配置模板，没有身份概念，不出徽标
  if (group?.is_default_group) {
    return null;
  }
  const role = String(group?.bot_role || "").toLowerCase();
  const meta = ROLE_BADGE_META[role];
  if (!meta) {
    return null;
  }

  const badge = document.createElement("span");
  badge.className = `group-role-badge ${role}`;

  const icon = document.createElement("span");
  icon.className = `group-role-icon ${role}`;
  icon.textContent = meta.icon;
  badge.appendChild(icon);

  const text = document.createElement("span");
  text.textContent = meta.text;
  badge.appendChild(text);

  return badge;
}

function formatMemberText(group) {
  const member = Number(group?.member_count) || 0;
  const max = Number(group?.max_member_count) || 0;
  if (max > 0) {
    return `${member}/${max} 人`;
  }
  if (member > 0) {
    return `${member} 人`;
  }
  return "";
}

export function renderGroupCards({
  root,
  groups,
  currentGroupId,
  onSelect,
  emptyText = "当前没有可显示的群。",
}) {
  root.innerHTML = "";

  if (!groups.length) {
    root.classList.add("empty-state");
    root.textContent = emptyText;
    return;
  }

  root.classList.remove("empty-state");

  const fragment = document.createDocumentFragment();

  groups.forEach((group) => {
    const card = document.createElement("article");
    card.className = "group-card";
    card.dataset.groupId = String(group.group_id ?? "");
    if (group.group_id === currentGroupId) {
      card.classList.add("is-active");
    }

    const avatar = document.createElement("img");
    avatar.className = "group-card-avatar";
    avatar.src =
      group.avatar ||
      "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96' viewBox='0 0 96 96'><rect width='96' height='96' rx='24' fill='%23e8c49a'/><text x='48' y='56' text-anchor='middle' font-size='34' fill='%23824f1f' font-family='Arial'>D</text></svg>";
    avatar.alt = `${group.group_name} 群头像`;
    avatar.loading = "lazy";
    avatar.decoding = "async";
    card.appendChild(avatar);

    const main = document.createElement("div");
    main.className = "group-card-main";

    const title = document.createElement("div");
    title.className = "group-card-title";

    const name = document.createElement("div");
    name.className = "group-card-name";
    name.textContent = group.group_name || `群 ${group.group_id}`;
    title.appendChild(name);

    const roleBadge = buildGroupRoleBadge(group);
    if (roleBadge) {
      title.appendChild(roleBadge);
    }

    main.appendChild(title);

    const subline = document.createElement("div");
    subline.className = "group-card-subline";
    if (group.is_default_group) {
      subline.innerHTML = `
        <span class="group-card-id">默认模板</span>
        <span>新群继承这里的配置</span>
      `;
    } else {
      const memberText = formatMemberText(group);
      subline.innerHTML = `
        <span class="group-card-id">${group.group_id}</span>
        ${memberText ? `<span>${memberText}</span>` : ""}
      `;
    }
    main.appendChild(subline);

    card.appendChild(main);

    fragment.appendChild(card);
  });

  // 事件委托：整表共用一个点击监听器，避免逐卡绑定
  root.onclick = (e) => {
    const card = e.target instanceof Element ? e.target.closest(".group-card") : null;
    if (!card || !root.contains(card)) {
      return;
    }
    onSelect?.(card.dataset.groupId);
  };

  root.appendChild(fragment);
}

export function renderGroupDetailHeader(els, payload) {
  const info = payload.group_info || {};
  els.currentGroupName.textContent = info.group_name || `群 ${payload.group_id}`;
  if (els.currentGroupMeta) {
    const memberText = formatMemberText(info);
    const groupId = payload.group_id && !payload.is_default_group ? `群号 ${payload.group_id}` : "";
    els.currentGroupMeta.textContent = [groupId, memberText ? `人数 ${memberText}` : ""].filter(Boolean).join(" · ");
  }
}
