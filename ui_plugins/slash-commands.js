/**
 * @name Fun Slash Commands
 * @author Darkmoon
 * @description Adds /shrug, /tableflip, /roll, /clear-input and /summarize.
 * @version 1.0
 */
Darkmoon.plugins.register({
  name: "Fun Slash Commands",
  start(api) {
    api.addSlashCommand({ name: "shrug", description: "Append ¯\\_(ツ)_/¯ and send", run: (t) => api.send(`${t} ¯\\_(ツ)_/¯`.trim()) });
    api.addSlashCommand({ name: "tableflip", description: "(╯°□°)╯︵ ┻━┻", run: (t) => api.send(`${t} (╯°□°)╯︵ ┻━┻`.trim()) });
    api.addSlashCommand({ name: "roll", description: "Roll dice, e.g. /roll 2d6", run: (t) => {
      const [, n = 1, s = 6] = (t.match(/(\d*)d(\d+)/) || []).map(Number);
      const rolls = Array.from({ length: n || 1 }, () => 1 + Math.floor(Math.random() * s));
      api.toast(`🎲 ${rolls.join(" + ")} = ${rolls.reduce((a, b) => a + b, 0)}`);
    } });
    api.addSlashCommand({ name: "summarize", description: "Ask for a summary of this chat", run: () => api.send("Summarize our conversation so far in 5 bullet points.") });
  },
});
