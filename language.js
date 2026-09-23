// Follow macOS by default; all language controls share the same user override.
(() => {
  const selects = [...document.querySelectorAll("#uiLanguage, #welcomeLanguage")];
  if (!selects.length) return;
  let previous = "system";
  try { previous = localStorage.getItem("notesOrganizerLanguage") || "system"; } catch {}
  for (const select of selects) {
    select.value = previous;
    select.addEventListener("change", () => {
      if (typeof guardDirty === "function" && !guardDirty()) { select.value = previous; return; }
      previous = select.value;
      for (const peer of selects) peer.value = previous;
      try { localStorage.setItem("notesOrganizerLanguage", previous); } catch {}
      // Direct language links still work when browser storage is unavailable.
      location.href = previous === "en" ? "app.en.html" : previous === "zh" ? "app.zh-CN.html" : "app.html";
    });
  }
})();
