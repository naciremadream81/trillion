"""
The per-project preview app — playbooks/design-subagent.md, Tier 2, which
the playbook calls the architectural keystone.

Every project gets a Next.js + Tailwind + shadcn app at
`<project>/.prism/preview/`. The reason is blunt: "the output target IS the
design ceiling — vanilla HTML caps quality below award-winning." Mockups are
composed as real React pages against real primitives, then statically
exported and served.

THREE next.config settings are load-bearing, and two of them are the
playbook's named stumbling blocks:

  output: 'export'      static export, so viewing needs no Node runtime
  trailingSlash: true   each route becomes out/<path>/index.html. Without
                        it Next emits <screen>.html and the serving endpoint
                        looks for a path that isn't there — the playbook is
                        explicit that the fix is this setting, NOT changing
                        the serving expectations to match the default.
  basePath + assetPrefix  BOTH, set to the serving prefix, or Next emits
                        asset URLs that 404 under that prefix.

**Disk preflight.** Not in the playbook, and added because this machine is
at 93% of a 235G disk. A Next install is 400-700MB per project, and filling
this disk doesn't just fail the scaffold — it takes down Trillion, which
lives on the same volume. So the scaffold refuses before writing rather than
discovering it at `npm install` time.

Idempotent: a project whose package.json exists is left alone.
"""

from __future__ import annotations

import json
import os
import shutil

from .component_catalog import render_full_catalog_markdown
from .design_tokens import DesignTokens, render_globals_css, render_tailwind_config
from .docs import PREVIEW_DIR, DesignDocError, assert_within_project

# Refuse to scaffold with less than this free. A Next.js install runs
# 400-700MB; leaving a couple of gigabytes of headroom means a scaffold can
# never be the thing that fills the volume Trillion itself runs on.
MIN_FREE_BYTES = 3 * 1024**3


def free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        # Unknown beats refusing: a stat failure shouldn't block a scaffold
        # on a machine with plenty of room.
        return MIN_FREE_BYTES


def preview_base_path(project_slug: str) -> str:
    """The URL prefix this project's preview is served under."""
    return f"/api/design/{project_slug}/preview"


def _package_json() -> str:
    return json.dumps(
        {
            "name": "prism-preview",
            "private": True,
            "version": "0.1.0",
            "scripts": {
                "dev": "next dev",
                "build": "next build",
                "start": "next start",
                "lint": "next lint",
            },
            "dependencies": {
                "next": "^15.0.0",
                "react": "^19.0.0",
                "react-dom": "^19.0.0",
                "class-variance-authority": "^0.7.0",
                "clsx": "^2.1.1",
                "tailwind-merge": "^2.5.0",
                "tailwindcss-animate": "^1.0.7",
                "lucide-react": "^0.460.0",
            },
            "devDependencies": {
                "typescript": "^5.6.0",
                "@types/node": "^22.0.0",
                "@types/react": "^19.0.0",
                "@types/react-dom": "^19.0.0",
                "tailwindcss": "^3.4.0",
                "postcss": "^8.4.0",
                "autoprefixer": "^10.4.0",
            },
        },
        indent=2,
    ) + "\n"


def _next_config(base_path: str) -> str:
    return f"""/** @type {{import('next').NextConfig}} */
const nextConfig = {{
  // Static export: viewing a mockup must not need a Node runtime.
  output: 'export',
  // Each route becomes out/<path>/index.html. WITHOUT THIS, Next emits
  // <screen>.html and the serving endpoint looks for a path that does not
  // exist. The fix is this setting, not relaxing what the server expects.
  trailingSlash: true,
  // BOTH are required. basePath alone leaves asset URLs unprefixed, so
  // every script and stylesheet 404s under the serving prefix.
  basePath: '{base_path}',
  assetPrefix: '{base_path}',
  images: {{
    // Static export has no image optimizer. Plain <img> is simpler and
    // equivalent here — see the Tier 5 note about basePath and <img>.
    unoptimized: true,
  }},
  eslint: {{ ignoreDuringBuilds: true }},
  typescript: {{ ignoreBuildErrors: true }},
}};

export default nextConfig;
"""


def _components_json(tokens: DesignTokens) -> str:
    return json.dumps(
        {
            "$schema": "https://ui.shadcn.com/schema.json",
            "style": "new-york",
            "rsc": True,
            "tsx": True,
            "tailwind": {
                "config": "tailwind.config.ts",
                "css": "app/globals.css",
                "baseColor": tokens.shadcn_base_color,
                "cssVariables": True,
            },
            "aliases": {"components": "@/components", "utils": "@/lib/utils"},
        },
        indent=2,
    ) + "\n"


# next/font/google requires an explicit `weight` for families that are not
# published as variable fonts, and rejects one for families that are. Getting
# this wrong fails the build inside a Claude Code run rather than here, so the
# curated families that need it are listed rather than guessed at by name —
# "Serif" in the name is not what decides it.
_STATIC_WEIGHT_FAMILIES = {
    "Instrument Serif": ["400"],
    "DM Serif Display": ["400"],
    "Libre Baskerville": ["400", "700"],
    "Space Mono": ["400", "700"],
    "Playfair Display": None,   # variable
}


