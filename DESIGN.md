---
name: labslots
description: The lab's PC slot desk, set like the department's class routine.
colors:
  sheet: "#ffffff"
  panel: "#eef2f6"
  panel-2: "#dfe6ee"
  ink: "#15202b"
  ink-2: "#47525e"
  ink-3: "#66717d"
  rule: "#15202b"
  rule-soft: "#c6cfd9"
  field-stroke: "#8995a2"
  routine-blue: "#1d4e89"
  routine-blue-deep: "#163d6c"
  routine-blue-soft: "#d6e3f2"
  routine-blue-tint: "#e6eef8"
  on-blue: "#ffffff"
  state-red: "#b42318"
  state-red-tint: "#fdecea"
  state-amber: "#7a4f00"
  state-amber-tint: "#fff3d6"
  state-green: "#1e6b3b"
  state-green-tint: "#e5f3ea"
typography:
  headline:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  title:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif"
    fontSize: "17px"
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum"
  label:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1
  code-mark:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    letterSpacing: "0.03em"
  table-head:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    letterSpacing: "0.04em"
  caption:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif"
    fontSize: "12px"
    fontWeight: 400
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Cascadia Mono, Menlo, monospace"
    fontSize: "13px"
    fontWeight: 400
rounded:
  mark: "2px"
  control: "4px"
  dot: "50%"
spacing:
  s1: "4px"
  s2: "8px"
  s3: "12px"
  s4: "16px"
  s5: "20px"
  s6: "24px"
  s8: "32px"
  s10: "40px"
components:
  button:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 16px"
    height: "36px"
  button-hover:
    backgroundColor: "{colors.panel}"
  button-active:
    backgroundColor: "{colors.panel-2}"
  button-primary:
    backgroundColor: "{colors.routine-blue}"
    textColor: "{colors.on-blue}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 16px"
    height: "36px"
  button-primary-hover:
    backgroundColor: "{colors.routine-blue-deep}"
  button-danger:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.state-red}"
    rounded: "{rounded.control}"
  button-danger-hover:
    backgroundColor: "{colors.state-red-tint}"
  button-quiet:
    textColor: "{colors.routine-blue}"
    rounded: "{rounded.control}"
  button-small:
    padding: "5px 12px"
    height: "28px"
  input:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
    height: "36px"
  shell:
    backgroundColor: "{colors.routine-blue}"
    textColor: "{colors.on-blue}"
    padding: "0 20px"
    height: "52px"
  nav-link:
    textColor: "{colors.routine-blue-soft}"
    typography: "{typography.label}"
    padding: "0 12px"
  nav-link-active:
    textColor: "{colors.on-blue}"
  statusline:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    padding: "8px 20px"
  code-mark:
    textColor: "{colors.ink}"
    typography: "{typography.code-mark}"
    rounded: "{rounded.mark}"
    padding: "1px 6px"
  code-mark-pending:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.mark}"
    padding: "1px 6px"
  routine-cell:
    backgroundColor: "{colors.sheet}"
    height: "48px"
    padding: "4px"
  routine-cell-today:
    backgroundColor: "{colors.routine-blue-tint}"
  routine-cell-live:
    backgroundColor: "{colors.routine-blue}"
  routine-head-today:
    backgroundColor: "{colors.routine-blue}"
    textColor: "{colors.on-blue}"
  panel:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.control}"
    padding: "16px"
  note:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.control}"
    padding: "12px 16px"
  note-ok:
    backgroundColor: "{colors.state-green-tint}"
  note-err:
    backgroundColor: "{colors.state-red-tint}"
  note-warn:
    backgroundColor: "{colors.state-amber-tint}"
---

# Design System: labslots

## Overview

**Creative North Star: "The Class Routine"**

The desk is set like the department's printed class routine: a white sheet, days running across, PCs running down, group codes typed into ruled cells. Ink rules carry the structure and grow heavier at each day boundary. Routine blue owns two things only: the shell bar that frames every page, and time (today's column, the slot running now, the primary action). Everything else is ink on white, with a pale blue-grey second layer for panels, table heads and the status line.

