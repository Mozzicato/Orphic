# ORPHIC — MASTER BUILD ROADMAP

### Project vision

> **Orphic is a local-first filesystem intelligence engine in Python that indexes storage at scale, finds duplicates provably, and proposes safe cleanup — with natural language as a suggestive interface over a deterministic, auditable engine.**

The long-term experience is:

> **"Orphic, why is my storage full?"**
> **"Show me what I probably don't need anymore — but don't touch my projects or school files."**

The key architectural principle:

> **Natural language can suggest intent. Deterministic systems decide what is actually safe to do.** The model is an interface to the engine, never the engine.

---

## 0. SCOPE DECISION — READ THIS FIRST

Orphic v1 is a **CLI-first, engine-centric tool** — not a desktop app, not an AI product.

**Why this is deliberate:**
- The engine (scanning, indexing, hashing, safety) is the interview asset. UI and chat layers are presentations of work you've already proven.
- Python desktop frameworks (Tauri/Electron bindings, Qt) are scope swamps that teach interviewers nothing about you.
- Every later phase (dashboard, conversational interface) is a **strictly additive** layer over a tested engine — you can stop at any release and the project stands.

**What we explicitly do NOT build (v1):** desktop UI (until 0.6), LLM integration (until 0.7 — and even then as a query layer, never an actor), near-duplicate detection (0.7 stretch), semantic search (0.8+). Premature architecture is still architecture debt.

**Why Python (write this ADR before you start):** fastest honest iteration for I/O-bound systems work, pytest/tmp_path are a filesystem test lab, and the performance story ("where does the time go, proven with cProfile") matters more than raw language speed — you will *show* the profiling, and knowing Python's I/O limits while explaining them is a stronger signal than pretending they don't exist.

---

## 1. THE ORPHIC DEVELOPMENT MODEL

Same loop as EventMesh and ForgeDB:

```text
Problem
   ↓
Smallest implementation
   ↓
Experiment
   ↓
Measure (benchmark, always)
   ↓
Test (pathological cases are the curriculum)
   ↓
Document (research note / experiment log / ADR)
   ↓
Ship (tagged release)
   ↓
Next problem
```

Every phase has: **Objective → Deliverables → Experiments → Exit criteria.**

Four standing rules (these protect everything):
1. **No marketing numbers.** Never "scans 500GB in seconds." Always "on environment X, Y files in Z seconds" — with the benchmark script committed. **We earn every number.**
2. **Every commit real and dated.** The GitHub history is authenticity evidence.
3. **Every phase ends with a tagged release**, never a half-finished `main`.
4. **The pathological-filesystem test lab runs in CI from Phase 1.** Not on your laptop. Forever.

---

## 2. PHASE 0 — THE FOUNDATION (Week 1)

### Objective
Engineering environment where every experiment is reproducible. This phase prevents six months of chaos.

### Deliverables
```text
orphic/
├── src/orphic/
│   ├── scanner/
│   ├── index/
│   ├── dedup/
│   ├── cleanup/
│   └── cli/
├── tests/                  # includes fixtures/ — the test lab
├── experiments/
├── benchmarks/             # scripts + results/ committed
├── scripts/                # gen_files.py, plot.py
├── docs/{decisions,experiments,research,architecture}/
├── .github/workflows/      # CI: pytest + coverage, ruff, mypy (gradual)
├── pyproject.toml
├── README.md
└── .gitignore
```

Toolchain decisions (ADR them): `uv` or venv + pip, `pytest` + `pytest-cov` + `pytest-xdist`, `ruff`, `typer` + `rich` for CLI, SQLite via stdlib `sqlite3` first, GitHub Actions matrix (3 Python versions).

### Research before/while doing it
- Python project structure, `pyproject.toml`
- `os.scandir` — **why it matters:** `DirEntry` caches stat info from the directory read, avoiding a syscall per file. This is your first deliberate performance primitive.
- pytest: `tmp_path` fixture — your filesystem test lab lives inside it
- Git branches, semantic commits, PR discipline (solo project, professional habits)

### Exit criteria
Fresh clone → `pip install -e . && pytest` green in CI. CLI skeleton runs.

---

## 3. PHASE 1 — THE EYES: Filesystem Discovery Engine (Weeks 2–3)

### Objective
Orphic **sees** a filesystem correctly. Not analyzes. Not cleans. Just sees — through every pathological case.

