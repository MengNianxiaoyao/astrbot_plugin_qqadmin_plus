function pathJoin(prefix, key) {
  return prefix ? `${prefix}.${key}` : key;
}

function normalizeOptions(options = {}) {
  const raw = options.collapsedObjectPaths;
  const rawExpanded = options.expandedObjectPaths;
  return {
    ...options,
    collapsedObjectPaths: raw instanceof Set ? raw : new Set(raw),
    expandedObjectPaths: rawExpanded instanceof Set ? rawExpanded : new Set(rawExpanded),
  };
}

function isDisabledPath(path, options = {}) {
  if (typeof options.isFieldDisabled !== "function") {
    return false;
  }
  return Boolean(options.isFieldDisabled(path));
}

function setByPath(target, path, value) {
  const parts = path.split(".");
  let cursor = target;

  parts.forEach((part, index) => {
    if (index === parts.length - 1) {
      cursor[part] = value;
      return;
    }
    if (!cursor[part] || typeof cursor[part] !== "object") {
      cursor[part] = {};
    }
    cursor = cursor[part];
  });
}

function buildCollapseShell(titleText, titleClassName = "section-title") {
  const wrapper = document.createElement("section");
  wrapper.className = "form-object is-collapsible";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "form-object-toggle";

  const copy = document.createElement("span");
  copy.className = "form-object-toggle-copy section-head";

  const title = document.createElement("span");
  title.className = titleClassName;
  title.textContent = titleText;
  copy.appendChild(title);

  const action = document.createElement("span");
  action.className = "form-object-toggle-action";

  const body = document.createElement("div");
  body.className = "form-object-body";

  toggle.append(copy, action);
  wrapper.append(toggle, body);
  return { wrapper, toggle, copy, action, body };
}

function bindCollapseToggle(shell, isCollapsed, onToggle) {
  const syncCollapsedState = () => {
    const collapsed = isCollapsed();
    shell.wrapper.classList.toggle("is-collapsed", collapsed);
    shell.toggle.setAttribute("aria-expanded", String(!collapsed));
    shell.action.textContent = collapsed ? "展开" : "收起";
  };
  shell.toggle.addEventListener("click", () => {
    onToggle();
    syncCollapsedState();
  });
  syncCollapsedState();
}

