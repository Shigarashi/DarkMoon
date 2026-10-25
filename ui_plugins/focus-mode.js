/**
 * @name Focus Mode
 * @author Darkmoon
 * @description Header button (and Ctrl+Shift+F) that hides everything except the chat.
 * @version 1.0
 */
Darkmoon.plugins.register({
  name: "Focus Mode",
  start(api) {
    api.injectCSS(`body.la-focus #sidebar,body.la-focus #topbar>*:not(#header-plugins){display:none!important}
      body.la-focus #app{grid-template-columns:0 1fr}
      body.la-focus #topbar{border:0;background:transparent;justify-content:flex-end}`);
    const toggle = () => document.body.classList.toggle("la-focus");
    api.addHeaderButton({ icon: "◎", title: "Focus mode (Ctrl+Shift+F)", onClick: toggle });
    this.key = (e) => { if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "f") { e.preventDefault(); toggle(); } };
    document.addEventListener("keydown", this.key);
  },
  stop() { document.removeEventListener("keydown", this.key); document.body.classList.remove("la-focus"); },
});