### Deliverables
- Recursive traversal built on `os.scandir()` (not `os.walk` — write the ADR on why).
- `FileRecord`: `path, name, extension, size, mtime, ctime, is_directory, is_symlink, scan_id`.
- **Deliberate symlink policy (ADR):** record symlinks, never follow them by default — cycles are a footgun and "record, don't follow" is defensible.
- Error taxonomy: `PermissionError`, `FileNotFoundError` (file vanished mid-scan), `OSError` catch-alls — each recorded as a structured skip with reason, **scan never aborts**.
- Progress reporting (`rich` progress) that survives errors.
- **The test lab** (`tests/fixtures/` builder): builds synthetic trees with — deep nesting, 10k+ files, permission-denied dirs, broken symlinks, symlink cycles, empty files, unicode names, 255-char names, files created/deleted *during* scan (background thread fixture).

### Experiments
Scale ladder: `10 → 100 → 1k → 10k → 100k files`. Record wall-clock per run, committed as CSV.
scandir vs os.walk head-to-head → your first published experiment log.

### Exit criteria
Given any directory, Orphic produces a trustworthy inventory: what it found, what it skipped, and why. **No hashing yet. No duplicates yet. No AI.**

### Interview story
> "The scanner is built on os.scandir because DirEntry avoids a stat syscall per file; it records symlinks without following them and survives permission errors, vanished files, and symlink cycles — all reproduced in a fixture-based test lab."

---

## 4. PHASE 2 — THE MEMORY: Local Index (Weeks 4–5)

### Objective
Stop treating every scan as temporary. Give Orphic persistent memory.