function buildField(path, key, schema, value, options = {}) {
  const type = schema.type || "string";
  const disabled = isDisabledPath(path, options);

  if (type === "object") {
    const isCollapsible =
      options.collapsedObjectPaths.has(path) || options.expandedObjectPaths.has(path);
    if (isCollapsible) {
      // 对象即独立设置组：标题使用组级高亮样式
      const shell = buildCollapseShell(schema.description || key, "section-title section-group-title");
      if (schema.hint) {
        const hint = document.createElement("span");
        hint.className = "section-hint";
        hint.textContent = schema.hint;
        shell.copy.appendChild(hint);
      }
      shell.toggle.disabled = disabled;
      if (!objectCollapsedState.has(path)) {
        objectCollapsedState.set(path, !options.expandedObjectPaths.has(path));
      }
      let collapsed = objectCollapsedState.get(path);
      bindCollapseToggle(shell, () => collapsed, () => {
        collapsed = !collapsed;
        objectCollapsedState.set(path, collapsed);
      });
      shell.body.appendChild(buildChildGrid(schema, value, path, options));
      if (disabled) {
        shell.wrapper.classList.add("is-disabled");
      }
      return shell.wrapper;
    }

    const wrapper = document.createElement("section");
    wrapper.className = "form-object";
    if (disabled) {
      wrapper.classList.add("is-disabled");
    }
    const header = document.createElement("div");
    header.className = "section-head";

    const title = document.createElement("div");
    title.className = "section-title";
    title.textContent = schema.description || key;
    header.appendChild(title);

    if (schema.hint) {
      const hint = document.createElement("div");
      hint.className = "section-hint";
      hint.textContent = schema.hint;
      header.appendChild(hint);
    }

    wrapper.appendChild(header);
    wrapper.appendChild(buildChildGrid(schema, value, path, options));
    return wrapper;
  }

  const field = document.createElement("label");
  field.className = "field";
  if (type === "bool") {
    field.classList.add("checkbox-field");
  }
  if (disabled) {
    field.classList.add("is-disabled");
  }

  const copy = document.createElement("div");
  copy.className = "field-copy";

  const label = document.createElement("div");
  label.className = "field-label";
  label.textContent = schema.description || key;
  copy.appendChild(label);

  if (schema.hint) {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent = schema.hint;
    copy.appendChild(hint);
  }

  field.appendChild(copy);

  const control = document.createElement("div");
  control.className = "field-control";

  let input;
  if (type === "bool") {
    const shell = document.createElement("span");
    shell.className = "switch";
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    const slider = document.createElement("span");
    slider.className = "slider";
    shell.appendChild(input);
    shell.appendChild(slider);
    control.appendChild(shell);
  } else if ((type === "string" || type === "text") && schema.options?.length) {
    // 仅字符串类型支持单选下拉；list+options 走下面的复选框组
    input = document.createElement("select");
    schema.options.forEach((option) => {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = option;
      if (String(value ?? schema.default ?? "") === option) {
        node.selected = true;
      }
      input.appendChild(node);
    });
    control.appendChild(input);
  } else if (type === "int") {
    input = document.createElement("input");
    input.type = "number";
    input.value = String(value ?? schema.default ?? 0);
    if (schema.slider) {
      if (schema.slider.min !== undefined) input.min = schema.slider.min;
      if (schema.slider.max !== undefined) input.max = schema.slider.max;
      if (schema.slider.step !== undefined) input.step = schema.slider.step;
    }
    control.appendChild(input);
  } else if (type === "list") {
    if (schema.options?.length) {
      // 带固定选项的列表渲染为复选框组
      const selected = new Set(
        (Array.isArray(value) ? value : []).map((item) => String(item))
      );
      const group = document.createElement("div");
      group.className = "check-group";
      schema.options.forEach((option) => {
        const optionValue = String(option?.value ?? option);
        const optionLabel = option?.label ?? option?.description ?? optionValue;
        const label = document.createElement("label");
        label.className = "check-item";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = optionValue;
        box.checked = selected.has(optionValue);
        box.dataset.path = path;
        box.dataset.type = "multiselect";
        box.disabled = disabled;
        label.append(box, document.createTextNode(optionLabel));
        group.appendChild(label);
      });
      control.appendChild(group);
      input = null;
    } else {
      input = document.createElement("textarea");
      input.value = Array.isArray(value) ? value.join("\n") : "";
      input.placeholder = "每行一个条目，也支持粘贴后分行整理";
      control.appendChild(input);
    }
  } else {
    const multiline =
      type === "text" ||
      String(value || "").includes("\n") ||
      key.includes("welcome");
    input = document.createElement(multiline ? "textarea" : "input");
    if (!multiline) {
      input.type = "text";
    }
    input.value = String(value ?? schema.default ?? "");
    control.appendChild(input);
  }

  if (input) {
    input.dataset.path = path;
    input.dataset.type = type;
    input.disabled = disabled;
  }
  field.appendChild(control);
  return field;
}

// 分组折叠状态（页面内保持，切换群不丢失）
const collapsedSections = new Set();

// 对象字段折叠状态（页面内保持，重渲染/切换群不丢失；首次取传入的 collapsed/expanded 默认值）
const objectCollapsedState = new Map();

function buildFieldsGrid(entries, values, normalizedOptions) {
  const grid = document.createElement("div");
  grid.className = "field-grid";
  if (normalizedOptions.singleColumn) {
    grid.classList.add("single-column");
  }
  entries.forEach(({ path, key, schema: fieldSchema }) => {
    grid.appendChild(
      buildField(
        path,
        key,
        fieldSchema,
        values?.[key] ?? fieldSchema.default,
        normalizedOptions
      )
    );
  });
  return grid;
}

function buildChildGrid(schema, value, path, options) {
  return buildFieldsGrid(
    Object.entries(schema.items || {}).map(([childKey, childSchema]) => ({
      path: pathJoin(path, childKey),
      key: childKey,
      schema: childSchema,
    })),
    value,
    options
  );
}

