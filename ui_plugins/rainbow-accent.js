/**
 * @name Rainbow Accent
 * @author Darkmoon
 * @description Slowly cycles the accent color through the rainbow. Purely for fun.
 * @version 1.0
 */
Darkmoon.plugins.register({
  name: "Rainbow Accent",
  start(api) {
    const style = document.createElement("style");
    document.head.append(style);
    let hue = 0;
    this.timer = setInterval(() => {
      hue = (hue + 1) % 360;
      style.textContent = `:root{--accent:hsl(${hue} 85% 65%)!important;--accent-2:hsl(${(hue + 60) % 360} 85% 60%)!important}`;
    }, 80);
    this.style = style;
  },
  stop() { clearInterval(this.timer); this.style?.remove(); },
});
