## Evaluation

You're right, and my earlier "no overlaps" result was the problem. I checked only **box-vs-box
bounding rectangles**. I never checked whether text fits its box, or where edge labels land.
The diagram passed every check the skill defines while being visually broken.

### What is actually wrong

**1. Edge labels sitting on boxes — the dominant cause (10 of 23 labelled edges).**

draw.io renders an edge label at the edge midpoint unless given an explicit offset.
**Zero of the 23 labelled edges have any offset or waypoint**, so ten land on unrelated boxes:

| Edge label | Lands on |
|---|---|
| `EXECUTE TASK (async, works on suspended task)` | `av-stage` |
| `diff on FILE_NAME` | `whisper`, `cortex-complete` |
| `DROP SERVICE IF EXISTS + EXECUTE JOB SERVICE` | `whisper` |
| `GET media files` | `task` |
| `SPECIFICATION_FILE + volume mount` | `ffmpeg` |
| `EXECUTE NOTEBOOK (rollback mode only)` | `deprecated`, `payload-stage` |
| `progress events incl. terminal SUCCEEDED` | `payload-stage` |
| `put_stream upload ≤ 200 MB hard cap` | `payload-stage` |
| `EXECUTE TASK (guarded on IS_ACTIVE)` | `payload-stage` |
| `object names (no drift)` | `run-events` |

Root cause is structural, not cosmetic: **inter-column gutters are 30-70px** while these labels
need 150-250px. A midpoint label cannot fit in the gap, so it must land on something.

**2. Text wrapping past the room allowed (9 boxes).** Nearly every style sets
`whiteSpace=wrap`, so a too-narrow box does not overflow sideways — it silently **wraps to more
lines** and fills the box wall-to-wall, which is what reads as "text crammed into boxes":

| Box | Authored lines | Wrapped to | Consequence |
|---|---|---|---|
| `pool-boundary` | 1 | **2** | 2nd line drops *inside* the pool, over `notebook` |
| `deprecated` | 5 | **7** | fills 87px of a 105px box |
| `config-store` | 4 | **6** | fills 76px of 105px |
| `av-stage` | 3 | **5** | fills 70px of an 80px box |
| `whisper` | 3 | **4** | fills 53px of 65px |
| `mcp` | 2 | **3** | fills 46px of 55px |
| `resolved-note` | 29 | **31** | **overflows its box by 6px** |

`pool-boundary` is the worst: a wrapped *container* label is painted over its own child.

**3. Cramped gutters.** 6 pairs at ≤8px, 3 at 15px. Content already reaches x=1885 of a
1900-wide page, so there is no slack to spread into without growing the canvas.

### What is wrong with the skill

