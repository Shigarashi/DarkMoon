/**
 * @name Message Timestamps
 * @author Darkmoon
 * @description Shows the time each message was sent.
 * @version 1.0
 */
Darkmoon.plugins.register({
  name: "Message Timestamps",
  start(api) {
    api.injectCSS(`.la-ts{font-size:11px;color:var(--text-faint);margin-top:3px}
      .msg.user .la-ts{text-align:right}`);
    api.onMessageRendered(({ el, created }) => {
      if (el.querySelector(".la-ts")) return;
      const d = created ? new Date(created * 1000) : new Date();
      const ts = document.createElement("div");
      ts.className = "la-ts";
      ts.textContent = d.toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
      el.querySelector(".body").append(ts);
    });
  },
  stop() { document.querySelectorAll(".la-ts").forEach((e) => e.remove()); },
});
