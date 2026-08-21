"""
Design token parsing, validation, and rendering — Tier 1's other half.

design.md carries a ```yaml tokens block that this module parses into a
validated structure and renders into the two files the preview app needs:
tailwind.config.ts and app/globals.css.

**Why validation is strict.** The playbook's instruction for the composer is
to "bail loudly if the YAML block is missing or invalid — don't try to
compose against a half-shaped system". A malformed block that gets guessed at
produces a mockup built on invented colours, which looks like a design
failure rather than a config one. So every value is checked and a bad one
raises with the specific field named.

**FORBIDDEN_FAMILIES.** Tier 4's instruction is to block fonts that signal
"generic AI SaaS" at validation time. This is not taste policing for its own
sake — these families are what every model reaches for by default, so
allowing them means the output announces how it was made before anyone reads
a word of it.

shadcn wants HSL triples in CSS variables while designers write hex, so the
conversion happens here rather than being asked of whoever edits design.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEX_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# shadcn's base-color enum. Anything else makes `npx shadcn init` fail in a
# way that surfaces deep inside a Claude Code run rather than here.
SHADCN_BASE_COLORS = frozenset({"slate", "gray", "zinc", "neutral", "stone"})

# Curated Google Font families, most distinctive first (Tier 4: "order
# matters"). Not exhaustive — a curated list is the point.
DISPLAY_FONTS = (
    "Instrument Serif", "Fraunces", "Bricolage Grotesque", "Playfair Display",
    "Libre Baskerville", "Syne", "Unbounded", "Archivo", "Sora", "DM Serif Display",
)
BODY_FONTS = (
    "Inter", "Geist", "Source Sans 3", "IBM Plex Sans", "Public Sans",
    "Work Sans", "Karla", "Lato", "Nunito Sans", "Figtree",
)
MONO_FONTS = (
    "JetBrains Mono", "IBM Plex Mono", "Geist Mono", "Space Mono",
    "Fira Code", "Source Code Pro", "DM Mono", "Roboto Mono",
)

# Blocked at validation. These are the defaults that make generated design
# look generated — see the module docstring.
FORBIDDEN_FAMILIES = frozenset({
    "Space Grotesk", "Plus Jakarta Sans", "Poppins", "Montserrat",
    "Raleway", "Open Sans", "Roboto", "Nunito",
})

REQUIRED_COLORS = ("background", "foreground", "accent", "muted")


class TokenError(ValueError):
    """A design.md tokens block that cannot be trusted to compose against."""


@dataclass
class DesignTokens:
    fonts: dict = field(default_factory=dict)      # display / body / mono
    colors: dict = field(default_factory=dict)     # name -> #hex
    radius: str = "0.5rem"
    shadcn_base_color: str = "neutral"

    def google_font_families(self) -> list:
        seen, families = set(), []
        for role in ("display", "body", "mono"):
            name = self.fonts.get(role)
            if name and name not in seen:
                seen.add(name)
                families.append(name)
        return families


def extract_tokens_block(markdown: str) -> str:
    """
    Pull the ```yaml tokens fenced block out of design.md.

    Matched on the `yaml tokens` info string specifically, not on any yaml
    block: design.md is prose as well as config, and a doc that happens to
    contain an example yaml snippet must not be mistaken for the real one.
    """
    if not markdown:
        raise TokenError("design.md is empty — run the bootstrap first.")
    match = re.search(r"```yaml\s+tokens\s*\n(.*?)```", markdown, re.S)
    if not match:
        raise TokenError(
            "design.md has no ```yaml tokens block. The design system has to be "
            "declared before anything can be composed against it."
        )
    return match.group(1)


def _parse_simple_yaml(text: str) -> dict:
    """
    A deliberately tiny YAML reader for this one block shape: top-level keys,
    one level of `key: value` nesting, no lists or anchors.

    Written rather than depending on PyYAML because it is the only YAML in
    the project and the shape is fixed — adding a dependency to the Pi for
    forty lines of parsing is a worse trade than forty lines of parsing.
    Anything more complex than the documented shape raises.
    """
    result: dict = {}
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if ":" not in stripped:
            raise TokenError(f"tokens block: cannot read line {raw_line.strip()!r}")
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        # Comment stripping happens HERE, after the value is isolated, and
        # never inside a quoted one. Stripping `#` from the raw line first —
        # the obvious way to write this — eats every hex colour in the block,
        # because `background: "#0B0C0F"` becomes `background: "`. The values
        # this parser exists to read are mostly hex colours.
        if value[:1] in {'"', "'"}:
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        if indent == 0:
            if value:
                result[key] = value
                current = None
            else:
                current = {}
                result[key] = current
        else:
            if current is None:
                raise TokenError(f"tokens block: {key!r} is indented under nothing.")
            current[key] = value
    return result


