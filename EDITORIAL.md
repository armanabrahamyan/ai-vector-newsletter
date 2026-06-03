# EDITORIAL.md — AI Vector voice and section taxonomy

*"Today's AI, with a heading."* Author: Arman. Editor: this document.

This is the editor's working voice document. It is not the public masthead
(README.md) and it is not the technical contract (docs/internal/DESIGN.md).
It sits between them: *what the publication should sound like* and *what
each section is for.* Arman owns voice; the editor keeps this document
sharp so the LLM Engineer's prompts and the Eval Engineer's rubrics have
something specific to reference.

*Updated 2026-05-31: per-section closing shapes.* Each section now has a
distinct closing rhythm so the last sentence itself signals the section.
Frames at `_scratch/2026-05-31-closing-frames.md`.

---

## What the publication sounds like

Six rules that should be enforceable by the summarise prompt and checkable
by the voice rubric. Rule 0 is **the** principle; the rest are how it
manifests.

0. **AI Vector has takes.** Voice is not balance. Voice is not hedge.
   Voice is not "trust but verify." Voice is naming what's true now and
   where the field is moving, with the confidence of an editor who picked
   this story for a reason. If the closing reads as "be cautious about
   this," the voice is absent. If it reads as "this is the news; here's
   what shifts," the voice is present.

### Headlines that land

A headline that needs a second read is a headline that failed. The
reader skims; the headline carries the story. Three rules for getting
there.

**Rule 1 — Name the artifact in the headline only when readers will recognise the name.**

> Names belong in the headline when a senior practitioner reads them and
> connects to a mental model they already have. OpenAI, Anthropic, Claude,
> GPT-5, Llama, vLLM, EU AI Act, Nvidia — all earn the name in the headline
> because recognition is immediate. New tools, new papers, new benchmarks,
> new methods — JudgmentBench, COLLEAGUE.SKILL, dot-skill, ITS-Mina,
> FinGuard — do NOT belong in the headline. For these, describe what the
> thing does in the headline and let the name appear in the first one or
> two sentences of the summary.
>
> Why: a name in a headline that does not trigger recognition is noise. It
> is a string of letters the reader has to mentally park while they figure
> out what the headline is about. The summary is where new names earn their
> place — by then the reader knows what kind of thing they are reading
> about and the name can attach to the mental model.
>
> The test: would a senior practitioner reading the headline alone
> recognise this name without further context? If no, the name does not
> belong in the headline.
>
> *In voice (known name):* "Anthropic ships a more honest flagship model"
>
> *In voice (unknown name, described not named):* "A Shanghai AI Lab paper
> turns a departing engineer's traces into an editable skill file (no proof
> yet it captures judgment)"
>
> *In voice (unknown name, described not named):* "A new benchmark finds
> pairwise scoring beats rubrics for judging language-model output"
>
> *Off voice (unknown name in headline):* "COLLEAGUE.SKILL packages a
> departing engineer's judgment as a skill file" — the reader does not
> know what COLLEAGUE.SKILL is, so the name does not help.
>
> *Off voice (unknown name in headline):* "Capture a departing engineer's
> judgment as a versioned, editable file" — different failure mode: no
> name AND no description, just an instruction the reader cannot ground.

**Rule 2 — Preserve distinctions the source makes.**

> When the source distinguishes between an existing artifact and a new one
> — an existing tool and a new paper that reinterprets it, an existing
> benchmark and a new technique tested against it — the summary preserves
> that distinction. Collapse them and the reader loses the news.
>
> *In voice:* "A new Shanghai AI Lab paper, COLLEAGUE.SKILL, reframes the
> existing dot-skill repo (18,500 GitHub stars) as a portable file format
> rather than a digital twin."
>
> *Off voice:* "dot-skill (titanwings/colleague-skill, MIT, 18,500 GitHub
> stars) reads an engineer's code reviews and writes them into a Markdown
> file." — the reader cannot tell what is new.

