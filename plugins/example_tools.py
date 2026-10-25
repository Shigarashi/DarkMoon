"""Example tool plugin — copy this file into your tool plugins folder to start.

Your folder: Settings > Tools > "Open tool plugins folder"
  (Windows: %APPDATA%\\Darkmoon\\plugins)
After editing, click "Reload tool plugins" — no restart needed.

Rules:
  * Decorate a function with @tool. Type hints become the JSON schema.
  * The docstring's first paragraph is the description the model sees;
    an "Args:" section documents each parameter.
  * Add `ctx` as the first parameter to get the workspace, config and a
    path resolver that respects the workspace sandbox (ctx.path("x.txt")).
  * Use approval="write" | "shell" | "python" to require a confirmation click.
  * Return a string (or anything JSON-serializable). Sync or async both work.
"""
from __future__ import annotations

import json
import random
import urllib.parse
import urllib.request

from app.tools import tool


@tool
def roll_dice(sides: int = 6, count: int = 1) -> str:
    """Roll dice and return the results.

    Args:
        sides: number of sides per die
        count: how many dice to roll
    """
    rolls = [random.randint(1, max(2, sides)) for _ in range(max(1, min(count, 100)))]
    return f"Rolled {rolls} (total {sum(rolls)})"


@tool
def get_weather(city: str) -> str:
    """Current weather for a city (free wttr.in service, no API key).

    Args:
        city: city name, e.g. "Seattle"
    """
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
    with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310
        data = json.load(r)
    cur = data["current_condition"][0]
    return (f"{city}: {cur['weatherDesc'][0]['value']}, {cur['temp_C']}°C / {cur['temp_F']}°F, "
            f"feels like {cur['FeelsLikeC']}°C, humidity {cur['humidity']}%, wind {cur['windspeedKmph']} km/h")


@tool(approval="write")
def append_note(ctx, text: str) -> str:
    """Append a line to notes.md in the workspace (asks for approval first).

    Args:
        text: the note to add
    """
    f = ctx.path("notes.md")
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(f"- {text}\n")
    return f"Added to {f}"