def parse_tokens(markdown: str) -> DesignTokens:
    """Parse and validate. Raises TokenError naming the offending field."""
    data = _parse_simple_yaml(extract_tokens_block(markdown))

    fonts = data.get("fonts")
    if not isinstance(fonts, dict):
        raise TokenError("tokens block: a `fonts:` section with display/body/mono is required.")
    for role in ("display", "body", "mono"):
        name = (fonts.get(role) or "").strip()
        if not name:
            raise TokenError(f"tokens block: fonts.{role} is required.")
        if name in FORBIDDEN_FAMILIES:
            raise TokenError(
                f"tokens block: fonts.{role} = {name!r} is on the forbidden list. "
                "It reads as default AI-generated design. "
                f"Try one of: {', '.join((DISPLAY_FONTS if role == 'display' else BODY_FONTS if role == 'body' else MONO_FONTS)[:4])}."
            )

    colors = data.get("colors")
    if not isinstance(colors, dict):
        raise TokenError("tokens block: a `colors:` section is required.")
    for name in REQUIRED_COLORS:
        value = (colors.get(name) or "").strip()
        if not value:
            raise TokenError(f"tokens block: colors.{name} is required.")
        if not HEX_PATTERN.match(value):
            raise TokenError(f"tokens block: colors.{name} = {value!r} is not a hex colour.")

    base_color = (data.get("shadcn_base_color") or "neutral").strip()
    if base_color not in SHADCN_BASE_COLORS:
        raise TokenError(
            f"tokens block: shadcn_base_color = {base_color!r} is not one of "
            f"{', '.join(sorted(SHADCN_BASE_COLORS))}."
        )

    radius = (data.get("radius") or "0.5rem").strip()
    if not re.match(r"^\d+(\.\d+)?(rem|px|em)$", radius):
        raise TokenError(f"tokens block: radius = {radius!r} needs a unit (e.g. 0.5rem).")

    return DesignTokens(
        fonts={k: (fonts.get(k) or "").strip() for k in ("display", "body", "mono")},
        colors={k: v.strip() for k, v in colors.items() if isinstance(v, str)},
        radius=radius,
        shadcn_base_color=base_color,
    )


def hex_to_hsl(value: str) -> str:
    """
    '#0e0f13' -> '222 14% 6%' — the triple shadcn puts in CSS variables.

    Done here so whoever edits design.md writes hex like a designer, rather
    than being asked to hand-convert to a colour space to satisfy a library.
    """
    if not HEX_PATTERN.match(value or ""):
        raise TokenError(f"not a hex colour: {value!r}")
    raw = value.lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    r, g, b = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))

    high, low = max(r, g, b), min(r, g, b)
    lightness = (high + low) / 2
    if high == low:
        hue = saturation = 0.0
    else:
        delta = high - low
        saturation = delta / (2 - high - low) if lightness > 0.5 else delta / (high + low)
        if high == r:
            hue = ((g - b) / delta) % 6
        elif high == g:
            hue = (b - r) / delta + 2
        else:
            hue = (r - g) / delta + 4
        hue *= 60
    return f"{round(hue)} {round(saturation * 100)}% {round(lightness * 100)}%"


def default_tokens_yaml() -> str:
    """
    The bootstrap's concrete starting point. Real families, real hex values —
    never TODOs, per the playbook's stumbling block for this tier.
    """
    return """fonts:
  display: Instrument Serif
  body: Geist
  mono: JetBrains Mono
colors:
  background: "#0B0C0F"
  foreground: "#F2F3F5"
  accent: "#E8B44A"
  muted: "#8A8F98"
radius: 0.5rem
shadcn_base_color: neutral"""


def render_globals_css(tokens: DesignTokens) -> str:
    """app/globals.css — Tailwind directives plus shadcn's CSS variables."""
    c = tokens.colors
    return f""":root {{
  --background: {hex_to_hsl(c["background"])};
  --foreground: {hex_to_hsl(c["foreground"])};
  --accent: {hex_to_hsl(c["accent"])};
  --accent-foreground: {hex_to_hsl(c["background"])};
  --muted: {hex_to_hsl(c["muted"])};
  --muted-foreground: {hex_to_hsl(c["muted"])};
  --card: {hex_to_hsl(c["background"])};
  --card-foreground: {hex_to_hsl(c["foreground"])};
  --popover: {hex_to_hsl(c["background"])};
  --popover-foreground: {hex_to_hsl(c["foreground"])};
  --primary: {hex_to_hsl(c["accent"])};
  --primary-foreground: {hex_to_hsl(c["background"])};
  --secondary: {hex_to_hsl(c["muted"])};
  --secondary-foreground: {hex_to_hsl(c["foreground"])};
  --border: {hex_to_hsl(c["muted"])};
  --input: {hex_to_hsl(c["muted"])};
  --ring: {hex_to_hsl(c["accent"])};
  --radius: {tokens.radius};
}}

@tailwind base;
@tailwind components;
@tailwind utilities;

body {{
  background: hsl(var(--background));
  color: hsl(var(--foreground));
  font-family: var(--font-body), ui-sans-serif, system-ui, sans-serif;
}}
"""


def render_tailwind_config(tokens: DesignTokens) -> str:
    """tailwind.config.ts wired to the CSS variables above."""
    return """import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        display: ["var(--font-display)"],
        sans: ["var(--font-body)"],
        mono: ["var(--font-mono)"],
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
"""