**Rule 3 — A headline lands when it reads in one beat, not two.**

> A reader should not have to back up and re-read. Tests for this:
>
> - **Concrete subject up front.** Name the actor, vendor, paper, lab, or
>   artifact in the first three words. "OpenAI maps its safety practices
>   to EU rules" — subject in the first word. "Anthropic ships a more
>   honest flagship model" — same.
> - **Specific objects, not abstract nouns.** "An editable file format for
>   engineer expertise" beats "A versioned, editable container for tacit
>   knowledge." Concrete things land. Abstract reframes stall.
> - **One core idea.** If the headline needs a colon, semicolon, or em-dash
>   to hold two ideas together, it is two headlines. Pick the more
>   important one.
> - **No editorial dictionary lookups.** Words like "versioned,"
>   "operationalise," "primitives," "instrumentation" are jargon. Use
>   plainer words — "tracked," "make routine," "building blocks,"
>   "monitoring."
> - **Imperative is allowed only when the artifact is named.** "Capture X
>   as Y" works when "Y" is a thing the reader has already heard of. When Y
>   is being introduced for the first time, the imperative obscures it.
>   Use declarative instead: "A new tool packages X as Y."
>
> *In voice:* "A Shanghai AI Lab paper turns a departing engineer's traces
> into an editable skill file (no proof yet it captures judgment)"
>
> *Off voice:* "Capture a departing engineer's judgment as a versioned,
> editable file"

When the editor's review flags a headline that did not land, add the
specific phrase to the anti-patterns catalogue at the end of this
document.

1. **Warm, not chummy.** Trust the reader; don't perform for them. Cut
   adjectives that don't earn their place. "Major," "powerful,"
   "groundbreaking" are almost always cuttable.
2. **Point, don't list.** Every section says where the field moved and
   which way it's heading. A section that just enumerates is a failure.
3. **Signal density.** Three sentences should carry as much as a paragraph
   of the trade press. If a sentence doesn't push the reader toward a
   decision or a frame, cut it.
4. **Finance lens lands when it lands.** Moderate, not maximum. When the
   FS angle is forced, it's worse than absent — readers feel the reach.
5. **No emojis, no exclamation marks** unless Arman explicitly asks. No
   "🚀". Ever.

Quiet nod to the vector → direction → pulse lineage in the section names.
Never explain the joke.

### Strong vs hedge — what rule 0 looks like on the page

The same story, edited two ways. The hedge version sounds careful; the
strong version sounds like a publication. AI Vector is in the position
business. Three pairs per section so the contrast is unmistakable.

**The Pulse**

*Strong (in voice):* "The IDE plug-in era is over. The vendors who win the
next round own the runtime — the VM, the spec, the PR — not just the
autocomplete."
*Hedge (off voice):* "Coding tools may be evolving toward more autonomous
agents, though the timeline remains uncertain."
The difference: the strong version takes a position about where the
frontier is. The hedge describes a trend.

*Strong:* "Anthropic just told you the truth about a release. That's the
news. Whether to swap is procurement; whether the lab is honest is
strategy."
*Hedge:* "Anthropic's disclosure is a notable development that some
observers may find reassuring."
The difference: the strong version names what changed and separates the
two decisions. The hedge gestures at significance without claiming any.

*Strong:* "Domain-grounded safety filtering is where the credible
regulated-AI story starts now. Open-web filters have hit their fit limit."
*Hedge:* "There are growing questions about whether general-purpose safety
filters meet the needs of regulated industries."
The difference: the strong version says *what comes next*. The hedge
raises a question and walks away from it.

**The Big Picture**

*Strong (in voice):* "A runtime compliance score could replace the
once-a-year AI audit — and the firms that build the score win the next
governance cycle."
*Hedge (off voice):* "Runtime monitoring approaches are emerging as a
possible complement to traditional audit processes."
The difference: the strong version names a first-order consequence and a
winner. The hedge names a category.