Density is spreadsheet density. The full week for every PC has to stay readable in one view on a laptop, so the type is small and even (14px body, 13px inside the grid), numerals are tabular everywhere, and nothing decorative competes with state. What is live, what is pending and what has failed outrank everything else. Each thesis group appears as a highlighter tint behind its bold capital code, the way a student marks their own classes on a printed routine.

The world rejects the admin-template look: no cards, no pills, no grey sidebar, no drop shadows. Grouping comes from a ruled heading or a flat panel fill. On a phone the routine turns into one PC's week per screen, and each swipe snaps to the next PC.

**Key Characteristics:**
- White sheet ground with ink rules: 2px at day boundaries and the PC column, 1px soft rules inside a day.
- Routine blue for the shell, today and the live slot, never as decoration.
- Group identity is a highlighter tint behind a bold uppercase code; pending is hollow and dashed.
- System sans throughout with tabular numerals; one type size inside the grid.
- Flat colour only: depth comes from the panel layer, state comes from inset rings.

## Colors

A cool, nearly monochrome ink-and-paper palette with one institutional blue and three state hues held in reserve.

### Primary
- **Routine Blue** (routine-blue): the shell bar, today's day header, the live cell (fully reversed), primary buttons, links, the focus ring and the caret. On blue, text is white (on-blue) and secondary text is **Routine Blue Soft** (routine-blue-soft).
- **Routine Blue Deep** (routine-blue-deep): hover for primary buttons and the shell's sign-out button. Never used at rest.
- **Today Tint** (routine-blue-tint): the body cells of today's column and the text-selection highlight.

### Neutral
- **Sheet** (sheet): page ground, table body, inputs and default buttons.
- **Panel Layer** (panel): the second layer. Used for the status line, table heads, notes, `pre` blocks, locked fields, the fixed email suffix and button hover. **Panel Deep** (panel-2) is used only for button press.
- **Ink** (ink): body text, default button stroke, and (as **Rule**, the same value) day-boundary and section rules.
- **Ink 2** (ink-2): secondary text such as page descriptions, table-head labels, booking meta and the key. Contrast is 7.6:1 on white.
- **Ink 3** (ink-3): captions, PC tags, half-day labels and empty-state text. Contrast is 4.9:1 on white, and this is the lightest text the system allows.
- **Soft Rule** (rule-soft): the 1px rules inside a day, table row dividers and the free-cell dash.
- **Field Stroke** (field-stroke): the 1px border of inputs, selects and textareas at rest. It darkens to Ink 2 on hover.

### State
- **Red** (state-red) on **Red Tint**: a failed switch, enforcement errors, the fail-closed alert and destructive buttons.
- **Amber** (state-amber) on **Amber Tint**: waiting. Used for the pending count in the status line, dry-run switches, "waiting for first run" and warning notes.
- **Green** (state-green) on **Green Tint**: healthy. Used for switches that are on, nodes that are online, and success notes.

