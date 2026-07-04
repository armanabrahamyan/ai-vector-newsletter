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
Every story carries a source-quality flag in prose — "Single-source
interview." / "Code is public; benchmarks are self-reported." / "Scores
come from the paper's own LLM-judge ensemble." — placed consistently as
the second-to-last beat, between the fact and the stake. This three-beat
body rhythm (**fact → source-quality flag → stake**) is one of the
publication's best inventions. It is Grice's maxim of quality made
visible. **Protect it; keep the placement invariant.**

The flag's *form* is constrained: it characterises the evidence that
exists; it never inventories what is missing. See §3, "Presence, not
absence" (Arman's direction, 2026-07-04). Note: before 2026-07-04 this
section itself listed "No code yet." as an example flag — that form is
retired.

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

### Presence, not absence, AND non-default — the trust-flag form rule
*Arman's direction, 2026-07-04, refined the same day. First: "acknowledge
if it is present but don't say it is not present and any other move like
that — please remove." Then, pushing deeper: a presence-form flag that
merely restates the default of its evidence class ("a preprint from a
single research team") still says nothing a DS/engineer reader didn't
already know from the word "preprint."*

A trust flag must pass **three independent gates**. Fail any one and it
should not appear:

1. **Source-supported** [v0.17] — the characterisation is one the source
   explicitly supports; never invented.
2. **Presence-form** [R-8] — it describes evidence that EXISTS; it never
   inventories what is missing.
3. **Informative vs. the evidence-class default** [this refinement] — it
   tells the reader something the source-class name did not already tell
   them.

**Grounded in a reader-needs study (2026-07-04), not craft doctrine
alone.** Two evidence streams confirm the three gates:

*Archive-as-evidence (the verifier as reader-proxy).* Across #20–#23,
the verifier's verdict on trust-flag claims splits almost perfectly along
the presence/absence line. **Absence-form flags come back `unverifiable`:**
"No code is public yet", "No code is public", "No code yet", "No
independent replication yet", "Single-source, no code released",
"Vendor-published benchmark, no independent replication yet" (the absence
half), "Single-author post, no benchmark data" — all `unverifiable`.
**Presence-form flags come back `supported`:** "Single-source,
practitioner-reported", "Single-source, no independent benchmarks yet"
(where "single-source" is source-stated), "Benchmarks are self-reported",
"Single-source, pre-publication". The verifier is a skeptical reader
standing in for ours; it consistently *cannot confirm* the absence-flags,
which means a real reader couldn't either — and the source often never
said it. This is gate 1 and gate 2 confirmed on disk.

*Persona panel (five subscriber archetypes walked through real
flagged stories at three-minute reading speed).* The full findings are in
R-8; the load-bearing results: every persona **skipped** flags that
restate the class default ("no independent replication yet" on a fresh
preprint — "of course it isn't", the quant researcher) and **used** flags
that mark deviation ("vendor-published benchmark" on the ScarfBench story
where an agent claimed 29/30 but only 22 held — the platform engineer's
single most-used flag). Even the two personas most drawn to
absence-language — the model-risk officer and the artifact-hunting builder
— were better served by presence-form: the risk officer needs "this is a
vendor announcement" (a class name she infers "unvalidated" from), never
"no independent validation"; the builder needs the *affirmative* "code and
weights are public" when true, and is actively harmed by an unverifiable
"No code is public" that may be a guess. Therefore the pattern below.

**The class attribution carries the default calibration for free.**
Naming the source class in the body — "an arXiv preprint", "Anthropic's
release notes", "a single podcast interview", "a Reddit thread" — already
tells a DS/engineer reader what to assume: preprint ⇒ single team, not
peer-reviewed, authors' own scoring; release notes ⇒ the vendor's own
numbers and framing; Reddit ⇒ anecdote, n=1. So the DEFAULT posture needs
no separate flag at all. An explicit trust flag earns its six words only
when the evidence **deviates** from its class default — up or down.

**Deviation taxonomy — when a flag is informative:**

| Evidence class | Default (no flag; body's class-name carries it) | Deviation that EARNS a flag |
|---|---|---|
| arXiv preprint | single team, not peer-reviewed, authors' own scoring | independent replication PRESENT; multi-lab authorship on a dramatic claim; scored by a *third party* |
| Vendor blog / release notes | vendor's own numbers, vendor's framing | benchmark presented as if neutral/third-party; a *competitor* ran the comparison; an independent audit is cited |
| Named-author experiment / blog | one practitioner's setup, n=1 | reproduced by others; run at production scale |
| Reddit / forum thread | anecdote, n=1 | (rarely deviates; if a big claim rests on it, see magnitude below) |

Downward deviation counts too: flag when the evidence is *weaker* than its
class name implies — "a benchmark, but the vendor scored its own rival's
model" — because "benchmark" promised more rigour than the evidence
delivers.

**The claim-magnitude interaction — whose job is it?** A dramatic claim
resting on default-weak evidence (a field-redefining result in a
single-team preprint; a big number from one Reddit thread) is a real
calibration the reader needs. But that mismatch is the **stake's** job,
not the flag's. The flag reports what the evidence IS; the Currents
calibrated-stake close and the Big Picture strategic question already
carry claim-vs-evidence tension in-voice ("if it replicates → X; the
evidence is one team's preprint"). Keeping them separate stops the flag
from becoming a hedge the close then repeats. One licensed exception:
"thin sourcing, one Reddit thread" on a big claim is a legitimate *flag*
because the deviation IS the magnitude/evidence mismatch — but say it
once, in the flag OR the stake, never both.

**Passes all three gates (KEEP) — presence-form AND non-default:**
- "the vendor's own competitor ran the comparison" — deviation: who scored it.
- "independently replicated by a second lab" — deviation: replication present on a preprint.
- benchmark "presented as neutral, but the numbers are the vendor's own" — deviation: framing vs. source.
- "scored by an ensemble of LLM judges" — deviation: the scoring *method* (a mechanism the reader can't infer from "preprint"; informative in a way "authors' own" is not).

**Fails gate 3 — presence-form but default-restating (DROP; let the body's class-name carry it):**
- "A preprint from a single research team." — single-team is the preprint default. (This was a RIGHT-side example in the first draft; Arman was right to reject it.)
- "one research team's analysis" — same.
- "Single-author experiment" when the body already says "Simon Willison ran…".
- "Vendor-published benchmark" when the body already names the vendor as author.

**Fails gate 2 — absence-inventory (REMOVE), examples from R-8:**
- "No code is public yet." (verbatim ×3 in #23 alone)
- "no independent replication yet" / "no independent benchmarks yet"
- "No code is linked." (#22 Pulse — the verifier marked this claim
  *unverifiable*: the source never mentioned code availability at all)
- "peer review pending" (absence dressed as status)
- early-era exotics that prove the unbounded-set point: "No regulatory
  framework yet", "No patch exists yet", "no stable tag yet" (#3–#7 era).

**Why gate 2 (Grice, quantity + relation).** The absence of a thing is an
unbounded set — the summary could equally truthfully say "no mobile app
yet" — so naming one absence is arbitrary and reads as filler-hedging. The
trailing "yet" adds a speculative promise the source never made; a global
negative asserted from a local excerpt is usually unverifiable by
construction; and the absence-clause is typically *entailed* by an
adjacent presence-form ("Single-source interview; no independent
benchmarks" — the second clause adds nothing).

**Why gate 3 (Grice, quantity again).** A flag that restates the class
default is informationally empty relative to the class name the reader
already parsed. It costs six words of a 60-word budget and delivers zero
bits. Gate 3 is what stops the presence-rewrite of gate 2 from
manufacturing new empty flags — the failure mode Arman caught in the first
draft.

**The affirmative-presence obligation (the positive half of the ban).**
The reader-needs study (R-8, the builder stress-test) shows the ban on
absence must be paired with a duty to state presence: when the source
supports that an artifact IS available, say so in presence-form — "code
and weights are public", "dataset and tooling are public". This is what
the artifact-hunting builder actually acts on, and it is source-supported
(gate 1) and non-default (gate 3 — availability is not assumable from
"preprint"). Do not merely delete "no code"; where the source states
availability, *acknowledge it*.

**The rewrite moves, in order of preference:**
1. **Name the source class in the body, drop the flag entirely.** If the
   calibration is just the class default, one noun phrase in the body
   ("an arXiv preprint", "Anthropic's release notes") replaces the whole
   flag. This is now the *most common* fix — an absence-inventory that was
   also a default-restatement fails gates 2 and 3 at once, and both
   clauses go.
2. **Keep the flag only on deviation**, phrased as presence ("a second lab
   replicated it", "the vendor scored its own rival").
3. **Push claim-vs-evidence mismatch into the close/stake**, not the flag.
4. **Source-stated, decision-relevant absence → report the actor's action.**
   If the source itself states an absence that matters ("weights withheld
   for safety", "code released on acceptance", an advisory noting no patch
   yet), report it as what the actor did — a presence-form. This is rare
   and must clear all three gates; if the source doesn't state it, v0.17
   already forbids asserting it.

**Composition:** three gates, all required — **source-supported AND
presence-form AND informative-vs-default**. None substitutes for another.

**Boundary note:** "no action yet" in a Currents direction-note is a
*recommendation to the reader*, not an evidence inventory — a different
speech act, and allowed. The WHY that follows it must still be
presence-form AND non-default ("Vercel's own eve framework", not "no
independent validation yet", and not the empty "a single vendor" when the
body already named the vendor).

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

### R-8 · 2026-07-04 · Absence-inventory has become the reflex trust-flag form (Arman's direction: remove)
Audit of released issues: stories whose summary contains at least one
absence-statement ("No code is public yet", "no independent replication
yet", "no independent benchmarks", "peer review pending", …) — #23: 6 of
8; #22: 6 of 9 (including the Pulse, where the verifier marked "No code
is linked" *unverifiable*); #21: 5 of 13; #20: 4 of 10 plus one in the
Currents section intro; #19: 8 of 13; and the density runs back to issue
#1 (2026-05-23 era issues show 5–9 per issue, including arbitrary
absences like "No regulatory framework yet" and "no stable tag yet").
Diagnosis: the trust-flag discipline taught the model to hedge, and
absence-inventory became its cheapest hedge form — it can be produced
without reading the source closely, which is exactly why it decorates
even release-notes stories ("Vendor release notes only; no independent
benchmarks" — nobody benchmarks a changelog; a relation violation).
**Reader cost:** a sentence per story that carries no information (often
entailed by the adjacent presence-form), plus a speculative "yet"
promise, plus a factual-integrity risk (unbounded negatives are
unverifiable from an excerpt). Roughly half the issue's trust-flag beats
spend the reader's attention on voids.
**Direction:** three-gate trust-flag rule written into §3 ("Presence, not
absence, AND non-default"); §2 trust-flag examples corrected;
implementation spec issued to LLM Engineer.
**Refinement (Arman, same day) — the third gate.** The presence-form fix
alone is not enough: a presence-form flag that restates the *default* of
its evidence class ("a preprint from a single research team") is still
informationally empty, because AI Vector's DS/engineer readers already
know what "preprint" implies. So the composed rule gained a third gate —
**informative vs. the evidence-class default** — and the class attribution
in the body ("an arXiv preprint", "Anthropic's release notes") now carries
the default calibration for free; an explicit flag appears only on
*deviation* from the class default. Full deviation taxonomy in §3.

**Reader-needs study (2026-07-04) — the three gates as tested hypothesis.**
Arman's standing direction: rulings trace to studied reader needs, not
doctrine. The three-gate rule was treated as the hypothesis and validated
two ways.

*Archive-as-reader-proxy (verifier verdicts, #20–#23).* The advisory
verifier's verdict on trust-flag claims splits along the presence/absence
line. Absence-form flags return `unverifiable` (the source never stated
the lack): "No code is public yet", "No code is public", "No code yet",
"No independent replication yet", "Single-source, no code released",
"Single-author post, no benchmark data", and the absence half of
"Vendor-published benchmark, no independent replication yet". Presence-form
flags return `supported`: "Single-source, practitioner-reported",
"Benchmarks are self-reported", "Single-source, pre-publication",
"Single-source, no independent benchmarks yet". High invention/unverifiable
rate + prior Arman pre-release fixes (v0.17 changelog: verifier caught
invented hedges three times in three days) = these flags were serving the
*writer's reflex*, not the reader. Gates 1 and 2 confirmed on disk.

*Persona panel (five archetypes × real flagged stories, three-minute
mode).* Per persona — need at decide-moment, and verdict on today's flags:

| Persona | Needs at decide-moment | Used (informative) | Skipped as noise | Verdict on 3-gate rule |
|---|---|---|---|---|
| **Quant researcher** (reproduce a paper?) | evidence class; *who scored it* (authors vs third-party); is data/code reproducible | OCB "scoring runs on the paper's own LLM-judge ensemble" (scoring *method* — a deviation) | "no independent replication yet" ("of course — it's a fresh preprint") | Confirms. Needs scoring-provenance deviation, not absence. |
| **ML platform engineer** (adopt tooling?) | vendor's-own vs neutral numbers; maturity | ScarfBench "vendor-published benchmark" (his single most-used flag — the story shows 29/30 claimed, 22 real) | "no independent benchmarks" on a brand-new vendor CLI (default) | Confirms. Benchmark authorship is the deviation that matters. |
| **Model-risk officer** (control gap?) | source class (vendor announcement vs independent study vs regulator) | "single vendor announcement" on OpenAI Daybreak | "no public code exist yet" (irrelevant — she isn't cloning) | Confirms. Infers "unvalidated" from the class name; never needs the absence form. |
| **Eng director** (forward to whom?) | one calibration bit: how much can I trust this? | class name + one deviation flag | every "no code yet" | Confirms. Already knows preprint = early. |
| **Hands-on builder** (clone tonight?) | IS there an artifact to run? | OCB "dataset and tooling are public" (affirmative, decisive) | — | Confirms *with a rider* (below). |

**The builder stress-test (the rule's riskiest consequence).** Under the
three gates most absence-flags get deleted; the builder is the persona who
seemed to need "No code is public" so as not to hunt a repo that isn't
there. The study resolves this against bare absence, on evidence: (1) those
flags are exactly the ones the verifier marks `unverifiable` — they are
often the writer's *guess*, so they may stop a builder from a repo that
does exist or send him hunting on a false premise; (2) the class name in
the body already carries the default ("an arXiv preprint" ⇒ code not
guaranteed); (3) what the builder actually acts on is the *affirmative*
signal — "dataset and tooling are public", "weights and code are public" —
which is presence-form and source-supported. So the builder is served
better, not worse, on one condition: **presence-form must be affirmative,
not merely non-absent — when the source states an artifact IS available,
the flag must say so.** That is the positive half of Arman's own words
("acknowledge if it is present"), and it becomes an explicit obligation in
the spec, not just a ban on the negative.

**Divergence surfaced, not averaged:** the only genuine cross-persona
tension is that the builder wants artifact-availability signals the
risk officer never reads. This does not fracture the rule — both are served
by the same presence-form move (affirmative when present; class-name
default when the source is silent). No persona needs bare absence-inventory.
Nothing to escalate to Arman beyond ratifying the affirmative-presence
obligation.

**Ruling:** all three gates confirmed. One amendment the study adds — the
**affirmative-presence obligation** (say what IS available when the source
supports it), folded into gate 2. The amendment spec to the LLM Engineer
stands as issued, with one delta: add the affirmative-presence instruction
alongside the absence ban (spec item A/D).

**Status:** ratified and study-validated (Arman, 2026-07-04, incl. same-day
refinement); implementation pending (spec + amendment issued to LLM
Engineer).

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
