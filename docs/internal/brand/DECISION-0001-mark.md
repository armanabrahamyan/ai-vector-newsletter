# Decision record 0001 — the AI Vector mark

**Status: RATIFIED — 2026-08-09.** The mark is *The Open Block*, in primary
ink, on the issue masthead, the index hero and the favicon. This file records
what was explored so that rejected ground is not walked again by accident.

- **Date drafted:** 2026-08-09
- **Author:** brand-designer
- **Decides:** which mark AI Vector uses as its favicon and as a visible mark on
  the issue and index pages
- **Presentation:** `docs/internal/brand/logo-presentation.html`
- **Geometry source:** `tools/brand_marks.py` → `docs/internal/brand/candidates/`
- **Recommended:** *The Open Block* (`docs/internal/brand/candidates/a-open-v.svg`)
- **Ratified:** *The Open Block*, in primary ink — canonical at
  `docs/brand/aiv-mark.svg`
- **Date ratified:** 2026-08-09

---

## The question

The publication had a favicon (`docs/favicon.svg`: a V with a chevron floating
above it, in two colours on an opaque white square) and no mark on the pages.
The operator wants the identity present on the issue and index pages, not only
in the browser tab. That raises the size problem immediately: the same drawing
has to work at 16 px in a tab and at 22 px beside a 32 px Newsreader wordmark,
so it was judged at those sizes first and admired later.

---

## What was drawn, and the verdict

Six marks were drawn as hand-authored single-path SVGs, square `viewBox`,
`fill="currentColor"`. Each was rendered by a real browser at a device scale
factor of 1 — true 16 px pixels — and inspected magnified. The raster is
embedded in the presentation page as evidence.

| Mark | The idea in one line | Verdict |
| --- | --- | --- |
| **The Open Block** | A block of ink with a leaning V cut out of it; the long arm takes the top-right corner off | **Recommended** |
| Waterline | The masthead rule taken off the horizontal, cut out of a block | Survives 16 px; rejected |
| The Overshoot | A Newsreader-weight V whose thin arm rises past the cap line, absorbing the ancestor's chevron | Survives 16 px; rejected |
| The Standard | The slash of *AI/Vector* planted on the masthead rule | **Fails 16 px** |
| Magnitude | A single tapered stroke — mass at the tail, nothing at the tip | Survives 16 px; rejected |
| Caret & Rule | The ancestor's chevron kept and the V retired: a heading above the line | **Fails 16 px** |

### Why each rejected one was rejected

- **Waterline** — clears legibility and misses distinctiveness. It carries no
  trace of the name, and a block cut by a diagonal reads at a glance closer to a
  negation symbol than to a heading.
- **The Overshoot** — legible once the thin arm was thickened, but it repeats a
  letter the wordmark already contains in Newsreader's own hand. In the lockup it
  reads as a stray glyph that fell out of the wordmark rather than a mark
  standing beside it.
- **The Standard** — at 16 px the foot of the slash and the rule merge into one
  smear and the residue reads as a tick on a line. Thickening both elements made
  it heavier, not clearer: the failure is the acute junction, not the weight.
- **Magnitude** — clean at every size, but beside the wordmark it reads as a
  checkmark, which implies a verification the newsletter does not claim to
  perform. A mark that says the wrong thing clearly is worse than one that says
  nothing.
- **Caret & Rule** — at 16 px the apex blunts and the arms fuse with the rule,
  leaving a roof or an eject glyph.

---

## Directions considered and not drawn

Recorded so they are not re-opened without new reasoning.

- **Three sheared rules of decreasing length** (a block of text leaning into its
  heading). Collides head-on with the hamburger menu icon at exactly the sizes
  the mark must work at.
- **Ascending bars / a chart line.** The finance lens makes this tempting and
  that is precisely the trap: it dates the publication to a sector rather than
  identifying it, and bar-chart marks are the most crowded space in the
  category.
- **An A/V monogram sharing a diagonal.** The shared-stroke construction lands
  on a zigzag that reads as a price chart — two ideas in one mark, and the
  second one is the wrong idea.
- **A compass needle or a wedge inside a disc.** A bounded disc with a counter
  is another publication's territory in this operator's portfolio; adopting it
  would blur two identities that should not resemble each other.
- **A literal arrow, in any form.** Ruled out by the brief and by the name: a
  vector carries direction, and drawing an arrowhead is the cliché that admits
  the shape could not carry it alone.
- **A dog-eared page corner.** Editorial and pleasant, but it encodes no
  direction, so it answers only half the name.