### Group highlighter tints
Group colours are data, not tokens. Each group stores its own colour (the Colour column of the Sheet's Groups tab; a new group gets the next unused highlighter tint). The colour is only ever painted behind that group's code mark and in its swatch in the calendar key. Pick pale highlighter values so bold Ink text on top stays at 4.5:1 or better.

### Named Rules
**The Routine Blue Rule.** Blue means the frame or the clock: the shell, today, the live slot, the one primary action, links and focus. If blue isn't marking one of those, it shouldn't be there.

**The Highlighter Rule.** A group's colour appears only as the tint behind its bold capital code and in its key swatch. Never use it as a text colour, a border, a row fill or a chart series.

**The State Reserve Rule.** Red, amber and green are spent only on state (failed, waiting, healthy), always paired with a word. They are never used for decoration or categories.

## Typography

**Body Font:** system-ui (with -apple-system, Segoe UI, Roboto, Noto Sans, Noto Sans Bengali, sans-serif)
**Label/Mono Font:** ui-monospace (with SFMono-Regular, Cascadia Mono, Menlo, monospace)

**Character:** The plain, native sans of a printed routine. Hierarchy comes from weight and rules, not size. Bengali coverage is in the stack on purpose. Tabular numerals are set on `body`, so every time, date and count aligns in columns.

### Hierarchy
- **Headline** (700, 24px, 1.25, -0.01em, balanced wrap): one page title per page. Drops to 20px under 760px. There is no display tier.
- **Title** (700, 17px, 1.3): section headings after a 2px rule, and the PC name on phone pages.
- **Body** (400, 14px, 1.5): running text, inputs and the default size. Page descriptions are capped at 70ch.
- **Label** (600, 13px): buttons, navigation, form labels and the status line.
- **Code mark** (700, 13px, 0.03em, uppercase): a group's short code in the grid, on the Now table and in the slot picker.
- **Table head** (700, 12px, 0.04em, uppercase, Ink 2): column heads of list tables. This style is used for column labels only.
- **Caption** (400, 12px): hints, meta lines, the key and day/half sub-labels.
- **Mono** (400, 13px; 12px in `pre`): PC tags, IPs, account names, code and policy previews.

### Named Rules
**The One Size Grid Rule.** Inside the routine everything is set at 13px. Weight, case and tint separate code from caption; size never does.

**The Tabular Rule.** Numerals are tabular everywhere. Times, dates and counts must line up down a column.

## Layout

The page has a sticky 52px blue shell. Under it, a panel-coloured status line (live slot, next change, three enforcement switches, alerts, pending count), then `main`. Shell, status line and main share a centred content width (max 1680px) with 20px side gutters. Main padding is 24px top and 40px bottom. A page head puts the headline and description on the left, page-level controls on the right, and wraps when space runs out.

The routine lives in the Calendar's week view. It is a fixed-layout table with a 128px sticky PC column and seven day columns, each split into 06:00–17:55 and 18:00–05:55 halves. Cells are 48px tall. Header rows stick to the top. Content sections tile with `auto-fit` at a 420px minimum and 32px gaps.

Spacing uses the 4px scale s1–s10 for gaps, margins and layout padding. Control heights are fixed (36px default, 28px small, 30px in-cell inputs). Their internal padding uses odd pixel values so a 1px border still lands on those heights.

Responsive steps:
- **≤760px:** the shell wraps and the nav becomes a full-width horizontal scroller (40px tall). Gutters go to 16px. The desktop routine is hidden and replaced by phone pages, one PC's week per screen as a 7-day × 2-half mini table, with mandatory scroll-snap between PCs. List tables collapse into stacked rows with `data-label` prefixes.

## Elevation & Depth

The system is flat. Depth is one tonal step: Sheet, then the Panel layer. Nothing floats. There are no outer shadows. Box-shadow is used only as an **inset ring** that reports state:

### Shadow Vocabulary
- **Nav current** (`inset 0 -3px 0 #ffffff`): a 3px white underline on the current page's tab.

### Named Rules
**The Flat Sheet Rule.** Surfaces never lift. If something needs to stand apart, give it the Panel fill or a rule; box-shadow is used only as the inset underline on the current tab.

## Shapes

Corners are just softened: 4px on controls, panels, table frames and notes, and 2px on code marks, swatches, the request count and key samples. Status dots are the only circles (8px). Tables are framed with a 1px Ink border at 4px radius and ruled inside. The routine draws 2px Ink rules at each day start and to the right of the PC column, and 1px Soft Rules elsewhere. A section is a 2px Ink top rule with 16px padding above its heading, never a box. The sign-in page uses a 4px Routine Blue top rule over a 440px column.

## Components

### Buttons
Plain and ruled, like a form on a notice board.
- **Shape:** gently softened (4px), 36px tall, 1px Ink stroke on Sheet, 600-weight label.
- **Primary:** Routine Blue fill and stroke with white text. Hover darkens to Routine Blue Deep. Use one per decision point.
- **Hover / Active / Focus:** hover fills Panel, press fills Panel Deep. Focus is a 2px Routine Blue outline offset 2px. Disabled is 45% opacity. An in-flight htmx request is 60% opacity with a progress cursor.
- **Danger:** red text and stroke on Sheet, hover Red Tint. Used for remove actions, which ask for confirmation first.
- **Quiet:** no stroke, blue text, for tertiary actions.
- **Small:** 28px tall, 12px text, for row actions.

### Cards / Containers
- **Corner Style:** 4px.
- **Background:** Panel, flat. `.panel` blocks, notes and `pre` use it.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** panels have none. Notes have a 1px stroke tinted to their state. Content sections are not containers; they are a 2px rule plus a heading.
- **Internal Padding:** 16px for panels, 12px × 16px for notes, 12px for `pre`.

### Inputs / Fields
- **Style:** Sheet fill, 1px Field Stroke, 4px radius, 36px tall, 14px text. Labels stack above at 13px/600, with hints at 12px Ink 3. Checkbox and radio controls are 16px and use Routine Blue as the accent colour.
- **Focus:** 2px Routine Blue outline at zero offset, and the border turns blue.
- **Error / Disabled:** form errors use the red note at the top of the page, one line per problem, naming the field and the fix. What was typed is kept. Read-only fields (the student's own email) take the Panel fill and a Soft Rule stroke.
- **Long forms** (the enrollment form) split into parts: each part has a 1px Soft Rule above it, its 13px/700 heading and an Ink 3 hint in a 200px left column, and its fields on the right. On a phone the parts stack.
- **Lists of people** (supervisors, members) are one ruled table each: 30px in-cell inputs, an Ink 2 role or row number in the first column, a quiet Remove button per optional row and an "Add" button under the table. On a phone each row becomes a block of labelled fields.
- **Fixed suffix:** an address that must be on one domain is typed as its local part, with the domain shown as a Panel-filled suffix joined to the input.

### Navigation
- **Style:** a sticky Routine Blue shell. The brand is a two-line lockup (lab name at 14px/700, "PC slots" at 12px in Routine Blue Soft). Tabs are 13px/600 in Routine Blue Soft and turn white on hover. The current page is white with a 3px white inset underline. The Enrollments tab carries a 2px-radius white count badge with blue numerals: enrollments waiting. The signed-in user and an outlined Sign out button sit at the right.
- **Mobile:** brand and user share the first row. The tabs move to a full-width horizontal scroller below.

### Status line
A Panel strip under the shell, always visible: the live slot and next change in bold, then each enforcement switch as a word with an 8px marker (solid green when on, hollow amber ring for dry-run, hollow Ink 3 ring when off). Red alert links and the amber pending count come last.

### Routine grid (signature)
The ruled weekly sheet. Free cells show an 8px Soft Rule dash. Approved groups stack as code marks (group tint, bold uppercase Ink code). Pending groups are hollow: a transparent fill, a 1.5px dashed Ink 2 outline and a trailing "?". Today's header cells are Routine Blue with white text, and today's body cells take the Today Tint. The live cell is fully reversed: Routine Blue fill, with approved marks inverted to white on blue. A key under the grid explains approved, pending, running now and today. The grid is read-only (the schedule is edited in the Google Sheet), so its cells have no pointer and no hover ring.

### Code mark
The group's identity in any context: bold 13px uppercase code, 0.03em tracking, padded 1px × 6px on the group's tint, 2px radius, ellipsis when it overflows. The pending variant is the hollow dashed form described above.

## Do's and Don'ts

### Do:
- **Do** frame every page with the Routine Blue shell and the Panel status line. State is always one glance away.
- **Do** separate days with 2px Ink rules, and separate halves and rows with 1px Soft Rules.
- **Do** show a group only as its bold uppercase code on its highlighter tint, and show pending as a hollow dashed code with "?".
- **Do** pair every state colour with a word ("Switch failed", "dry-run", "4 awaiting approval").
- **Do** build sections from a 2px Ink rule and a 17px heading, and use the flat Panel fill when a block must stand apart.
- **Do** keep numerals tabular and keep the grid at a single 13px size.
- **Do** keep Ink 3 (4.9:1) as the lightest text on white.

### Don't:
- **Don't** wrap content in cards with drop shadows, and don't add a grey sidebar. The admin-template look is what this world refuses.
- **Don't** use outer or ambient shadows. The current tab's inset underline is the only box-shadow.
- **Don't** use rounded-full pills for chips, badges or buttons. Corners stop at 4px, and circles are reserved for 8px status dots.
- **Don't** spend Routine Blue on decoration, illustration or secondary emphasis.
- **Don't** use a group's colour as text, border or background outside its code mark and swatch.
- **Don't** put uppercase eyebrow labels above headings. Uppercase tracked type is for column heads only.
- **Don't** invent or imitate a university logo or identity asset. The lab name in text is the brand.
