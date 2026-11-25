# Excudo Design System

Design DNA for the Excudo desktop app (`excudo/app/ui/`), synthesized from three
AI coding-tool product sites: **Claude Code** (claude.com/product/claude-code),
**Codex** (openai.com/codex) and **Google Antigravity** (antigravity.google).
Values were read from the live computed styles and CSS custom properties of
those pages (2026-10-07). Brand-specific assets (logos, proprietary fonts,
signature brand colors) are **not** copied; Excudo uses its own accent and
open fonts.

Implemented in `excudo/app/ui/style.css`.

---

## A. Style overview

**Shared philosophy of the three references**

- **Monochrome first.** The primary button uses the text color as its background
  (black on light, off-white on dark). Accent color is used sparingly.
- **Calm, generous whitespace.** Large type with moderate weights (400–500, rarely
  bold); paragraphs capped at ~60–70ch.
- **Low ornament.** Hardly any heavy borders or shadows; hierarchy comes from
  stacked surfaces of the same hue.
- **Per-site signature**
  - Claude Code: warm, slightly beige neutrals (`#141413` / `#faf9f5`), radii 6–12px, "out-expo" motion.
  - Codex: pure black/white, pill buttons, tight negative letter-spacing.
  - Antigravity: cool neutrals, pill buttons, 10% tonal fills, 16–24px card radii.

**Excudo vibe:** a calm, trustworthy work tool — Claude's warm neutrals,
Antigravity's pill + tonal buttons, Codex's tight type.

| Do | Don't |
| --- | --- |
| Monochrome primary; accent only for focus, links, state | Several brand colors at once, large gradients |
| Layer with neutral surfaces | Thick 2px borders around everything |
| Headings at weight 450–500 | Bold (700) everywhere |
| Consistent radii: 8 / 12 / 16 / pill | Random radius sizes |
| 150–250ms motion with ease-out | Bounce or long animations |

**Missing data (to verify)**

- No screenshots could be captured (browser pane was minimized); hero/footer layouts are **estimates**.
- Antigravity dark theme and the pricing pages of all three sites were not inspected.
- All three use **proprietary fonts** (anthropicSans, OpenAI Sans, Google Sans Flex); open alternatives are proposed below.
- Verify by comparing DevTools → Computed with real screenshots.

---

## B. Color system (role-based tokens)