The skill validates **structural integrity only** — XML parses, round-trip matches, no duplicate
IDs, no dangling refs. My diagram passed all four. It *states* layout requirements ("never
overlapping nodes or edges", "Expand the canvas if needed") but ships **no way to check them**,
so compliance depends entirely on the author eyeballing XML they cannot see rendered. That is the
gap worth closing: a stated requirement with no verification is a requirement that silently rots.

---

## 1. Add `scripts/drawio_lint.py` to the skill  [build the instrument first]

Everything else is unverifiable without this, so it leads. A standalone linter (stdlib only,
matching `drawio_compress.py`) that reads an `.uncompressed.drawio` and exits non-zero on:

| Check | Catches |
|---|---|
| Text fit, **simulating `whiteSpace=wrap`** | all 9 problems in table 2 |
| Edge-label placement vs every non-endpoint vertex | all 10 collisions in table 1 |
| Vertex-vertex overlap (excluding declared containers) | the check I ran ad hoc |
| Children inside their container's bounds + clear of its label band | wrapped container labels |
| Content within `pageWidth`/`pageHeight` | off-canvas drift |
| Minimum gutter between neighbouring boxes (default 20px, configurable) | crowding |
| Duplicate IDs, dangling `source`/`target` | folds in today's checks |

Text metrics are an **approximation** (Arial ≈ `0.58 × fontSize` per char, line height
`1.25 × fontSize`). It cannot be exact without a font engine, so it reports "likely overflow"
with the estimated pixel delta and supports a `--strict` flag. Worth stating plainly in the
script header so nobody mistakes it for a renderer.

## 2. Re-layout the diagram to create real gutters

Right-sizing alone would stop the collisions without giving the breathing room you asked for, so
widen the columns. Concrete plan — gutters go from 30-70px to **140-160px**:

| Column | x | w | Contents |
|---|---|---|---|
| Local | 40 | 210 | `local-files`, `uploader`, `snowhouse` |
| Stage / config | 400 | 250 | `av-stage`, `config-store`, `config-view` |
| Task / gate | 810 | 220 | `task`, `gate`, `deprecated` |
| GPU pool | 1190 | **310** | `notebook`, `whisper` (w 130), `cortex-complete` (w 130), `ffmpeg` |
| (below pool) | 1190 | 310 | `payload-stage`, `rollback-notebook`, `run-events`, `run-status` |
| Data | 1660 | 200 | `results`, `summary-view`, `unified`, `gong-mirror` |
| Consumption | 2020 | 220 | `search`, `semantic`, `agent`, `mcp`, `dashboard` |
| Notes | 2380 | 285 | `legend`, `resolved-note` (h 350 → 380) |

`sf-boundary` → x=370, w=1900. `app-role` spans the last two columns → x=1660, w=580.
Page grows **1900×1020 → 2720×1060** (+43% width). That is the cost of the whitespace; the
diagram is a left-to-right pipeline, so widening suits it better than growing taller.

Box widths follow from the text audit: `av-stage` 180→250, `config-store` 200→250,
`deprecated` 200→220, `whisper` 105→130, `mcp` 170→220, `pool-boundary` 270→310,
`resolved-note` 265→285.

## 3. Offset the edge labels that still collide

A 150px gutter still cannot centre a 250px label cleanly, so after the re-layout re-run the
linter and fix each remaining hit with explicit label geometry — shifting along the edge and
perpendicular to it:

```xml
<mxGeometry relative="1" x="-0.4" y="0" as="geometry">
  <mxPoint as="offset" x="0" y="-14" />
</mxGeometry>
```

Driven by linter output rather than guesswork, so the result is verified rather than hoped for.
Also split the longest labels across more lines so they are tall-and-narrow instead of wide.

## 4. Fix the two small things

`title`/`subtitle` are touching at 0px — give the subtitle a 6px gap. `resolved-note` height to
380 so its 31 wrapped lines fit.

## 5. Compress and verify

Compress with the skill's bundled script, then confirm: round-trip cell count matches (74),
payload not zlib-wrapped (must not start `eJx`), `xmllint` clean, **and `drawio_lint.py` exits 0**
on the source. Probe the compressed artifact for the same content strings as last time so the
corrected claims are known to have survived.

## 6. Harden `SKILL.md`

- **New "Layout rules" section** with the things that would have prevented this:
  - Size a box from its text: `width ≥ longest_line × 0.58 × fontSize + 12`,
    `height ≥ lines × 1.25 × fontSize + 8`, and remember `whiteSpace=wrap` converts a
    too-narrow box into extra lines rather than an obvious overflow.
  - **Leave ≥150px between columns that carry labelled edges** — an edge label needs somewhere
    to live, and the midpoint is the default.
  - Container labels wrap too; reserve a label band and keep children below it.
  - Prefer several short label lines over one long one.
- **Rewrite Step 3 (Validate)** to run `drawio_lint.py` as the first check, demoting the current
  four structural checks to "necessary but not sufficient — a diagram can pass all of these and
  still be unreadable."
- **Extend the checklist** with "layout lint passed (text fit, label placement, overlap, gutters)".
- **Add a stopping point:** run the linter *before* the compress step, since the existing Step
  1→2 gate asks the user to confirm a layout that nobody can currently measure.

## Verification

1. `drawio_lint.py` exits 0 on `architecture.uncompressed.drawio`.
2. Edge-label collisions: 10 → **0**. Text-fit problems: 9 → **0**.
3. Minimum gutter between content boxes ≥ 20px; inter-column ≥ 140px.
4. Round-trip 74 cells; not `eJx`-prefixed; `xmllint` clean on both files.
5. Compressed artifact still contains `name from config`, `4 launched + 1 SKIPPED`,
   `TRANSCRIBE_JOB`, `PAYLOAD_STAGE`, `ROLLBACK ONLY`, `381 media files`, `553 rows`.
6. Deliberate negative test: shrink one box in a scratch copy, confirm the linter **fails** —
   a check that cannot fail is not a check.

## Out of scope

- `architecture.md` prose — verified against the live account earlier; unchanged here.
- The `_client.drawio` variant the skill offers; can follow once the internal one is clean.
- The open teardown guard C gap in `999_teardown.sql` — still unfixed, tracked separately.
