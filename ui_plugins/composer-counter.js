/**
 * @name Composer Counter
 * @author Darkmoon
 * @description Live word / character count and a rough token estimate in the message box.
 * @version 1.0
 */
Darkmoon.plugins.register({
  name: "Composer Counter",
  start(api) {
    const el = document.createElement("span");
    api.addComposerWidget(el);
    const update = (v) => {
      v = v ?? api.getInput();
      const words = (v.match(/\S+/g) || []).length;
      el.textContent = v ? `${words}w · ${v.length}c · ~${Math.ceil(v.length / 4)} tok` : "";
    };
    api.on("input", update);
    api.on("send", () => update(""));
  },
});
