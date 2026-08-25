# Show Bible Tracker

A local, zero-dependency Python web app for authoring, tracking and
correcting the prompt bible behind an AI-generated film. It manages
multiple projects, each with its own source data, prompt cards and
review state, and can draft the identity layer of a fresh project from
a story document via Claude.

## What it does

- **Cards** — turns a project's `matrix_data.json` (characters, factions,
  props, locations, lighting, etc.) into a set of stable-keyed prompt
  cards you can copy into an image / video model interface.
- **Review** — per-card status (pending / generated / approved / needs
  revision), free-form director notes, drift detection when a prompt
  was regenerated after the review was made.
- **Correction loop** — apply a director's note through Claude, which
  makes a surgical edit to the underlying matrix source fields and
  reruns the generator. Includes undo.
- **Story ingest** — paste or load a story into a new project and have
  Claude draft the identity layer as a staged proposal. The proposal is
  never merged until you review it item by item and accept the ones you
  want.
- **Multi-project** — each project is a self-contained folder under
  `projects/`; switching between them preserves per-project view state.

## Requirements

- **Python 3.10+**. Standard library only, no packages to install.
- **Anthropic API key** (optional). Only needed for the note-correction
  loop and story ingestion. Everything else works without it.

## Install and run

```bash
tar -xzf show-bible-tracker.tar.gz
cd show-bible-tracker

# Optional: create .env from the example if you want Claude features
cp .env.example .env
# then edit .env and paste your key over the placeholder,
# OR skip this and paste it into the app when the keybar appears

python3 app.py
```

Open <http://127.0.0.1:8777>. Default port is `8777`; change `PORT` at
the top of `app.py` if you need something else.

## First project

Two paths.

### From an empty matrix

1. Click `+ New project`.
2. Give it a name, choose "start from house defaults" (there is no
   existing project to copy doctrine from on a fresh install).
3. Leave the story field empty.
4. The project is created with an empty matrix — you get five layout
   header cards immediately, and any collection you populate in
   `projects/<slug>/matrix_data.json` shows up as cards on the next
   `Regenerate all`.

### From a story

1. Click `+ New project`.
2. Give it a name, paste or drop a treatment / novella / synopsis into
   the story field, click Create.
3. The proposal modal opens automatically. Claude drafts the identity
   layer in five per-collection calls (lighting → props → locations →
   factions → characters) and shows you every drafted item with its
   source quote.
4. Tick the ones to accept, click Apply. `matrix_data.json` is written
   and the generator runs.

The story is preserved at `projects/<slug>/SOURCE_STORY.md` and can be
re-ingested by clicking the `Ingest story` button in the toolbar.

## Project layout

```
projects/<slug>/
  project.json             configuration: title, doctrine, section list
  matrix_data.json         source data — the thing prompts are built from
  cards.json               generated: the current prompt cards
  review_state.json        per-card status and notes
  versions.json            content hashes for drift detection
  SOURCE_STORY.md          optional: the story a matrix was drafted from
  ingest_proposal.json     transient: a pending story-ingest proposal
  ingest_undo.json         transient: snapshot for undoing an ingest
  undo.json                transient: stack of note-apply undos
  SHOW_BIBLE_PROMPTS.md    generated: human-readable card export
  REVISION_BRIEF.md        generated: the "needs revision" export
```

Every file with a `transient` note is safe to delete — the tracker will
re-create it or ignore its absence.

## HTTP API

The UI is a single-page client on top of a small JSON API. Every write
takes an optional `project` field; omit it to hit the currently active
one.

| Method | Path                     | Purpose |
|--------|--------------------------|---------|
| GET    | `/api/state`             | full payload for the client (cards, reviews, projects) |
| GET    | `/api/source?key=…`      | resolve a card key back to its editable matrix fields |
| GET    | `/api/ingest`            | fetch the current staged proposal, if any |
| POST   | `/api/project`           | switch the active project |
| POST   | `/api/project/new`       | `{title, copy_doctrine?, story?}` — scaffold a new project |
| POST   | `/api/review`            | `{key, status?, note?}` — set card status / note |
| POST   | `/api/source`            | `{edits:[{path,value},…]}` — write matrix fields and regenerate |
| POST   | `/api/apply_note`        | `{key, note}` — Claude applies a note and edits source |
| POST   | `/api/undo`              | pop the most recent note-apply undo |
| POST   | `/api/regenerate`        | rerun the generator on the current matrix |
| POST   | `/api/brief`             | write and return the revision brief |
| POST   | `/api/ingest`            | run story extraction, write `ingest_proposal.json` |
| POST   | `/api/ingest/apply`      | `{accepted_ids:[…]}` — merge accepted proposal items |
| POST   | `/api/ingest/undo`       | restore the pre-apply matrix snapshot |
| POST   | `/api/ingest/discard`    | delete the pending proposal |
| POST   | `/api/key`               | `{key}` — save and verify an Anthropic key |

Programmatic use, from another system, minimal example:

```bash
# Create a project with a story attached (waits ~30s for scaffolding)
curl -sX POST localhost:8777/api/project/new \
  -H 'content-type: application/json' \
  -d '{"title":"My Film","copy_doctrine":null,
       "story":"Once upon a time..."}'

# Draft the identity layer (waits several minutes)
curl -sX POST localhost:8777/api/ingest \
  -H 'content-type: application/json' \
  -d '{"project":"my-film"}'

# Merge every drafted item
IDS=$(curl -s "localhost:8777/api/ingest?project=my-film" \
      | python3 -c 'import json,sys,itertools;d=json.load(sys.stdin)["proposal"];print(json.dumps([i["id"] for c in ("lighting","characters","factions","props","locations") for i in d.get(c,[])]))')
curl -sX POST localhost:8777/api/ingest/apply \
  -H 'content-type: application/json' \
  -d "{\"project\":\"my-film\",\"accepted_ids\":$IDS}"
```

## Doctrine and sections

Each project's `project.json` carries two blocks the generator reads:

- `doctrine` — string values inlined into prompts. Common keys:
  - `period_lock` — verbatim period-styling clause on every image card
  - `photo_lock`  — photographic realism clause (aperture, film stock…)
  - `crowd_line`  — crowd variety clause for group plates
  - `cine_look`   — cinematographic look language for video cards
  - `lean_text_lock` — readable-text guard for text-heavy environments
- `sections` — the ordered list of card sections. Each entry names a
  `collection` (which drives the factory) plus a `short`, `md_title`
  and `blurb`. The included lean tier stack is a working default:
  `T1_SOULS`, `T1F_FACTIONS`, `T2_PLATES`, `T5_PROPS`, `LAYOUTS`.

The matrix schema story ingest produces is documented at the top of
`story_ingest.py`, and every generator factory that consumes it is in
`generate_prompts.py`.

## Data and privacy

Everything lives in `projects/` on the machine the tracker runs on.
Nothing leaves the box except calls to the Anthropic API when you
apply a note or ingest a story. The `.env` file is `chmod 600` on
write and is never returned by any endpoint.

## License

Original tooling by Patrick Fogarty. Use freely.
