# WattLab — Testing Strategy

## Philosophy

Three tiers, each with a clear time budget and a clear coverage scope. The goal is the **sweet spot** where tests get *run*, not avoided. A 30-second smoke that runs every push is worth more than a 30-minute suite that runs once a quarter.

We deliberately do **not** test:
- Real ffmpeg / LLM / image measurements — require GPU + minutes of wall time + actual heat. Validated by accumulated `results/*.json` history.
- Tapo P110 power readings — physical device, single point of failure. Hardware mock would be more code than the actual measurement layer.
- Cross-browser rendering / mobile layout pixels — needs Playwright/Selenium harness. Overkill for current audience.
- Network resilience / timeout edge cases — nginx + uvicorn defaults are sufficient for our load profile.

These are covered by Tier 3 manual checks before high-stakes use.

---

## Tier 1 — Automated smoke (~30 seconds)

**When:** Before every push. Run as a habit, not as a gate.
**Catches:** "I broke an import" · "an endpoint is 500-ing" · "JSON shape changed" · "settings file got corrupted."
**Where:** `scripts/smoke.sh` (to be written; outline below).

```bash
# Smoke test outline — implement as a single bash script
set -e
BASE=http://127.0.0.1:8000
COOKIE="wl_auth=$(grep WATTLAB_GATE_PASSWORD /home/gos/wattlab/.env | cut -d= -f2-)"

# 1. Module imports
cd /home/gos/wattlab/wattlab_service && python3 -c \
    "import main, persist, rag, llm, video, image_gen, power, settings, sources"

# 2. Page routes return 200 (HTML pages — assert content-length > 1000)
for p in "" /video /llm /image /rag /demo /methodology /queue-status /settings; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" -b "$COOKIE" "$BASE$p")
    [ "$code" = "200" ] || { echo "FAIL: $p returned $code"; exit 1; }
done

# 3. Gate page renders without auth
curl -sS "$BASE/gate" | grep -q "WattLab" || { echo "FAIL: /gate"; exit 1; }

# 4. Static asset serves
curl -sS -o /dev/null -w "%{http_code}\n" "$BASE/static/owl.svg" | grep -q "^200$"

# 5. JSON endpoints — assert valid JSON + expected keys
for ep in /live /power /queue /rag/index-status /rag/corpus-list; do
    curl -sS -b "$COOKIE" "$BASE$ep" | python3 -m json.tool > /dev/null \
        || { echo "FAIL: $ep not JSON"; exit 1; }
done

# 6. Pure-function unit checks
python3 -c "
import sys; sys.path.insert(0, '/home/gos/wattlab/wattlab_service')
from rag import confidence
# known inputs → expected flags
assert confidence(50.0, 15, 50.0)['flag'] == '🟢', 'confidence green'
assert confidence(0.5, 3, 50.0)['flag'] == '🔴', 'confidence red'
print('confidence OK')
"

# 7. Settings round-trip
python3 -c "
import sys; sys.path.insert(0, '/home/gos/wattlab/wattlab_service')
import settings
s = settings.load()
assert 'baseline_polls' in s and 'rag_corpus_path' in s
print('settings OK')
"

echo 'SMOKE OK'
```

**Failure mode:** exit 1 on first failure with a one-line message. Don't try to recover.

---

## Tier 2 — Integration smoke (~2-5 minutes)

**When:** Before merging anything that touches `persist.py`, `rag.py`, `settings.py`, or the JSON/CSV shape.
**Catches:** "Persistence shape drifted" · "CSV no longer parses" · "RAG corpus_list out of sync with collection."
**Where:** `scripts/integration.sh` (to be written).

```bash
# Integration test outline
set -e
cd /home/gos/wattlab

# 1. Persistence round-trip — for each job_type, construct minimal dict,
#    save_result, load_result, assert equality on key fields, to_csv, parse.
python3 -c "
import sys, csv, io, json
sys.path.insert(0, 'wattlab_service')
from persist import save_result, load_result, to_csv

# Minimal valid LLM result fixture
fixture = {
    'mode': 'single',
    'model_label': 'TinyLlama',
    'task_label': 'T3',
    'inference': {'output_tokens': 50, 'duration_s': 1.2,
                  'tokens_per_sec': 41.6, 'response': 'test answer'},
    'energy': {'w_base': 52.0, 'w_task': 60.0, 'delta_w': 8.0,
               'delta_e_wh': 0.003, 'mwh_per_token': 0.06,
               'poll_count': 2, 'confidence': {'flag': '🟡', 'label': 'yellow'}},
    'thermals': {'cpu_base': 45.0, 'gpu_base': 38.0},
}
saved = save_result('llm', 'TEST_FIXTURE', fixture)
loaded = load_result('llm', 'TEST_FIXTURE')
assert loaded['inference']['response'] == 'test answer', 'response roundtrip'
csv_text = to_csv('llm', loaded)
rows = list(csv.DictReader(io.StringIO(csv_text)))
assert rows and rows[0]['response'] == 'test answer', 'CSV response column'
saved.unlink()  # cleanup
print('persistence + CSV OK')
"

# 2. RAG corpus_list reads ChromaDB metadata correctly
python3 -c "
import sys
sys.path.insert(0, 'wattlab_service')
from rag import corpus_list, check_index
check_index()
docs = corpus_list()
assert isinstance(docs, list)
if docs:
    assert all('name' in d and 'indexed' in d for d in docs)
    print(f'corpus_list OK: {len(docs)} docs, {sum(d[\"indexed\"] for d in docs)} indexed')
else:
    print('corpus_list OK (empty corpus)')
"

echo 'INTEGRATION OK'
```