| Role | Light | Dark | Reference |
| --- | --- | --- | --- |
| `--bg` app background | `#faf9f7` | `#141413` | Claude gray-050/950 |
| `--surface` cards, inputs | `#ffffff` | `#1f1e1d` | Claude gray-000/850 |
| `--surface-2` containers, hover, sidebar | `#f3f2ee` | `#262624` | Claude gray-100/800, Antigravity container |
| `--surface-3` code, selection | `#ebe9e3` | `#30302e` | Claude gray-200/750 |
| `--tonal` secondary-button fill | `rgba(183,191,217,.12)` | `rgba(255,255,255,.08)` | Antigravity secondary-button |
| `--text` | `#141413` | `#faf9f5` | Claude |
| `--text-muted` | `#5e5d59` | `#b0aea5` | Claude gray-600/400 |
| `--text-subtle` (meta only) | `#87867f` | `#87867f` | Claude gray-500 |
| `--border` | `rgba(20,20,19,.10)` | `rgba(250,249,245,.10)` | Antigravity outline .12/.06 |
| `--border-strong` | `rgba(20,20,19,.22)` | `rgba(250,249,245,.20)` | Antigravity button outline .22 |
| `--primary` / `--on-primary` | `#141413` / `#faf9f5` | `#faf9f5` / `#141413` | Monochrome primary (all three) |
| `--accent` (Excudo's own) | `#3b6ef5` (estimate) | `#7b9cff` | Own hue, near the Codex/Antigravity blues |
| `--accent-soft` | `rgba(59,110,245,.12)` | `rgba(123,156,255,.16)` | |
| `--success` | `#2f8f5b` | `#5cc48d` | (estimate) |
| `--warn` / `--warn-soft` | `#b7791f` / `#fff6df` | `#e0a446` / `#3a3222` | (estimate) |
| `--error` / `--error-soft` | `#bf4d43` / `#fdecea` | `#e8786e` / `#3b2626` | Claude `--color-error` |
| `--focus` | `= --accent` | `= --accent` | Claude uses its accent for focus |

Tailwind mapping: `bg-bg`, `bg-surface`, `bg-surface-2`, `text-fg`, `text-fg-muted`,
`border-line`, `bg-primary text-primary-fg`, `text-accent` … (see H2).

---

## C. Typography

| Role | Stack | Note |
| --- | --- | --- |
| Sans (UI + content) | `"Inter", "Noto Sans JP", "Segoe UI", system-ui, sans-serif` | Replaces the proprietary brand fonts. Inter covers Vietnamese diacritics; Noto Sans JP covers Japanese |
| Mono | `"JetBrains Mono", "Cascadia Mono", "BIZ UDGothic", ui-monospace, monospace` | (estimate) |

| Token | Size / line-height | Weight | Letter-spacing | Reference |
| --- | --- | --- | --- | --- |
| Display | 48 / 52.8px | 450 | −0.02em | Claude h1 48/52.8; Antigravity 37.6/41.4 |
| H1 | 36 / 40px | 500 | −0.02em | |
| H2 | 32 / 36.5px | 500 | −0.016em | Codex h2 32/36.5, −0.53px |
| H3 | 24 / 31.7px | 500 | −0.01em | Codex h3 24/31.7, −0.24px |
| H4 | 20 / 28px | 500 | −0.005em | |
| H5 | 17 / 26px | 600 | 0 | |
| H6 | 15 / 22px | 600 | 0 | |
| Body L (marketing) | 17–18 / 28px | 400 | −0.01em | Codex 17/28; Antigravity 18/28 |
| Body (app) | 15 / 22.5px | 400 | 0 | Claude body 15/22.5 |
| Small | 13 / 20px | 400 | 0 | |
| Caption / meta | 12 / 16px | 450 | 0.01em | |
| Code | 13 / 20px mono | 400 | 0 | |

Measure: body 60ch, prose 70–80ch, headlines 30ch (Claude `--text-width-*`).

---

## D. Layout & spacing

- **Containers:** 1440 (main), 1192 (medium), 960 (small), 660 (narrow) — Claude; Codex uses 1100 / 840.
- **Breakpoints (estimate, Tailwind defaults):** sm 640, md 768, lg 1024, xl 1280, 2xl 1440.
- **Gutter:** 16 (mobile), 24 (md), 32 (lg).
- **Spacing scale (4px base):** 4, 8, 12, **16, 24, 32, 48** (Claude `--gap-xs…lg`), 64, 96.
- **Section padding:** 64 (mobile) → 96 (desktop); footer 64 vertical (Claude).
- **Grid:** 12 columns, gap 24–32; feature cards in 2–3 columns.
- **App density (Excudo):** sidebar 248px, top bar 52px, conversation column max 820px, 16px between messages.

---

## E. Component DNA

| # | Component | Spec |
| --- | --- | --- |
| 1 | **Button – primary** | `--primary` bg, `--on-primary` text, 36–40px tall, 0×16–24 padding, **pill** radius (Codex/Antigravity; Claude uses 8px), 14–15/500. Hover: opacity .88. Disabled: opacity .4 |
| 2 | **Button – secondary (tonal)** | `--tonal` bg + 1px `--border`, `--text`, same size. Hover: `--surface-2` |
| 3 | **Button – ghost / icon** | Transparent; hover `--surface-2`. Icon buttons 32–36px square |
| 4 | **Card** | `--surface`, 1px `--border`, radius **12px** (app) / 16–24px (marketing), padding 14–24, no shadow; floating layers use `--shadow-lg` |
| 5 | **Navbar (marketing)** | Fixed, same color as `--bg`, no border; logo left, links 13/600 −0.01em (Codex nav), pill CTA right |
| 6 | **Sidebar (app)** | 248px, `--surface-2`, 1px right border; items 36px tall, radius 8, hover `--surface-3`, active `--surface` + weight 500 |
| 7 | **Composer / input** | Chat composer: `--surface`, 1px `--border-strong`, radius **16px**, send button inside. Focus: `--accent` border + `0 0 0 3px var(--accent-soft)`. Plain inputs: radius 10px |
| 8 | **Select** | Same as input, 36–40px, chevron in `--text-muted` |
| 9 | **Badge / tag** | Pill, 22px, 2×8 padding, 12/500; tonal = `--surface-2`, status = `*-soft` bg + status text |
| 10 | **Modal / drawer** | Overlay `rgba(20,20,19,.45)` (dark .6); panel `--surface`, radius 16, 480–560px, padding 24; drawer from right 360–420px |
| 11 | **Table** | Header `--surface-2`, 12.5/600 muted; cells 7×12; horizontal 1px `--border` only |
| 12 | **Code block** | `--surface-3`, radius 8, 12×14 padding, mono 13/20 |
| 13 | **Chat message** | User: `--surface-2` bubble, radius 18 (bottom-right 6), right-aligned, max 75%. Assistant: plain text, no bubble |
| 14 | **Tool activity row** | One 13px muted line with a status icon (✓ success, ✗/⊘ error); hover `--surface-2`; click to expand details |
| 15 | **Approval card** | Radius 12, 1px status border (`--warn` / `--error`) + `*-soft` fill; primary "Allow" + secondary "Deny" (focused by default) |
| 16 | **Hero (estimate)** | Centered; display 48; muted Body L subtitle ≤60ch; primary + tonal CTAs 12px apart; product visual below |
| 17 | **Footer (estimate)** | `--bg`, 64 vertical padding, 4–5 link columns 13px muted, copyright 12px subtle |

---

## F. Motion & micro-interactions

- **Easing:** `--ease-out: cubic-bezier(.19,1,.22,1)` (Claude out-expo) for entering elements; `--ease-standard: cubic-bezier(.25,1,.5,1)` (Claude out-power2) for hovers.
- **Durations:** 120ms (text color), **150–200ms** (background/border; Claude uses 0.15s / 0.2s), 300ms (modal/drawer).
- **Hover:** color/background only, no scaling. **Press:** `scale(.98)`.
- **Focus:** 2px `--focus` ring, 2px offset.
- **Streaming chat:** text appears without animation; "working" indicator = three dots pulsing over 1s.
- Respect `prefers-reduced-motion` (motion disabled).

---

## G. Accessibility

- **Contrast (estimates — verify with a contrast checker):**

| Pair | Ratio | Result |
| --- | --- | --- |
| `--text` on `--bg` (both themes) | ~17–18:1 | ✅ |
| `--text-muted` light `#5e5d59` on `#faf9f7` | ~6.5:1 | ✅ AA |
| `--text-muted` dark `#b0aea5` on `#141413` | ~8:1 | ✅ |
| `--text-subtle` `#87867f` | ~3.5:1 | ⚠️ meta / large text only |

- **Focus:** always a visible `:focus-visible` ring; never remove outlines without a replacement.
- **Touch targets:** ≥ 40×40 desktop, ≥ 44×44 touch.
- **Never color alone:** status always has an icon (✓ ✗ ⊘) and text.
- `<html lang>` follows the UI language (ja / vi / en).

---

## H. Implementation pack

### 1) CSS variables

See the `:root` and `@media (prefers-color-scheme: dark)` blocks at the top of
`excudo/app/ui/style.css` (source of truth). For class-based theming, put the
dark block under `.dark` instead.

### 2) Tailwind theme extension

```js
// tailwind.config.js
module.exports = {
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        surface: { DEFAULT: "var(--surface)", 2: "var(--surface-2)", 3: "var(--surface-3)" },
        tonal: "var(--tonal)",
        fg: { DEFAULT: "var(--text)", muted: "var(--text-muted)", subtle: "var(--text-subtle)" },
        line: { DEFAULT: "var(--border)", strong: "var(--border-strong)" },
        primary: { DEFAULT: "var(--primary)", fg: "var(--on-primary)" },
        accent: { DEFAULT: "var(--accent)", soft: "var(--accent-soft)" },
        success: "var(--success)",
        warn: { DEFAULT: "var(--warn)", soft: "var(--warn-soft)" },
        error: { DEFAULT: "var(--error)", soft: "var(--error-soft)" },
      },
      fontFamily: { sans: ["var(--font-sans)"], mono: ["var(--font-mono)"] },
      fontSize: {
        display: ["48px", { lineHeight: "1.1", letterSpacing: "-0.02em", fontWeight: "450" }],
        h1: ["36px", { lineHeight: "40px", letterSpacing: "-0.02em", fontWeight: "500" }],
        h2: ["32px", { lineHeight: "36.5px", letterSpacing: "-0.016em", fontWeight: "500" }],
        h3: ["24px", { lineHeight: "31.7px", letterSpacing: "-0.01em", fontWeight: "500" }],
        h4: ["20px", { lineHeight: "28px", fontWeight: "500" }],
        "body-lg": ["17px", { lineHeight: "28px", letterSpacing: "-0.01em" }],
        body: ["15px", { lineHeight: "22.5px" }],
        small: ["13px", { lineHeight: "20px" }],
        caption: ["12px", { lineHeight: "16px", letterSpacing: "0.01em" }],
      },
      borderRadius: { sm: "6px", md: "8px", lg: "12px", xl: "16px", pill: "9999px" },
      boxShadow: { lg: "var(--shadow-lg)" },
      maxWidth: { main: "1440px", medium: "1192px", small: "960px", narrow: "660px", chat: "820px" },
      transitionTimingFunction: { out: "var(--ease-out)", standard: "var(--ease-standard)" },
      transitionDuration: { fast: "120ms", DEFAULT: "180ms", slow: "300ms" },
    },
  },
};
```

> For opacity modifiers (`bg-accent/20`), define channel variables such as
> `--accent-rgb: 59 110 245` and use `rgb(var(--accent-rgb) / <alpha-value>)`.

### 3) Component guidelines

- **One primary button per screen** (in Excudo: Send). Secondary actions are tonal, the rest ghost.
- **Surfaces:** `bg` (app) → `surface-2` (sidebar, hover) → `surface` (cards, composer) → `surface-3` (code, selection). Add a 1px `line` border only where two surfaces are too close.
- **Accent** only for focus, links, selected state and toggles — never large fills.
- **Status:** `*-soft` background + status-colored text + icon. Approval cards: warn for file changes/commands, error for outbound data/sensitive files.
- **Type:** headings 450–500, labels 600; paragraphs `max-w-[60ch]`; meta `text-caption text-fg-subtle`.
- **Radii:** inputs/icon buttons 8–10, cards/messages 12–18, buttons/tags pill — do not invent new sizes.
- **Motion:** hovers 150–200ms `ease-standard`; overlays 300ms `ease-out`; no hover scaling.