function buildCollapsibleSection(section, entries, values, normalizedOptions) {
  const shell = buildCollapseShell(section, "section-title section-group-title");
  const hintText = normalizedOptions.groups?.[section]?.hint;
  if (hintText) {
    const hint = document.createElement("span");
    hint.className = "section-hint";
    hint.textContent = hintText;
    shell.copy.appendChild(hint);
  }
  let collapsed = collapsedSections.has(section);
  bindCollapseToggle(shell, () => collapsed, () => {
    collapsed = !collapsed;
    if (collapsed) {
      collapsedSections.add(section);
    } else {
      collapsedSections.delete(section);
    }
  });
  shell.body.appendChild(buildFieldsGrid(entries, values, normalizedOptions));
  return shell.wrapper;
}

export function renderSchemaFields(root, schema, values, options = {}) {
  const normalizedOptions = normalizeOptions(options);
  root.innerHTML = "";

  const fragment = document.createDocumentFragment();
  const leadingKeys = new Set(normalizedOptions.leadingPaths || []);
  const groupTable = normalizedOptions.groups || {};

  // 置顶字段（如跟随开关）最先渲染
  const leadingEntries = [];
  // 按分组表归组（表内顺序即展示顺序）；表外字段沉底直接渲染
  const memberOf = {};
  Object.keys(groupTable).forEach((name) => {
    ((groupTable[name] || {}).items || []).forEach((key) => {
      if (!(key in memberOf)) {
        memberOf[key] = name;
      }
    });
  });
  const groups = new Map();
  const leftovers = [];
  Object.entries(schema).forEach(([key, fieldSchema]) => {
    if (leadingKeys.has(key)) {
      leadingEntries.push([key, fieldSchema]);
      return;
    }
    const section = memberOf[key];
    if (!section) {
      leftovers.push([key, fieldSchema]);
      return;
    }
    if (!groups.has(section)) {
      groups.set(section, []);
    }
    groups.get(section).push([key, fieldSchema]);
  });

  const toEntries = (list) =>
    list.map(([key, fieldSchema]) => ({ path: key, key, schema: fieldSchema }));

  if (leadingEntries.length) {
    fragment.appendChild(buildFieldsGrid(toEntries(leadingEntries), values, normalizedOptions));
  }
  Object.keys(groupTable).forEach((section) => {
    const entries = groups.get(section);
    if (!entries || !entries.length) {
      return;
    }
    // 单对象成组：直接渲染对象，标题改用组名（用对象自带的 hint，避免重复）
    if (entries.length === 1 && entries[0][1]?.type === "object") {
      const [key, fieldSchema] = entries[0];
      fragment.appendChild(
        buildField(
          key,
          key,
          { ...fieldSchema, description: section },
          values?.[key] ?? fieldSchema.default,
          normalizedOptions
        )
      );
      return;
    }
    fragment.appendChild(
      buildCollapsibleSection(section, toEntries(entries), values, normalizedOptions)
    );
  });
  if (leftovers.length) {
    fragment.appendChild(buildFieldsGrid(toEntries(leftovers), values, normalizedOptions));
  }

  root.appendChild(fragment);
}

function getByPath(target, path) {
  const parts = path.split(".");
  let cursor = target;
  for (const part of parts) {
    if (!cursor || typeof cursor !== "object") {
      return undefined;
    }
    cursor = cursor[part];
  }
  return cursor;
}

export function collectFormData(root) {
  const payload = {};
  // 收集所有字段（含禁用字段），使 payload 与页面展示的配置完全一致（所见即所得），
  // 不依赖后端根据 missing 字段回填来还原配置，从而与禁用态解耦。
  root.querySelectorAll("[data-path]").forEach((node) => {
    const { path, type } = node.dataset;
    let value;

    if (type === "bool") {
      value = node.checked;
    } else if (type === "int") {
      const parsed = Number(node.value);
      value = Number.isNaN(parsed) ? 0 : parsed;
    } else if (type === "list") {
      value = node.value
        .split(/\n+/)
        .map((item) => item.trim())
        .filter(Boolean);
    } else if (type === "multiselect") {
      // 同 path 的复选框累积为数组；先建空数组，保证全不选时也能提交 []
      let arr = getByPath(payload, path);
      if (!Array.isArray(arr)) {
        arr = [];
        setByPath(payload, path, arr);
      }
      if (node.checked) {
        arr.push(node.value);
      }
      return; // forEach 回调内用 return 跳过，不能用 continue
    } else {
      value = node.value;
    }

    setByPath(payload, path, value);
  });
  return payload;
}