*Strong:* "When the safety filter and the regulator's rulebook disagree,
the rulebook governs — and your customer-facing stack now has a procurement
question that wasn't on last quarter's roadmap."
*Hedge:* "There may be tensions between AI safety mechanisms and
regulatory frameworks that organisations will need to consider."
The difference: the strong version sends the reader to a specific decision.
The hedge sends them to a meeting.

*Strong:* "Pre-deployment governance is losing ground to runtime
governance. Boards that haven't reorganised the AI-risk committee around
that shift are reviewing last year's risk model."
*Hedge:* "Governance models are evolving and organisations should remain
attentive to changes in best practice."
The difference: the strong version names the loser. The hedge names
nothing.

**Hands-On**

*Strong (in voice):* "Swap one production agentic-coding loop to Opus 4.8
this week and measure the unflagged-flaw rate against your incident
baseline. If it holds, the migration math justifies itself."
*Hedge (off voice):* "Teams may want to evaluate Opus 4.8 against their
existing tooling to determine whether adoption is appropriate."
The difference: the strong version names the artefact, the trigger, and
the decision criterion. The hedge names none of them.

*Strong:* "Run v0.22.0 against your own latency baseline this week; if you
confirm even half the 28.9% claim, ship the upgrade."
*Hedge:* "Verify before you adopt this new release."
The difference: the strong version specifies *what to run*, *against
what*, *with what threshold*. The hedge is a slogan.

*Strong:* "Pin the canary to gpt-5.2-mini for the agent-eval suite and
re-run your regression set tonight — the routing change broke our
multi-turn fixture and yours probably has the same shape."
*Hedge:* "It's worth testing the new model carefully before deploying it
to production."
The difference: the strong version names the artefact and the failure
mode. The hedge names neither.

**Currents**

*Strong (in voice):* "Three jurisdictions are circling the same
agentic-payments question and none has acted. If one moves first the
others copy the shape; if none moves the question stays open for another
quarter and the early-mover advantage compounds."
*Hedge (off voice):* "Regulators around the world are increasingly
focused on questions related to autonomous AI agents in financial
services."
The difference: the strong version has two branches with stakes on both.
The hedge has neither branches nor stakes.

*Strong:* "If ITS-Mina replicates, attention-free forecasting is a real
architecture line and your shortlist needs revisiting. If it doesn't, the
benchmark suite itself becomes the story — and that matters more than any
single model claim."
*Hedge:* "ITS-Mina is an interesting new approach worth keeping an eye on
as more results emerge."
The difference: the strong version pre-commits to what each outcome means.
The hedge defers to "more results."

*Strong:* "Open-weights agent frameworks are converging on the same tool
interface. If that holds another quarter, the moat moves to evals and
data; if it fragments, the moat stays in the framework."
*Hedge:* "The open-weights agent space continues to develop in
interesting directions worth monitoring."
The difference: the strong version names two futures and where the moat
lives in each. The hedge names neither.

---

## Sections: current → proposed (Alternative C)

