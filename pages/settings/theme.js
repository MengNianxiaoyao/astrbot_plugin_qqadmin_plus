const THEME_STORAGE_KEY = "qqadmin-page-theme-mode";

function loadPreference() {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "auto") {
      return stored;
    }
  } catch {
    /* ignore */
  }
  return "auto";
}

function persistPreference(value) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, value);
  } catch {
    /* ignore */
  }
}

export function createThemeController({ getContext, onModeChange }) {
  const themeMediaQuery =
    typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: dark)")
      : null;

  let preference = loadPreference();
  let effectiveMode = null;
  let detachSystem = null;

  // Dashboard 通过 context 下发主题：新版用 isDark，旧版用 theme 字符串，两者都兼容
  const bridgeMode = (context) => {
    if (!context || typeof context !== "object") {
      return null;
    }
    if (typeof context.isDark === "boolean") {
      return context.isDark ? "dark" : "light";
    }
    return context.theme === "dark" || context.theme === "light"
      ? context.theme
      : null;
  };

  const systemMode = () => (themeMediaQuery?.matches ? "dark" : "light");

  function resolveMode(context) {
    if (preference === "dark" || preference === "light") {
      return preference;
    }
    // 自动模式只认 Dashboard 下发的主题信号；推送里没有可用信号时返回 null，
    // 由 sync 保持当前主题，而不是回退到系统主题（系统一般是浅色，会把深色面板洗白）。
    return bridgeMode(context);
  }

  function apply(mode) {
    effectiveMode = mode;
    document.documentElement.dataset.theme = mode;
    document.documentElement.style.colorScheme = mode;
  }

  function sync(context) {
    // 无信号且已有主题时保持不动；首屏（还未应用过任何主题）才用系统主题兜底。
    const mode =
      resolveMode(context) || (effectiveMode === null ? systemMode() : effectiveMode);
    apply(mode);
    onModeChange?.();
  }

  function onSystemChange() {
    if (preference === "auto") {
      sync(getContext());
    }
  }

  function bind() {
    if (!themeMediaQuery) {
      return;
    }
    if (typeof themeMediaQuery.addEventListener === "function") {
      themeMediaQuery.addEventListener("change", onSystemChange);
      detachSystem = () =>
        themeMediaQuery.removeEventListener("change", onSystemChange);
    } else if (typeof themeMediaQuery.addListener === "function") {
      themeMediaQuery.addListener(onSystemChange);
      detachSystem = () => themeMediaQuery.removeListener(onSystemChange);
    }
  }

  function cycle() {
    preference =
      preference === "auto" ? "dark" :
      preference === "dark" ? "light" :
      "auto";
    persistPreference(preference);
    sync(getContext());
  }

  return {
    bind,
    sync,
    cyclePreference: cycle,
    detach() {
      detachSystem?.();
    },
    getButtonLabel() {
      return effectiveMode === "dark" ? "主题：深色" : "主题：浅色";
    },
  };
}