def _fonts_ts(tokens: DesignTokens) -> str:
    """
    lib/fonts.ts — next/font/google for the families design.md picked.

    next/font/google imports by identifier, not display name, so
    "Instrument Serif" becomes Instrument_Serif.
    """
    imports, exports = [], []
    for role in ("display", "body", "mono"):
        family = (tokens.fonts.get(role) or "").strip()
        if not family:
            continue
        identifier = family.replace(" ", "_").replace("-", "_")
        imports.append(f"import {{ {identifier} }} from 'next/font/google';")

        options = [
            "  subsets: ['latin'],",
            f"  variable: '--font-{role}',",
            "  display: 'swap',",
        ]
        weights = _STATIC_WEIGHT_FAMILIES.get(family)
        if weights:
            rendered = ", ".join(f"'{w}'" for w in weights)
            options.append(f"  weight: [{rendered}],")
        body = "\n".join(options)
        exports.append(f"export const {role}Font = {identifier}({{\n{body}\n}});")

    return "\n".join(imports) + "\n\n" + "\n\n".join(exports) + "\n"


def _layout_tsx() -> str:
    return """import type { Metadata } from 'next';
import { displayFont, bodyFont, monoFont } from '@/lib/fonts';
import './globals.css';

export const metadata: Metadata = {
  title: 'Preview',
  description: 'Design mockups',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${displayFont.variable} ${bodyFont.variable} ${monoFont.variable}`}
    >
      <body>{children}</body>
    </html>
  );
}
"""


def _index_page() -> str:
    """
    app/page.tsx — a minimal index. Deliberately static rather than reading
    the filesystem: `output: 'export'` means this renders at build time, and
    a dynamic read would produce a stale list baked into HTML.
    """
    return """export default function Home() {
  return (
    <main className="min-h-screen flex items-center justify-center p-12">
      <div className="max-w-lg">
        <h1 className="font-display text-4xl mb-4">Preview</h1>
        <p className="text-muted-foreground">
          Generated mockups are served under this prefix at
          <code className="font-mono text-sm"> /&lt;feature&gt;/&lt;screen&gt;/</code>.
        </p>
      </div>
    </main>
  );
}
"""


def _utils_ts() -> str:
    return """import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
"""


def _tsconfig() -> str:
    return json.dumps(
        {
            "compilerOptions": {
                "target": "ES2017",
                "lib": ["dom", "dom.iterable", "esnext"],
                "allowJs": True,
                "skipLibCheck": True,
                "strict": False,
                "noEmit": True,
                "esModuleInterop": True,
                "module": "esnext",
                "moduleResolution": "bundler",
                "resolveJsonModule": True,
                "isolatedModules": True,
                "jsx": "preserve",
                "incremental": True,
                "plugins": [{"name": "next"}],
                "paths": {"@/*": ["./*"]},
            },
            "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
            "exclude": ["node_modules"],
        },
        indent=2,
    ) + "\n"


def scaffold_files(tokens: DesignTokens, project_slug: str) -> dict:
    """
    Every file the scaffold writes, as {relative_path: content}.

    A pure function so the whole scaffold can be tested without touching a
    filesystem — and so its contents can be asserted on, which is how the
    three load-bearing next.config settings stay load-bearing.
    """
    base_path = preview_base_path(project_slug)
    return {
        "package.json": _package_json(),
        "next.config.mjs": _next_config(base_path),
        "tailwind.config.ts": render_tailwind_config(tokens),
        "postcss.config.mjs": (
            "export default { plugins: { tailwindcss: {}, autoprefixer: {} } };\n"
        ),
        "tsconfig.json": _tsconfig(),
        "components.json": _components_json(tokens),
        "lib/fonts.ts": _fonts_ts(tokens),
        "lib/utils.ts": _utils_ts(),
        "app/layout.tsx": _layout_tsx(),
        "app/globals.css": render_globals_css(tokens),
        "app/page.tsx": _index_page(),
        "prism/component_catalog.md": render_full_catalog_markdown(),
        ".gitignore": "node_modules/\n.next/\nout/\n",
    }


def prepare_scaffold(
    project_root: str, project_slug: str, tokens: DesignTokens, force: bool = False
) -> dict:
    """
    Write the scaffold if it isn't there. Returns what happened.

    Idempotent on package.json, per the playbook. `force` rewrites the
    generated config files — but never `app/<feature>/` pages, which are
    composed output rather than scaffold, and never node_modules.
    """
    preview_root = assert_within_project(project_root, PREVIEW_DIR)

    already = os.path.isfile(os.path.join(preview_root, "package.json"))
    if already and not force:
        return {"scaffolded": False, "preview_root": preview_root, "written": []}

    available = free_bytes(project_root)
    if available < MIN_FREE_BYTES:
        raise DesignDocError(
            f"Refusing to scaffold: {available / 1024**3:.1f} GB free, "
            f"under the {MIN_FREE_BYTES / 1024**3:.0f} GB floor. A Next.js install "
            "needs several hundred MB, and filling this volume would take Trillion "
            "down with it."
        )

    written = []
    for relative, content in scaffold_files(tokens, project_slug).items():
        path = assert_within_project(preview_root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(relative)

    return {"scaffolded": True, "preview_root": preview_root, "written": sorted(written)}


def expected_output_path(preview_root: str, feature_slug: str, screen_name: str) -> str:
    """
    Where a composed screen must land. This is the "did the build actually
    succeed" check the composer runs after Claude Code returns — the presence
    of this file, not Claude Code's own report of success.
    """
    return assert_within_project(
        preview_root, os.path.join("out", feature_slug, screen_name, "index.html")
    )