Today's four sections silently mix three axes — *audience* (leaders vs
practitioners), *signal maturity* (verified vs early), and *position*
(Pulse is one story, the day's direction). The proposed taxonomy gives each
section ONE explicit primary axis. See
`_scratch/b_taxonomy_proposal_2026-05-30.md` for the diagnosis and
stress-test against the May 23–29 misses.

| Section (today) | Section (proposed) | Primary axis | Filter |
|---|---|---|---|
| The Pulse | **The Pulse** | Position — 1 story, the day's direction | Significance + direction visible in 2-3 sentences |
| The Big Picture | **The Big Picture** | Audience — senior leaders | "Would I raise this in a senior meeting?" Maturity carried in direction-note, not in the routing |
| Hands-On | **Hands-On** | Audience — practitioners | "Would I clone, install, or eval this?" Maturity carried in direction-note |
| On the Radar | **Currents** | Maturity — early, audience-agnostic | "The field is moving here, nobody is acting yet." Aggregate direction in `intro_lead` is mandatory |

**What changes:** *On the Radar* renames to *Currents* and its semantics
shift. Today's name implies "you might act on this soon" — a maturity
floor with action-readiness implied. Currents drops the action implication
and keeps only the maturity-tail meaning, which is what the section is
actually doing in practice.

**Schema impact (for architect, stream C):** one `IssueSection.SectionName`
literal value changes (`on_the_radar` → `currents`), mirrored on
`RankedStory.tier`. Pydantic validation alias `on_the_radar` keeps archived
issues parseable.

**Why this and not pure-audience or pure-maturity:** pure-audience
(Leaders / Practitioners / Watching) re-creates the papers-pool-overflow
problem in "Watching." Pure-maturity (Verified / Promising / Watching)
collapses the masthead promise of "strategic context for leaders." The
hybrid keeps both load-bearing affordances and fixes the audience-vs-
maturity conflation that was creating the 184-miss week.

---

## Voice rules per section

These are voice-prose. LLM Engineer turns them into summarise.py prompt
variants; Eval Engineer adds an "intra-section voice distinguishability"
check.

### The Pulse
The day's direction in one imperative sentence. Open on the verb where
possible. Direction-note is mandatory and lives in the body, not the
headline. No section-trope opening ("Researchers found..."; "A new paper
shows..."). The Pulse is a single editorial position, not a paper summary.

*In voice:* "Run autonomous coding agents with full system access, safely
isolated." (issue #3)
*Off voice:* "A new sandbox framework for AI agents has been released."

**Closing shape — Plain take.** A short editorial judgement (1-2 sentences).
The publication's position on this story today. Not a prescription, not a
question, not a hedge — just a sharp declarative take.

*In voice:* "General safety filters built for the open web are reaching
the limits of their fit for regulated work. Domain-grounded filtering is
where the credible safety story starts now."
*In voice:* "Anthropic just told you the truth about a release. That's the
news. Whether to swap is a procurement question; whether the lab is
honest is the strategic one."

### The Big Picture
Named actors + first-order consequence. Lead with *who* (organisation,
regulator, market) and *what changes for them.* No paper-titlecase
headlines. Direction-note frames the decision a leader would make this
week. Avoid "Researchers find X" — that's the practitioner frame.
Prefer "X is moving; here's what shifts."

*In voice:* "A runtime compliance score could replace the once-a-year AI
audit." (issue #4)
*Off voice:* "AI agents now act in ways pre-deployment governance cannot
fully anticipate." (Reads like a paper abstract; no actor, no consequence.)

**Closing shape — Strategic question.** End on the sharp unresolved
question the news raises. Forces the reader to take a position. Not a
rhetorical question with an obvious answer; not a prescription dressed as
a question.

*In voice:* "When the agent ships 80% of commits unsupervised, what does
the human reviewer still own — and is that role staffed in your org?"
*In voice:* "When the safety filter and the regulator's rulebook disagree,
which one governs your customer-facing deployment?"

### Hands-On
Tool, repo, version, or config in the headline noun phrase. The reader
should be able to tell what they'd `git clone` or `pip install` from the
headline alone. Direction-note prescribes the action ("clone before X";
"run against your eval"; "wait for the repo"). No leader-pull-quotes
pasted on the end.

*In voice:* "Shrink large-model training syncs from gigabytes to
megabytes." (issue #6)
*Off voice:* "A new technique improves training communication
efficiency." (No artefact in the noun phrase; could be a paper, a tool,
a blog post — reader doesn't know what to do.)

**Closing shape — Imperative action (sharpened).** A specific prescription
with a trigger or condition, on a specific artefact. Slack-able. Generic
verbs without specific targets fail this shape ("just test it", "bench
before you trust" — both fail).

*In voice:* "Swap one production agentic-coding loop to Opus 4.8 this week
and measure the unflagged-flaw rate against your incident baseline."
*In voice:* "Run v0.22.0 against your own latency baseline this week; if
you confirm even half the 28.9% claim, ship the upgrade — the cost-per-
token math justifies the migration."

### Currents
Conditional or hedged opening; signal of motion, not arrival. "If this
holds..."; "Early signal that..."; "Worth watching: X moving toward Y."
Direction-note explicitly says "no action yet" and why. `intro_lead` for
the section names the *aggregate direction* — Currents is the only
section where a thematic intro is mandatory rather than nice-to-have,
because without it the section is just an enumeration of early signals
and that violates "point, don't list."

*In voice (target):* "Early signal: regulators in three jurisdictions
are circling the same agentic-payments question. None has acted yet."
*Off voice:* "EU AI Act Newsletter #102: Pressure Builds over Anthropic's
Mythos." (Headline reads like a feed item, not a vector — and notice
this story has been homeless for 3 days under the current taxonomy.)

**Closing shape — Calibrated stake.** A two-sided watch-condition with
stakes on both branches. Structure: "If X holds, Y; if not, Z." Both
branches must carry real stakes — false-binaries and one-sided "if X, Y"
without an inverse fail this shape.

*In voice:* "If ITS-Mina replicates, attention-free forecasting is a real
architecture line and your shortlist needs revisiting. If it doesn't, the
benchmark suite itself becomes the story — and that matters more than
any single model claim."
*In voice:* "If the FinGuard claim holds under audit, every customer-
facing deployment without a regulation-grounded check is a compliance gap
waiting to be found. If it doesn't, the gap is the audit."

---

## The Pulse — the bar

Lifted verbatim from PLAN §4: *"the single most important thing today,
2-3 sentences. Warmth + signal."* Editor's working heuristic, applied to
Pulse picks the editor would push back on:

1. **Significance over volume.** The biggest news isn't always the most
   pulsed-about news.
2. **Direction visible in 2-3 sentences.** If the direction-note can't fit
   in that space, it's not Pulse material; it's a section item.
3. **One Pulse, not three.** If you find yourself wanting two, the second
   is "Where it's heading" material — or it belongs in Big Picture /
   Hands-On as the section's first story.

---

## On editorial-focus and finance-lens

The editor invokes `editorial-focus` *first* on every labelling pass — it
decides what's covered before voice or finance-lens considerations come in.
Then `finance-lens` — "is the FS angle earning its place today, or is it
forced?"

Heavy on Agentic AI and Generative AI. Traditional ML lands only when
load-bearing for the field today. We are ruthless on strong signal:
*today / tomorrow / practical.*

---

## Drift watch

Specific examples of voice drift this editor will flag to Arman as they
appear:

- **Intros collapsing into one register.** May 23–29 intros all read as
  "trust-but-verify" — even when the stories don't share that frame.
  That's voice collapse, not voice cohesion.
- **Hands-On stories carrying leader pull-quotes.** A Hands-On story that
  ends with "Raise this at your next model-risk review" is wearing the
  wrong jacket. Practitioners read it as a downgrade; leaders won't find
  it because it's not in Big Picture.
- **Pulse picks where direction is invisible in 3 sentences.** When the
  direction-note has to do all the lifting because the headline didn't,
  the Pulse is mis-picked. Propose an alternative inline.

---

## Pre-release review checklist

The editor runs a structured pass over every staged issue before Arman
ratifies. `src/review.py` paraphrases this list into its prompt; this
section is the canonical reference. Verdict is `green` (ratify) / `amber`
(ratify with notes) / `red` (hold).

- **Shape integrity.** Counts within caps (1 Pulse / 4 Big Picture / 5
  Hands-On / 8 Currents). If amber or red, is the cause a genuinely thin
  tier pool or a routing failure upstream?
- **Pulse pick.** Carries the day's editorial position. Closing shape is
  a plain take — no question, no prescription. Sourcing credible
  (multi-source, canonical_id, or trust-3+). Freshness vs recurrence
  earned.
- **Big Picture.** Named actors + first-order consequence framing.
  Closing shape on each story is a strategic question. Section intro
  frames a leader-orienting pattern across the four stories.
- **Hands-On.** Tool / repo / version in the headline noun phrase.
  Closing shape is an imperative action sharpened to a specific artefact
  + trigger — generic verbs without targets fail. Section intro carries a
  practitioner posture.
- **Currents.** Conditional or hedged opening. Closing shape is a
  calibrated stake ("If X, Y; if not, Z") with real stakes on both
  branches. Section `intro_lead` is mandatory and names aggregate
  motion direction.
- **Drift watch.** Recurring themes covered the same way without
  progression. Source repetition (same source 3+ days running on the
  same topic). Missing callbacks. Voice drift across section intros.
- **Finance angle.** Lands where it appears, or forced? Any story
  surfaced only because of a weak FS angle?
- **Section misroutes.** Any story reading more like a different
  section's voice than its assigned one.

---

## Anti-patterns the editor will flag

Specific phrases and constructions that, when repeated across issues or
used as a default register, signal voice absence rather than voice
presence. The summarise prompt reads this catalogue at run time; the LLM
is instructed to avoid these constructions today. New entries are added
by the editor when reviews catch them.

- "X outruns Y" / "X is outpacing Y" / "X precedes Y" — same shape across days (Big Picture intros: "Costs precede clarity", "Capability outruns control", "Speed is outrunning safety")
- "Verify before you [verb]" — Hands-On opener tic ("Verify before you deploy", "Verify before you adopt")
- "Trust, but verify." — Big Picture intro cliché; verbatim repeat across issues
- "treat this as a starting position, not a settled audit" — closing-shape repeat across days
- Two sections of one issue sharing the same thesis statement (#11: Big Picture "Speed is outrunning safety" + Currents "Capability races ahead of control")
- "Capability races ahead of control" / "Speed is outrunning safety" — register synonyms used in same issue
- Generic verify/audit/test imperatives without specific artefact + trigger (Hands-On closings: "just test it", "bench before you trust")
- Imperative actions dressed as strategic questions (Big Picture closings: "Which review gates survive in your org?" is an imperative in question clothing)
- One-sided "If X holds, Y" Currents closings (no inverse branch — calibrated stake needs both sides)
- "Worth subscribing to" / "Watch for X" — defensive Currents endings
- "may", "could", "potentially", "appears to" stacked in a single sentence — hedge accumulation
- "It remains to be seen whether..." — defers the take the publication exists to make
- "Time will tell" / "Only time will tell" — same deferral, shorter form
- "An interesting development" / "A notable development" — adjective doing the work of an editorial position
- "Some observers believe..." / "Critics argue..." without naming the position the publication holds
- "On the one hand... on the other hand..." structures that don't resolve to a take
- Closings that re-summarise the body instead of advancing the position
- "This space is evolving rapidly" — true of everything, says nothing
- Inline venue citations like "(arXiv, updated 2 June 2026)" / "(per arXiv 2603.12345)" / "(Substack post, 29 May)" in the body — attribution is the source URL's job, not the prose's. The reader can see where the link goes. Exceptions: when the venue IS the news (the FDA approved X), when the venue's recency is the news (a paper updated last week reverses an earlier claim), or when the channel is itself editorial signal (posted to an internal blog rather than published). Default is to drop the citation.

**Adding new entries**: when an editor review flags a construction that's
recurring, paraphrase it as a one-line entry here. Specific phrases
("Trust, but verify") are stronger anti-patterns than abstract shapes
("X outruns Y") — both belong in the list, but the specifics are what
stop verbatim repeats.