### Deliverables
SQLite schema:
```text
scans(id, root_path, started_at, finished_at, status)
files(id, scan_id, path, name, extension, size, mtime, ctime, is_symlink, first_seen, last_seen)
errors(id, scan_id, path, error_type, message, ts)
```
- **Incremental indexing:** skip re-descending directories whose mtime is unchanged (the ">80% repeat-scan reduction" claim starts here — measure it, don't assume it).
- Query layer: files by size, by extension, newest/oldest, largest N.
- **Architectural distinction to internalize and write down:** SQLite is the *index*, not a cache of everything in RAM. This decision matters at 500k+ files.

### Experiments
- Insert batching: 1-row commits vs 1,000-row transactions → the WAL mode + transaction story.
- Repeated scans of unchanged trees → measured reduction percentage (your real number, whatever it is).
- Interrupted scan (SIGKILL) → DB consistency on reopen (SQLite transactions do this for free — *understand why*, that's the interview answer).

### Exit criteria
Scan → persist → close → reopen → query, without rescanning. At this point Orphic is already a legitimate filesystem indexing engine.

---

## 5. PHASE 3 — THE DETECTOR: Exact Duplicate Engine (Weeks 6–7)

### Objective
Find files that are definitely identical **without unnecessarily reading everything.** Your first interview-grade algorithmic component.

### Pipeline (design on paper first)
```text
ALL FILES → GROUP BY SIZE → CANDIDATES → QUICK CONTENT CHECK → FULL SHA-256 → DUPLICATE GROUPS
```

### Deliverables
- Size grouping (free filter — reading `size` from the index, zero I/O).
- Quick content check: hash first 4KB (and last 4KB — experiment with it).
- Full SHA-256 only for true candidates.
- `duplicates(group_id, file_id, hash, algorithm)` + report rendering:
```text
Duplicate Group #17
Original candidate: C:\Photos\trip.jpg
Duplicates: C:\Backup\trip.jpg, C:\Downloads\trip.jpg
Recoverable: 8.7 MB
```

### Experiments (do not assume the pipeline is optimal — test it)
- Strategy ladder: `size→SHA-256` vs `size→first-4KB→SHA-256` vs `size→first+last→SHA-256`.
- Metrics per strategy: files opened, bytes read, wall-clock, CPU, memory, false-candidate rate.
- **Generate the adversarial dataset yourself:** 10k unique files + 2k duplicates with realistic size collisions.

### Exit criteria
Duplicate detection is correct, tested, measurable, and explainable. The winning strategy has a published experiment log defending it.

### Interview story
> "Size grouping eliminates most files for free; a partial-content hash eliminates most of the rest; full SHA-256 runs on a tiny candidate set. I tested three strategies on an adversarial dataset and chose based on bytes-read, not intuition."

---

## 6. PHASE 4 — THE ENGINEERING: Performance & Concurrency (Week 8)

### Objective
Make Orphic faster **through measurement, not vibes.** Arguably the most valuable phase for a Google-tier application.

### Deliverables
- **Baseline first:** `100,000 files → X seconds`. Committed. Everything is relative to this.
- Benchmark harness (`benchmarks/`, seedable, committed results).
- Concurrency experiments: `ProcessPoolExecutor` for hashing (CPU-bound) vs threads for scanning (I/O-bound) — **learn why the GIL makes this distinction exist**, it's a classic interview question you'll now answer from measurement.
- Profiling: `cProfile` + `py-spy` flame graph on the hot path; fix what actually shows up.
- Pipeline architecture sketch in docs (scanner → queues → hash workers → DB), implemented only if measurements justify it.

### Experiments
Worker sweep: 1/2/4/8/16 workers × scan-only vs hash-heavy workloads. Find and explain the inflection.

### Exit criteria
You can state precisely: *what was slow, why, what changed, and the measured delta.* That sentence pattern is the performance-engineering interview.

---

## 7. PHASE 5 — THE SHIELD: Safety & Cleanup Architecture (Weeks 9–10)

### Objective
Turn Orphic from an analyzer into a safe decision-support system. **Only now** does Orphic touch user decisions.

### Deliverables — the pipeline (respect it in the code structure)
```text
Inventory → Analysis → Candidate generation → Safety rules → Cleanup Plan → USER REVIEW → Execution
```
- **Protected paths:** system dirs, configured user paths (projects/, school/) — a policy layer, not hardcoded ifs.
- **Dry-run by default:** `14 files, 6.2 GB — would move / would keep / protected`, rendered before anything happens.
- **Recovery model:** moves go to a recovery area + operation log; nothing is permanently deleted by any default path.
- **The honesty ADR:** SQLite transactions keep bookkeeping consistent, but cannot make filesystem moves atomic — the DB and the FS can diverge; design the operation log to reconcile on startup. Knowing this distinction is senior.

### Exit criteria
Cleanup plan generation + conservative execution with confirmation, logging, recovery, failure reporting, and tests — including a crash-mid-move test.

At this point **Orphic is a real product, not a research engine.**

---

## 8. PHASE 6 — THE FACE: Desktop Dashboard (Weeks 11–12)

### Objective
Make the engine usable by someone who'll never read the code. First interface is a dashboard, not a chatbot.

### Deliverables (web dashboard via local server — simpler packaging than native, same demo value)
- Storage overview: used/free, largest categories, top directories.
- Treemap visualization (write the ADR on the viz library — choose by output size and dependency weight).
- Duplicate browser, cleanup review flow, recovery history, error visibility.
- `docker compose`-free single-command local launch.

### Exit criteria
A stranger can answer: *"What is using my storage?"* and *"What can I safely investigate?"* without help.

---

## 9. PHASE 7 — THE HARDER PROBLEM: Near-Duplicate Detection (Weeks 13–14, stretch)

### Objective
Expand from "are these bytes identical?" to "are these files meaningfully similar?" First target: images.

### Deliverables
- Perceptual hashing research note: dHash vs pHash (DCT) — implement dHash first (one-liner-ish, Hamming distance).
- Similarity dataset you build yourself: original / recompressed / resized / slightly-cropped pairs.
- Threshold evaluation: precision/recall curves at multiple Hamming thresholds. **Choose the threshold from the curve, defend it from the data.**

### Exit criteria
You can say "we selected this method and threshold because our experiments showed..." — never "I used pHash because it's on a roadmap."

---

## 10. PHASE 8 — THE CONVERSATIONAL LAYER (Week 15+, stretch — and only after 0.6 exists)

### Objective
**"Why is my storage full?"** — with the non-negotiable architecture:

```text
USER → Chat interface → Intent/constraint extraction → Orphic Query Layer
      (metadata, duplicates, similarity, policies) → Evidence
      → Recommendation → Safety validator → HUMAN APPROVAL → execution
```

**The model never operates the filesystem.** It interprets "find large old files in Downloads"; the engine determines what exists, what's protected, what's permitted. The AI is an interface to the system, not the system itself.

### Exit criteria
Natural-language questions receive answers grounded in local indexed data. The "wait, you built WHAT?" version — **but it is a bonus, not a graduation requirement.**

---

## 11. PHASE 9 — PRODUCTIZATION (final polish phase)

### Deliverables
Packaging, onboarding, architecture docs, benchmark suite, security notes, 85%+ test coverage, CI hardening, screenshots, demo GIF/video, technical blog post, README arc:

```text
Problem → Architecture → Engineering decisions → Experiments → Benchmarks
        → Safety model → Demo → Tests → Limitations → Future work
```

**Limitations are mandatory:** single-user, local-only, exact-duplicate scope (or near-dup if 0.7 landed), no cloud sync. Maturity = knowing what you didn't build.

---

## 12. RELEASE MILESTONES & STOPPING POINTS

| Release | Contains | Portfolio value |
|---|---|---|
| **Orphic 0.1** | Phase 1: trustworthy scanner + test lab | Solid foundations project |
| **Orphic 0.2** | + persistent SQLite index, incremental scans | Has memory; real persistence story |
| **Orphic 0.3** | + duplicate engine + strategy experiments | **First algorithmic core. Already strong.** |
| **Orphic 0.4** | + measured concurrency/optimization | Serious engineering material |
| **Orphic 0.5** | + safety model, dry-run, recovery | A real, safe product |
| **Orphic 0.6** | + dashboard | Polished and demonstrable |
| **Orphic 0.7** | + conversational interface | Exceptional portfolio piece |
| **Orphic 0.8+** | near-duplicates, semantic search | Bonus depth, not required |

**Stopping-point principle:** recruiting season hits at 0.3? You have traversal + persistence + duplicate detection + tests + benchmarks — apply. At 0.5? A real product. **You never say "I can't apply because Orphic isn't finished." Every release is a legitimate version.**

**Interview-mapping cheat sheet (rehearse as you build):**
| Phase | Interview topic it arms you for |
|---|---|
| 1 | I/O-bound vs CPU-bound thinking, error taxonomy, resource discipline |
| 2 | Persistence design, transactions, indexing |
| 3 | Algorithmic thinking: filtering pipelines, complexity of naive vs staged approaches |
| 4 | Performance methodology: baseline → profile → fix → verify; GIL and process pools |
| 5 | Safety engineering, auditability, dry-run culture |
| 6 | Product thinking, UX of technical tools |
| 7 | Applied ML literacy: precision/recall, threshold selection from data |
| 8 | LLM system design: tool-use boundaries, deterministic cores under probabilistic interfaces |
| 0–8 | "Tell me about a hard bug" — the test lab will hand you thirty |

---

## 13. RESEARCH / EXPERIMENT / DECISION SYSTEMS

```text
docs/research/     filesystem-traversal.md, symlink-behavior.md, hashing-strategies.md,
                   filesystem-concurrency.md, sqlite-concurrency.md, perceptual-hashing.md
                   — each answers: what is it, why needed, alternatives, tradeoffs, what we chose, why
docs/experiments/  001-scandir-vs-oswalk.md, 002-hash-strategy.md, 003-worker-count.md,
                   004-sqlite-batch-size.md, 005-phash-threshold.md
                   — each: Question, Hypothesis, Setup, Variables, Results, Interpretation, Decision
docs/decisions/    ADRs only for architecture-level choices
benchmarks/        every claim reproducible from a committed script
```

Research asks "what does this do?" Experiments ask "what happens in Orphic if I do this?" Keep them separate.

---

## 14. READING LIST — PHASED, NOT ALL AT ONCE

- **Phase 0–1:** Python docs on `os.scandir`; pytest docs (`tmp_path` fixture); Python packaging guide.
- **Phase 2–3:** SQLite WAL documentation; `hashlib` docs; a short writeup on CRC32 vs SHA-256 tradeoffs (context: why detection uses SHA-256 despite cost).
- **Phase 4:** `cProfile`/`py-spy` docs; Real Python's concurrency guides (threads vs processes vs GIL).
- **Phase 5+:** anything you need *when* you need it. Do not read perceptual-hashing literature in week 1.

---

## 15. YOUR FIRST CHECKPOINT

Not "build Orphic." This:

```text
Repository + pyproject + pytest + ruff + CI        (Phase 0)
      ↓
Test-lab fixture builder (synthetic pathological trees)  (Phase 1 prep)
      ↓
os.scandir recursive scanner → FileRecord          (Phase 1)
      ↓
Permission-denied / broken symlink / vanished file → structured skips   (Phase 1)
      ↓
Progress reporting + scale ladder benchmark        (Phase 1)
      ↓
scandir vs os.walk experiment log committed        (Phase 1)
      ↓
TAG: v0.1.0 → stop, inspect measurements, only then design 0.2
```

**Time budget:** ~10 weeks to 0.5 at 10–15 hrs/week alongside school — leading the Orphic (6–10w) → EventMesh (18w) → ForgeDB (28w) sequence.

The end goal is not "a file manager with AI."
It's:

> **"A system that understands your filesystem deeply enough that you can actually talk to it — and that never, under any circumstances, touches a file it wasn't explicitly, safely told to."**

That's Orphic.