---

## Rulings that hold regardless of which candidate is chosen

These came out of the exploration and are worth keeping even if the
recommendation is not the mark that is ratified.

1. **The ancestor splits, and the V is the half worth keeping.** Of the two
   pieces in `docs/favicon.svg`, the V survives at favicon size and the floating
   chevron does not — *Caret & Rule* was drawn specifically to test the chevron
   on its own and it failed at 16 px. The chevron is retired.
2. **One ink — recommended, not settled.** Every candidate is `currentColor`.
   A vermilion-block variant of the recommended mark was drawn and rendered
   beside the wordmark and in a tab shelf; it is genuinely competitive and is
   the more distinctive of the two at 16 px. It is recommended against because
   the accent is already spent on the slash in *AI/Vector*, and a vermilion
   block 14 px to its left demotes the slash from the publication's one
   emphatic gesture to an echo. This is the single ruling in the record most
   likely to be overturned, and it is a one-attribute change either way.
3. **Mark height equals wordmark cap height.** Measured, not chosen: Newsreader
   500 has a cap height of 0.6816 em (21.81 px at the 32 px masthead, 36.31 px
   at the 52 px hero). Above roughly 1.1 × cap, a block mark shouts down the
   serif.
4. **Minimum solo size is 14 px**, measured on a true 1 × raster. This is why
   the footer colophon — a 19 px wordmark, 12.75 px cap — keeps the wordmark
   alone rather than breaking the size relationship to fit a mark in.

---

## Observation logged in passing — now resolved

Found while reading the render surface: `templates/issue.html.j2` pinned the
Safari mask-icon colour to `#a83a2a` while the ratified accent
`oklch(0.49 0.135 30)` renders as approximately `#9f3b2e` — two different
vermilions.

The question is moot as of ratification. The mark is ink, so the mask-icon is
ink too (`#3b352d`, matching the favicon's light-mode fill). No vermilion
remains in either template's `link` tags.

---

## Ratification — what was decided and applied

Ratified 2026-08-09: **The Open Block, in primary ink, everywhere.**

- [x] Candidate ratified: *The Open Block*, 2026-08-09
- [x] Canonical path promoted to a single source of truth at
      `docs/brand/aiv-mark.svg`. `tools/brand_marks.py` is kept as the
      generator that produced the geometry — it still writes the six
      candidates, now to `docs/internal/brand/candidates/`, so the geometry can
      be regenerated rather than redrawn. The canonical path is byte-identical
      to the path the generator emits for `a-open-v`.
- [x] Integration applied by the release-engineer per the spec: issue masthead
      at 22 px with a 14 px gap, index hero at 36 px with a 23 px gap and both
      breakpoints mirrored, footer colophon unchanged.
- [x] `docs/favicon.svg` regenerated from the canonical path: transparent
      ground, explicit `#3b352d` fill with a `prefers-color-scheme: dark`
      query to `#f4f0e7`.
- [x] Mask-icon colour corrected in both templates to `#3b352d`.
- [x] File locations settled: `docs/` is the published site, so the
      presentation, this record and the candidate drawings moved to
      `docs/internal/brand/`. Only `aiv-mark.svg` and `favicon.svg` remain
      public.

### The vermilion variant

A vermilion mark was considered on a real issue page and **declined**. The
accent is a single, scarce signal and it is already spent on the wordmark
slash; a second vermilion object 14 px away halves the value of both. The mark
is ink everywhere, with no accent variant sanctioned.

### The retired ancestor

The previous favicon — a two-colour V with a chevron floating above it, on an
opaque white square — is **retired**. It was never a mark, only a tab icon; it
carried a cold `#ffffff` ground against the identity's warm paper, and its
vermilion chevron competed with the wordmark slash for the same signal. It has
no successor role.

### Still open

- [ ] Rendering confirmed in Safari and Firefox — everything in this record,
      and every raster in the presentation, was verified in headless Chrome
      only. `fill-rule="evenodd"` at 16 px is exactly the kind of thing that
      differs between rasterisers.
- [ ] The index breakpoint sizes (27 px and 23 px) are computed from
      Newsreader's cap-height ratio, not measured. Worth one look in a browser.
- [ ] Where the canonical SVG lives so a downstream fork can swap it without
      editing templates — a contract question for the architect, touching
      `config/brand.yaml` and the render path. The template layer currently
      includes `templates/_mark.svg.j2`, which is a derivative of the canonical
      file, not a fork seam.
