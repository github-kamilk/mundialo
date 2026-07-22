# Case study: running an AI newsroom through a World Cup — what actually breaks

> Status: **complete** — reflects the real tournament numbers end to end; pending the
> author's final read-through before going into the public repo.

## 1. The premise

One person, one month, 104 matches. After every game of the 2026 World Cup, produce an
Instagram carousel answering "how did the press of **both** countries react?" — real
quotes from real newspapers, in 17 languages, translated into Polish, attributed, and
consistent with the actual result. Published as [@mundaily_](https://www.instagram.com/mundaily_/).

The bar that shaped every design decision: **a fabricated quote or a wrong score kills
the product.** A sports-reaction account that misquotes Marca or gets the final score
wrong is dead on arrival. So the system is built guards-first: every LLM output is
either validated by deterministic code, reviewed by a stronger model, or degraded to
something safe.

The constraint that shaped everything else: **budget.** A solo side project cannot run
GPT-4-class models on every stage of every re-roll. Model routing (cheap vs strong per
task shape) and a disk-level search cache are cost features as much as quality features.
The result: the entire tournament — 104 matches, 254 pipeline runs — cost **about $10
in model API spend** (6.2M input tokens), roughly **$0.10 per published post**.

Why build this at all? I wanted to go from studying agentic patterns to **operating a
real agentic system** — one with concrete contracts, guards, and consequences, where a
bad output is publicly visible minutes later. And I wanted the domain to be something I
genuinely care about, so a month of daily operations would stay fun: football it was.

## 2. Architecture in one screen

See the [README](README.md#how-it-works) for the pipeline diagram. The short version:

- **Two paths per match**: a facts path (tiered sources, corroboration, "Tier C is never
  a fact") and a media path per country (whitelisted outlet sections + domain-restricted
  search → LLM curator → temporal gate → quote scout → translator).
- **Cross-cutting**: EvidenceStore (claims must trace to sources), provider-agnostic
  ModelGateway with structured output + retry, editorial voice as semantic memory,
  episodic per-outlet health memory, full audit trail per run.
- **Status machine**: `ready` / `needs_human_review` / `insufficient_evidence` — review
  is a first-class outcome, and the human approves anything uncertain before publishing.

The rest of this document is about what happened when that architecture met reality.

## 3. What actually broke (and what fixed it)

Five recurring failure classes, each with a representative war story. All of them
shipped with regression tests.

### 3.1 Retrieval is a language problem

The naive version — query a search API with English team names — silently returns an
empty panel for most of the world.

- **Exonyms.** Tunisia–Netherlands: querying "Netherlands" in French-language press
  finds nothing, because French papers write "Pays-Bas". Every country config carries
  per-language exonyms for opponents, and query templates use them. Subtlety: the
  space form ("Pays Bas") was needed because the matching blob strips hyphens.
- **Local tournament vocabulary.** "World Cup 2026" doesn't appear in Czech papers —
  "MS 2026" does; Dutch press writes "WK 2026". Each country carries a `world_cup`
  term in its own language, used in query templates.
- **Non-Latin scripts.** Morocco's config had a *French* tournament term for an
  *Arabic-language* press corpus — Latin queries never reached hespress recaps. Arabic
  terms fixed it. Downstream, a second trap: cheap models don't copy Arabic verbatim
  reliably, so extraction for non-Latin scripts is auto-routed to the strong model.
- **Substring traps.** The token "Jordan" matched "Jordanian" in an economics article
  about the dinar exchange rate — an off-topic slide in a football panel. Fix: a
  country's own name as a *substring* needs a football signal in context; full-word
  matches are trusted.
- **Small-country pattern.** Curaçao's press writes about *the result*, not the
  opponent ("Curacao Ecuador uitslag") — opponent-centric queries fail; result-centric
  templates per country fixed it.

**Lesson:** multilingual retrieval quality is a *data* problem (per-country config:
exonyms, vocabulary, query templates), not a prompt problem. What that looks like in
[`data/sources/country_media.json`](data/sources/country_media.json):

```jsonc
// Czechia — tournament vocabulary in the country's own language
"query_templates": ["{team} reakce medii {opponent}", "Cesko {opponent} MS 2026"],
"world_cup": "MS 2026"

// France — exonyms that other countries' queries reach for
"exonyms": { "es": "Francia", "pt": "Franca", "en": "France" }

// Curaçao — result-centric templates (the small-country pattern)
"query_templates": ["{team} {opponent} uitslag", "{team} {opponent} wedstrijd"]
```

### 3.2 Every anti-hallucination guard false-positives — budget for it

- **The digit guard vs "1/8 finału".** Slide text may not contain numbers absent from
  the source article. Polish writes "round of 16" as "1/8 finału"; the English source
  said "round of 16", so the guard saw a fabricated "8", burned 5 retries and salvaged
  the slide down to a bare quote. Fix: a stage-fraction vocabulary the guard understands.
- **The score guard vs football itself.** "Media mention other scores: 0-0 / 4-3" on a
  match that finished 3-1 after penalties — the guard was reading *halftime* scores and
  *shootout* results as contradictions. Fix: sentence-level filtering with halftime and
  shootout markers in seven languages (PL/ES/PT/FR/EN/DE + Nordic variants).
- **Context scores.** An article about your match cites a *different* match of the
  group (Belgium–NZ 5-1) as table context; the guard reads it as a contradiction.

**Lesson:** a guard without domain vocabulary trades hallucinations for false
positives, and false positives are not free — they eat retries, degrade slides, and
train the operator to distrust the system. Guards are worth it anyway: **across all
104 published posts, no fabricated score or quote ever reached publication.** Not
because the pipeline never produced one — over 254 runs it produced plenty of
candidates — but because guards, validators, and the human-review gate caught them
upstream of the feed. The published quality belongs to the whole loop
(pipeline + guards + operator), not to any single model call.

### 3.3 Time is the hardest dimension

The worst published-content bug of the tournament: a **pre-match press conference**
story appearing in a **post-match** panel. Root cause chain: some outlets use undated,
ID-only URLs; the search API returned `published_at=None`; every deterministic filter
(date, URL slug, ID heuristics) was structurally blind; and the story had a dramatic
lead that fooled the LLM curator *and* the quote scout — prompts alone did not hold.

The fix that held: a dedicated **LLM temporal gate** ("is this a post-match report of
*this* match?") placed before extraction. Two findings:

- The cheap model failed this binary judgment consistently (0/3 on the regression set);
  the strong model got 3/3. This crystallized the project's model-routing rule:
  **judgment without a validator → strong model; extraction behind a validator → cheap
  model.**
- The gate then produced its own false rejects — so it got a bypass (URL slug mentions
  the final score → trivially post-match) and a resurrection path (if a gate rejection
  empties a country's panel and the article corroborates the final score, bring it back).

**Lesson:** for LLM judgment errors, the remedy is not a better prompt but a better
*judge* — and every judge you add needs its own escape hatches.

### 3.4 The web fights back

A tour of what "just fetch the article" means in production:

- **Bot blocks at article level**: Germany's kicker.de serves the section page but 403s
  the match report itself; Egypt's al-Ahram 403s but the search index's cached
  `raw_content` still saves the quote. Switzerland's only German-language outlet
  403-ing meant *no* fallback — search is domain-restricted by design, so a blocked
  sole outlet needs a reachable replacement in the registry, not a retry.
- **Paywalls that look like success**: record.pt returns HTTP 200 and 955 characters of
  navigation chrome. Length and content heuristics, not status codes, decide fetch
  success.
- **JS walls**: pages that render entirely client-side yield zero article links from
  static fetch (TyC Sports, VG's `/spesial/`, both Curaçao outlets) — those countries
  become 100% search-dependent, which the health layer should know.
- **URL drift mid-tournament**: NRK moved World Cup recaps to `/fotballvm2026/`;
  FIFA's own pages served stale snippets under `?gender=2` URL variants (same match,
  outdated score — a poisoned *fact* source).
This class of failure is why the project grew an **episodic memory**: every run logs
per-outlet telemetry (fetch success, article yield, block status) into
`runs/.outlet_health.json`; subsequent runs consume it as advisories and re-order
sections. The source network is *learned*, not just configured. A slice of the state
after the final (`python -m app.health`; output in Polish, the operator surface):

```text
Magazyn zdrowia zrodel: runs\.outlet_health.json
Aktualizacja: 2026-07-19T22:46:42+00:00

[Hiszpania] MarcaES: 6 zdarzen w oknie
[Argentyna] OleAR: 13 zdarzen w oknie
[Szwajcaria] sekcja https://www.blick.ch/sport/fussball/: 1 zdarzen w oknie; seria: botblock x1 (od 2026-07-13)
[Francja] sekcja https://www.leparisien.fr/sports/football/: 2 zdarzen w oknie; seria: botblock x2 (od 2026-07-14)
[Argentyna] sekcja https://www.tycsports.com/seleccion-argentina.html: 1 zdarzen w oknie; seria: no_links x1 (od 2026-07-13)
```

The bot-block streaks (Blick, Le Parisien) and the JS-wall (`no_links` on TyC) are
exactly the knowledge that used to live only in the operator's head.

### 3.5 Variance, cost, and the operator loop

The media path has four stochastic LLM stages (curator → gate → scout → translator).
A single run is a lottery ticket: each re-roll fails somewhere else. Instead of
pretending determinism, the system embraces an **operator loop**:

- re-roll and pick the best run (the disk cache makes this cheap; the archive averages
  ≈ 2.4 runs per match — re-rolls and fixes were the operating model, not an anomaly);
- `--score X-Y` to inject a manually verified result when no machine-confirmable source
  exists (always forces human review);
- `--allow-review` renders review-gated runs for preview;
- **salvage over failure**: a summary that keeps violating its contract after retries
  is degraded to a bare attributed quote — loudly, in the run notes — instead of
  sinking a two-country post over one stubborn article;
- `scripts/roadmap.py` as the tournament dashboard (what's rendered, what's stuck);
- a Claude Code skill (`.claude/skills/debug-match-content/`) encoding the triage tree
  from symptom ("post didn't generate") to root cause — the runbook as an executable
  artifact, including the crucial split between *timing* problems (post-match content
  not indexed yet → wait and re-run) and *code/selection* problems (fix + test).

**Lesson:** for a solo-operated LLM product, the unit of reliability is not the single
run but the *(system + operator + tooling)* loop. Design for re-rolls, make state
inspectable (`run.json` audit trail), and make degradation loud.

## 4. Evaluation

- Scenario harness with assertions (`expected_status`, `must_have_checks`,
  `must_fail_checks`, forbidden terms) over both content tracks — real code paths, not
  mocks.
- A leaderboard scoring runs 40 (fact-check) + 40 (quality) + 20 (angle).
- 431 offline tests (~5 s, no API keys) — every war story above ends in a named
  regression test class (e.g. `StageFractionVocabularyTests`,
  `OffTopicDemonymFilterTests`, `PostMatchGateTests`, `SalvageDiagnosticsTests`).
- Honest gap, kept deliberately: eval results are not persisted across runs, so "did
  this prompt change help?" was answered by eyeball plus the regression suite. A
  persisted leaderboard with per-axis diffs is specced in `pomysly.md`; it is the
  first thing I would build if the project came back for another tournament.

## 5. Numbers

| Metric | Value |
|---|---|
| Matches covered / posts published | 104 / 104 |
| Pipeline runs archived | 254 (Jun 9 – Jul 19, 2026) — ≈ 2.4 per match |
| Outlet registry | 48 teams · 111 outlets · 17 languages |
| Tests | 431 offline |
| App code | ~10.7k lines of Python |
| Model API spend, whole tournament | ≈ $10 (6.2M input tokens) |
| Search API spend | $0 (Tavily free-tier credits + disk cache) |
| Cost per published post | ≈ $0.10 |
| Final whistle → published post | 2–24 h, dominated by operator availability (US night kickoffs, Polish mornings) and press indexing lag; the pipeline run itself takes minutes |

## 6. What I'd do differently

- **API-native structured output** instead of the hand-rolled parse-validate-retry loop
  (it worked, but it's code the platform now provides).
- **Confidence scores in LLM judgments** — the gate and curator return decisions, not
  calibrated confidence; thresholding would let borderline cases route to review
  automatically.
- **A machine-verifiable score source** from day one — the `--score` operator override
  existed because no reliable free API confirmed results fast; that gap cost the most
  manual work per match day.
- **Episodic memory from the start** — outlet health was added in the knockout stage;
  the group stage was flying blind through bot-blocks the system had already seen.
- **The bottleneck was the human, not the pipeline.** A run takes minutes; a post
  sometimes waited up to 24 hours for me — US night kickoffs, Polish mornings, days
  away from the computer. Next time: a scheduler fires the pipeline at the final
  whistle, and the operator's job shrinks to approve-and-publish from a phone.
- **Fix classes of errors, not instances.** The first weeks were per-country
  whack-a-mole — every country bit differently. Progress came whenever a fix became a
  mechanism: the `exonyms` field instead of hand-tuned queries, salvage instead of
  rescuing one stubborn slide, gate bypasses instead of prompt tweaks — always with a
  regression test attached. "Every fix must become a mechanism plus a test" should be
  a day-one rule, not a lesson from week two.

## 7. Takeaways for building LLM products

1. **Guards beat prompts.** Prompts did not stop the pre-match leak; a dedicated gate
   did. Put judgment in reviewable components, not instructions.
2. **Route models by task shape.** Judgment without a validator needs the strong model;
   extraction behind a validator can be cheap. This one rule carried most of the
   cost/quality trade.
3. **Every validator will false-positive.** Budget vocabulary work per language and per
   domain; measure the retry tax.
4. **Retrieval quality is data work.** Exonyms, local vocabulary, per-country query
   templates — config, not cleverness.
5. **Persist everything.** The `run.json` audit trail (evidence, tool calls, model
   choices, decision notes) made every bug in this list reproducible after the fact.
6. **Degrade loudly, never silently.** Salvage to a quote, resurrect with corroboration,
   force human review — but write it in the notes.
7. **Let the system learn its sources.** The web's hostility is stable enough to be
   worth remembering (episodic outlet health), but only if runs feed it automatically.

---

*Code: [github.com/github-kamilk/mundialo](https://github.com/github-kamilk/mundialo) ·
Architecture: [README](README.md) · Published output:
[@mundaily_](https://www.instagram.com/mundaily_/)*