**Note:** does NOT trigger a full RAG rebuild (slow, expensive). Reads existing ChromaDB. If you need to test build_index end-to-end, add a separate `scripts/rag-rebuild-test.sh` against a 1-PDF fixture corpus dir — but that's a once-per-quarter check, not part of routine.

---

## Tier 3 — Manual checklist (~5 minutes)

**When:** Before any demo, external feedback push, tagged release, or change that touches HTML/CSS/JS visibly.
**Catches:** "the UI looks broken" · "a flow is half-wired" · "mobile is unreadable."

### Pages render correctly (1 min)
- [ ] Open https://wattlab.greeningofstreaming.org on desktop **and** phone
- [ ] Owl + "WattLab ← Home" wordmark at top of every page (except `/gate`)
- [ ] Sub-labels readable on phone (no `#555` ghost text)
- [ ] `/methodology` shows owl + GoS logo in topbar
- [ ] `/queue-status` shows the owl wordmark via `_BACK`
- [ ] Browser console clean (Cmd+Opt+J on Chrome) — no JS errors

### Video flow (1.5 min)
- [ ] `/video` → Compare All Codecs → `meridian_120s` → Run
- [ ] Stage list advances correctly through 12 stages
- [ ] Matrix populates with all 6 cells (3 codecs × CPU/GPU)
- [ ] Confidence flags present (🟢/🟡)
- [ ] Most-efficient and fastest cells highlighted
- [ ] Download both CSV and JSON — both parse, CSV has `output_size_mb` column

### LLM flow (1 min)
- [ ] `/llm` → TinyLlama → T3 → Run
- [ ] Streaming output appears word-by-word
- [ ] mWh/token shown after completion
- [ ] Download CSV — has `response` column with full text (multi-line, properly quoted)

### RAG flow (1.5 min)
- [ ] `/rag` → green dot, "Index ready · N chunks"
- [ ] Click "Browse corpus documents" — list expands, shows ●/○ indicators per doc
- [ ] Question textarea pre-filled with "What is REM (Remote Energy Measurement)?"
- [ ] Run **Compare 3 modes** with TinyLlama → progress shows "⏱ Cooling down" between modes
- [ ] No negative `mWh/tok` values in any mode (would indicate cooldown bug regression)
- [ ] Phi-4 single run → answer mentions GoS streaming workflows (encoder, origin, packager, telco)

### Guided Tour (1 min)
- [ ] `/demo` → step through Welcome → Findings (7 steps)
- [ ] Findings step: video transcoding section is the visual headline (not a row in a table)
- [ ] LLM / Image / RAG sections appear as collapsible `<details>` blocks below
- [ ] All numbers populate (no "—" placeholders for workloads that ran)

---

## Pre-release additions

For tagged releases (`v1.x.y`):
- [ ] Run Tier 1, 2, 3 in order
- [ ] Variance calibration (`/settings → Run variance calibration`) — completes 🟢
- [ ] `df -h /home/gos/wattlab` — > 10 GB free for next month of results
- [ ] `git status` clean (or only intentional untracked)
- [ ] `JOURNAL.md`, `CLAUDE.md`, `README.md` updated

---

## When to relax this

| Change | Run |
|---|---|
| Typo, comment, doc-only | Tier 1 |
| CSS-only / colour palette / spacing | Tier 1 + visual spot-check on `/`, `/video`, `/rag` |
| Logic change in a single module | Tier 1 + Tier 2 (if module is in scope) |
| Schema / persistence / endpoint shape | Tier 1 + Tier 2 |
| Anything HTML / JS visible | Tier 1 + relevant Tier 3 flow |
| Pre-demo / pre-feedback-push / pre-release | All three tiers |

---

## What this strategy is NOT

- **Not a CI gate.** Nothing here runs in GitHub Actions. WattLab is single-server, single-maintainer; CI overhead would slow us more than it'd help. If we ever onboard a contributor, that's the trigger to wire Tier 1 into a pre-push hook.
- **Not exhaustive.** We test plumbing, not measurements. Measurement correctness is validated by accumulated runs in `results/*.json` and by the variance calibration framework.
- **Not static.** When a bug bites in production, add a Tier 1 or Tier 2 line that would have caught it. When a Tier 3 step never finds anything for six months, delete it.
