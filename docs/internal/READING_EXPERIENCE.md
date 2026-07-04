# READING_EXPERIENCE.md

The experience document for AI Vector. This is to *how the issue reads*
what `EDITORIAL.md` is to *how it sounds*. Owner: Experience Designer.

Its client is not the pipeline, the prose, or the archive — it is the
human holding the phone, who gives AI Vector about three minutes over
coffee. Every rule here is judged by one question: does this make those
three minutes more effortless, more oriented, more rewarding?

**Scope boundary.** This doc *specifies intent* ("summaries should sit at
65–75 characters per line"). It never contains CSS, template code, or
prose rewrites. Presentation intent → Release Engineer implements.
Text-unit patterns → Editor implements in voice, LLM Engineer encodes in
prompts. Product microcopy → proposed here, ratified by Editor + Arman.

---

## 1. The reading journey (as currently designed)

One issue is a four-beat arc the reader's hand learns as a ritual:

| Beat | Section | Reader's job | Turn the section takes |
|---|---|---|---|
| **Orient** | The Pulse | "What is today about?" | Hands the reader the day's single most important fact, then a plain take. |
| **Understand** | The Big Picture | "What does it mean?" | Names a structural thesis, then 3–4 stories that each pose a strategic question. |
| **Act** | Hands-On | "What do I do?" | Tools and methods the reader can run, each ending in an imperative. |
| **Watch** | Currents | "What do I keep an eye on?" | Early/thin-sourced signals, each ending in a calibrated stake. |

The arc is real and it works. A reader who reads only the Pulse is
oriented; a reader who reads to the end has moved orient → understand →
act → watch without being told they were on a journey. **This is the
publication's strongest structural asset. Protect it.**

The issue *ends* rather than stops: the footer + one-line "about" close
the turn. Currents is the correct final beat (watch = forward-looking).

### Section intro hand-off contract
Each section opens with a **bold thesis phrase** + 1–2 sentences of
explanation, then hands off to the stories. Observed thesis phrases:

- Big Picture: "Control is the product." / "Assumptions are the exposure." /
  "The foundations need checking." / "Architecture is the argument."
- Hands-On: "Clone first, commit later." / "Rent the stack tonight." /
  "Benchmark source matters today." / "Design for verifiability first."
- Currents: "Measurement is the missing piece." / "Quant tooling is
  getting sharper." / "A quiet day in the undercurrents." / "Small fixes,
  system-wide implications."

The bold-thesis intro is a **feature the reader's eye learns**. Keep it.
Watch one drift (see rationale log): the Big Picture thesis leans on the
nominal "X is the Y" frame — vary the grammar so the ritual stays fresh
rather than becoming a template.

---

## 2. The scanning-layer contract (as observed)

What a reader sees in the first ten seconds, in priority order:

1. **Masthead** — brand, weekday + date, "Issue No. N · ~M min read",
   "All Issues →". The read-time estimate is excellent orientation
   microcopy. Keep it.
2. **The Pulse** — tinted block, red rule, "Story of the day" label, one
   large headline. This is the single most important scanning object. It
   must be readable in one glance (see §3, Pulse headline rule).
3. **Section heads** — title + count ("4 stories" / "4 items"). Currents
   deliberately says *items*, not *stories* — a register signal that this
   section is terser and more provisional. Good; keep the distinction.
4. **Story titles** — the triage layer. A reader scans these to decide
   where the three minutes go. They must read as claims/artifacts, not
   teasers (recognition rule: names only if the reader knows them).
5. **Signal pills** (act/try/read/watch/discuss) — currently at the *foot*
   of each story. See rationale log R-4: they arrive after the read, not
   before, so they aid labelling more than triage, and they cross-cut the
   sections rather than reinforce them.

**Triage test:** can a reader pass once over headlines + section labels
and know where to spend their attention? Today: *mostly yes* on desktop,
weaker on the Pulse when its headline runs to three clauses, and the
pills add a second taxonomy the reader must reconcile.

### Trust flags (the calibration ritual)
Every story carries a source-quality flag in prose — "Single-source, no
independent replication yet." / "Code is public; benchmarks are
self-reported." / "No code yet." — placed consistently as the
second-to-last beat, between the fact and the stake. This three-beat body
rhythm (**fact → source-quality flag → stake**) is one of the
publication's best inventions. It is Grice's maxim of quality made
visible. **Protect it; keep the placement invariant.**

### Staging-only advisory layer
Duplicate-risk gate and verify flags are operator UI. They must never
render in `docs/released/`. Confirmed absent in all four released issues
reviewed. Guard this line from the reader's side.

---

## 3. The text-unit conversational contract (per section)

Each story is a short dialogue. The turns are:

- **Headline** — makes an honest bid for attention.
- **Summary body** — honours the bid: fact, then the source-quality flag.
- **Direction note / close** — the fused final sentence(s); answers the
  reader's "so what?" and hands the turn back.

There is no separate "direction note" field — the close is the tail of
the summary. That is fine, but it means the single most decision-relevant
sentence has no visual affordance in the scanning layer (see rationale
log R-5).

Each section has a **turn-type** the close should honour:

| Section | Close turn-type | Honoured when the close is… |
|---|---|---|
| Pulse | **Plain take** | A flat, declarative "here's what it means." |
| Big Picture | **Strategic question** | A question that lands the reader's own stakes. |
| Hands-On | **Imperative action** | "Do this specific thing, now." |
| Currents | **Calibrated stake** | Two-sided: if it holds → X; if not → still-useful Y. |

The turn-types are correct and mostly honoured. **The risk is not the
turn-type — it is that the *surface form* of each turn-type has calcified
into a near-invariant template** (see R-1, R-2). A pattern the reader's
hand learns is a ritual; a sentence scaffold the reader's eye predicts
word-for-word is a tic. The line between them is variety of surface form.

### Grice as the test
- **Quantity** — say enough for the turn, no more. Pulse headline R-3
  violates this by bundling three clauses into a one-glance object.
- **Quality** — nothing you can't stand behind. The trust-flag ritual
  satisfies this well.
- **Relation** — every sentence serves *this* turn. Generally strong.
- **Manner** — clear, ordered, unambiguous. The "if it replicates / if it
  doesn't" Currents close is clear but, repeated four times, its *manner*
  becomes monotone.

---

## 4. What is working (name it so it is protected)

1. The orient → understand → act → watch **arc**, and the fact that the
   issue *ends* rather than stops.
2. The **bold-thesis section intro** — one-glance structure before a word
   of story is read.
3. The **fact → source-quality flag → stake** three-beat body rhythm.
4. **Read-time microcopy** ("~4 min read") and the honest **section
   counts**, including *items* vs *stories* register.
5. The **recognition rule** in headlines — tool names (ComplianceGate,
   ReLaMix, ScarfBench, TabFM) live in bodies; headlines describe the
   artifact. This keeps the triage layer legible to a reader who doesn't
   yet know the names.
6. The **archive** (`docs/index.html`) — month-grouped `<details>`,
   `/`-to-search, "No issues match." empty state, tabular-num dates,
   headline-as-scan-column. It reads like a well-kept magazine rack, not a
   directory listing. The "A quiet day in the undercurrents" empty-state
   register is exactly right.
7. **Typographic restraint** — one accent colour (#e6452f), quiet
   greys, serif italic reserved for the tagline. No decoration competing
   with content.

---

## 5. Rationale log

Format: `R-n · date · finding · reader cost · proposed direction ·
status`. Status ∈ {observed, specced, ratified, shipped, rejected}.

### R-1 · 2026-07-04 · Hands-On closes are a near-invariant "…before X" scaffold
Across all four issues, ~95% of Hands-On stories close with
"[imperative] … **before [milestone]**": "check the badge *before* using
any score", "Run ScarfBench … *before* committing", "Add it … *before*
signing off", "run it … *before* renewing", "measure … *before* scaling",
"Use the scaffold … *before* commissioning one." The imperative turn-type
is correct; the surface form is not varied.
**Reader cost:** by the third or fourth story in the section the eye
predicts the sentence and stops reading it — the most actionable line in
each story is the one that goes unread.
**Direction (→ Editor / LLM Engineer):** keep the imperative turn-type;
diversify the surface form. Only some actions are genuinely
"before-a-milestone" gated; others are "do it this week", "start with X",
"measure Y first". Target: no more than ~2 "before X" closes per section.
**Status:** observed.

### R-2 · 2026-07-04 · Currents closes are a near-invariant "if it replicates / if it doesn't" scaffold
#21 uses the two-sided "If X replicates, [upside]; if it doesn't,
[still-useful]" close in **all four** items; #19 in all three. The
calibrated-stake turn-type is exactly right for a "watch" section — the
problem is that one grammatical mould carries it every time.
**Reader cost:** the section reads as one repeated sentence with the nouns
swapped; the calibration, which is the point, stops registering because
the frame is predicted.
**Direction (→ Editor / LLM Engineer):** preserve two-sidedness as the
*intent*; vary the construction (a single-clause stake, a "the tell will
be…", a "worth watching only if…"). #18 already mixes forms (2 of 4
two-sided) and reads better for it — that is the target rhythm, not the
all-two-sided form.
**Status:** observed.

### R-3 · 2026-07-04 · #21 Pulse headline breaks the one-glance promise
"EU delays AI compliance deadlines, makes content labelling mandatory but
its icons optional" is three clauses welded with a comma and a "but." The
Pulse headline is the single most important one-glance object in the
issue; #18/#19/#20 Pulse headlines are clean single ideas (9–11 words).
This one also propagates into the **archive scan column**, where it wraps
and out-weights its neighbours.
**Reader cost:** the reader parses three facts to find the story; the
one-glance orient beat becomes a two-second decode.
**Direction (→ Editor — presentation-fights-prose flag, not a rewrite):**
the Pulse headline should carry one idea; the nuance ("icons optional")
belongs in the body. This is a voice fix, not a CSS fix.
**Status:** observed.

### R-4 · 2026-07-04 · Signal pills add a second taxonomy and arrive too late to triage
The act/try/read/watch/discuss pill sits at the *foot* of each story,
after the body, and does not map cleanly to sections: Big Picture in #20
carried act/discuss/watch/read (all four different) — so the pill
fragments the section rather than reinforcing it, and in #21 an "act" pill
sits on a Big Picture story whose close is a reflective question.
**Reader cost:** the reader reconciles two overlapping triage systems
(section identity *and* pill), and the pill — placed after the read —
labels rather than guides. Uncertain it earns its Grice-quantity keep.
**Direction (consult — pill *existence* is Editor/Arman's; pill
*presentation* is mine):** two open options to spec and test, not decide
unilaterally — (a) if pills stay, align each pill's meaning with its
section's turn-type so the two axes stop competing, or (b) move the
verdict to a pre-read position (a lead-in on the headline line) so it aids
triage. Also revisit 9px uppercase legibility on phones (near the size
floor). Bring to a presentation review.
**Status:** observed.

### R-5 · 2026-07-04 · The stake/direction sentence has no scanning affordance
The close (the "so what") is the most decision-relevant sentence but is
the fused tail of a body paragraph, visually identical to the fact
sentences before it. A reader scanning headline + first line never sees
it.
**Reader cost:** the reader must fully read each body to reach the payoff;
the scan layer can't surface "here's what to do about it."
**Direction (spec, to bring to a presentation review — restraint
applies):** consider a *quiet* affordance for the closing sentence
(e.g. a subtle lead-in, weight, or measure change) that lets a scanner
catch the stake without reading the whole body. Must not add chrome or
break the fact → flag → stake rhythm. Explicitly a proposal, not a
decision.
**Status:** observed.

### R-6 · 2026-07-04 · "1 items" pluralization defect (#20 Currents count)
On a single-item Currents day, the section count rendered "1 items". The
archive script pluralizes correctly ("1 issue"); the issue-template
section count does not.
**Reader cost:** a small but real credibility ding on the exact day the
lovely "A quiet day in the undercurrents" microcopy is trying to make
scarcity feel intentional — the grammar undercuts the grace.
**Direction (→ Release Engineer):** pluralize the section count
("1 item" / "N items", "1 story" / "N stories"). Trivial, high-polish.
**Status:** observed.

### R-7 · 2026-07-04 · Big Picture close leaks non-question turn-types
Big Picture's turn-type is the strategic question, but the surface leaks:
#21 story 1 closes "raise this before your next data-residency design
review" (a Hands-On-style imperative), and #18 closes three of four with
"this is the [methodology/gap/benchmark] to [pressure-test/name/argue]
against" (a statement scaffold, itself a within-issue tic).
**Reader cost:** the section's "understand / reflect" register blurs into
the "act" register, softening the orient→understand→act progression.
**Direction (→ Editor / LLM Engineer):** hold Big Picture closes to the
strategic-question turn-type more consistently; where a story genuinely
wants an imperative close, that may be a signal it belongs in Hands-On.
**Status:** observed.

### Ritual-sameness watch (not yet a finding)
Two of four reviewed issues (#18, #21) lead the Pulse with an EU-AI-Act
story from the same source. Given the news cycle this is defensible, but
a run of regulatory Pulses is a ritual sameness a daily reader may feel.
Flag for the Editor's awareness; not an experience defect yet.

---

## 6. Open questions for Arman / the team
- Do the signal pills earn their place, and if so, is their job triage
  (→ move them up) or archival labelling (→ leave them, accept they're
  post-read)? (R-4)
- Is a quiet scanning affordance for the stake sentence worth one
  controlled deviation from pure body prose? (R-5)
- How much surface-form variety in section closes is worth asking the LLM
  prompts to carry, versus accepting some template as the cost of a daily
  pipeline? (R-1, R-2)
</content>
</invoke>
