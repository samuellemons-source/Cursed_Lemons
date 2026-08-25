#!/usr/bin/env python3
"""Show Bible Tracker — local multi-project production app.

A zero-dependency (stdlib only) local web app whose buttons actually
execute:

- switch between isolated projects; each keeps its own matrix, cards,
  operator state, house doctrine and output files
- browse/filter/search all prompt cards (from the project's cards.json)
- copy prompts for manual input on higgsfield.ai
- track status + director notes per card (review_state.json, keyed by
  STABLE card keys, survives regeneration)
- edit the underlying matrix source fields for any card and
  Save & Regenerate — rewrites that project's matrix_data.json, reruns
  generate_prompts.py for that project, and refreshes the cards in place
- export an agent brief of every "needs revision" card + note for
  freeform AI-applied rewrites
- create a new empty project, ready to be populated

Run:  python3 production/generator/app.py     → http://127.0.0.1:8777
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(HERE, "projects")
APP_STATE_PATH = os.path.join(HERE, "app_state.json")
GENERATOR = os.path.join(HERE, "generate_prompts.py")
# No preferred default slug in a shared build — the first available project
# wins, and an empty install shows the empty state until one is created.
DEFAULT_SLUG = None

COLLECTIONS = ["SCORE", "CHARACTERS", "FACTIONS", "CREATURES", "PROPS", "MOVERS",
               "LOCATIONS", "LOOK_PLATES"]

PORT = int(os.environ.get("SBT_PORT", "8777"))
# Reentrant: handlers that already hold the lock call helpers (apply_edits)
# that take it again.
_lock = threading.RLock()


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "x"


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------- project layer

def list_projects():
    """[{slug, title, cards, generated}] for every project folder."""
    if not os.path.isdir(PROJECTS_DIR):
        return []
    out = []
    for s in sorted(os.listdir(PROJECTS_DIR)):
        cfg_path = os.path.join(PROJECTS_DIR, s, "project.json")
        if not os.path.isfile(cfg_path):
            continue
        cfg = load_json(cfg_path, {})
        cards_doc = load_json(os.path.join(PROJECTS_DIR, s, "cards.json"), {})
        out.append({"slug": s,
                    "title": cfg.get("title", s),
                    "subtitle": cfg.get("subtitle", ""),
                    "cards": len(cards_doc.get("cards", [])),
                    "generated": cards_doc.get("generated", "—")})
    return out


def known_slugs():
    return [p["slug"] for p in list_projects()]


def active_slug():
    known = known_slugs()
    stored = load_json(APP_STATE_PATH, {}).get("active")
    if stored in known:
        return stored
    if DEFAULT_SLUG and DEFAULT_SLUG in known:
        return DEFAULT_SLUG
    return known[0] if known else (DEFAULT_SLUG or "")


def set_active(slug_name):
    state = load_json(APP_STATE_PATH, {})
    state["active"] = slug_name
    save_json(APP_STATE_PATH, state)


def ppath(slug_name, name):
    return os.path.join(PROJECTS_DIR, slug_name, name)


def load_reviews(slug_name):
    # On a totally empty install `active_slug()` returns ""; do not create
    # a phantom review_state.json at the projects/ root just to serve one
    # /api/state call before any project exists.
    if not slug_name:
        return {}
    path = ppath(slug_name, "review_state.json")
    reviews = load_json(path, None)
    if reviews is not None:
        return reviews
    reviews = {}
    save_json(path, reviews)
    return reviews


def brief_path(slug_name):
    cfg = load_json(ppath(slug_name, "project.json"), {})
    raw = (cfg.get("outputs") or {}).get("brief") or "REVISION_BRIEF.md"
    raw = os.path.expanduser(raw)
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(PROJECTS_DIR, slug_name, raw))


def run_generator(slug_name):
    """Run generate_prompts.py for one project; returns (ok, message)."""
    proc = subprocess.run(
        [sys.executable, GENERATOR, "--project", slug_name],
        cwd=HERE, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "generator failed").strip()
    return True, proc.stdout.strip()


STORY_FILENAME = "SOURCE_STORY.md"


def create_project(title, copy_doctrine_from=None, story=None):
    """Scaffold projects/<slug>/ and build it (0 cards). Returns the slug.

    When `story` is provided it is written to SOURCE_STORY.md alongside the
    project's other artefacts. This does NOT auto-run extraction — the ingest
    step is explicit so an operator can review the drafted matrix before it
    touches production data."""
    name = slug(title)
    if not name:
        raise ValueError("a project needs a name")
    pdir = os.path.join(PROJECTS_DIR, name)
    if os.path.exists(pdir):
        raise ValueError(f"project '{name}' already exists")

    doctrine = {}
    sections = None
    if copy_doctrine_from:
        src = load_json(ppath(copy_doctrine_from, "project.json"), {})
        doctrine = src.get("doctrine", {})
        sections = src.get("sections")
    if sections is None:
        sys.path.insert(0, HERE)
        from generate_prompts import DEFAULT_SECTIONS
        sections = DEFAULT_SECTIONS

    os.makedirs(pdir)
    story_text = (story or "").strip()
    if story_text:
        with open(os.path.join(pdir, STORY_FILENAME), "w",
                  encoding="utf-8") as f:
            f.write(story_text + "\n")
        source_note = f"{STORY_FILENAME} (extracted into the production matrix)"
    else:
        source_note = "the production matrix"

    cfg = {
        "slug": name,
        "title": title,
        "subtitle": "",
        "source_note": source_note,
        "outputs": {
            "markdown": "SHOW_BIBLE_PROMPTS.md",
            "brief": "REVISION_BRIEF.md",
            "canvas": None,
        },
        "doctrine": doctrine,
        "sections": sections,
    }
    save_json(os.path.join(pdir, "project.json"), cfg)
    empty = {c: [] for c in COLLECTIONS}
    empty["LIGHTING"] = {}
    save_json(os.path.join(pdir, "matrix_data.json"), empty)
    save_json(os.path.join(pdir, "review_state.json"), {})

    ok, msg = run_generator(name)
    if not ok:
        shutil.rmtree(pdir, ignore_errors=True)
        raise ValueError(f"generator failed for new project: {msg}")
    return name


def load_story(slug_name):
    """Return the SOURCE_STORY.md contents for a project, or '' if none."""
    path = ppath(slug_name, STORY_FILENAME)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def save_story(slug_name, story_text):
    """Write (or overwrite) SOURCE_STORY.md for a project. Also updates
    project.json source_note to reflect that the matrix has a story behind
    it, so provenance survives even if the project was created empty."""
    text = (story_text or "").strip()
    path = ppath(slug_name, STORY_FILENAME)
    if not text:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    cfg_path = ppath(slug_name, "project.json")
    cfg = load_json(cfg_path, {})
    if cfg:
        cfg["source_note"] = f"{STORY_FILENAME} (extracted into the production matrix)"
        save_json(cfg_path, cfg)


PROPOSAL_FILENAME = "ingest_proposal.json"


# ------------------------------------------------------------- Claude layer
# Applies a freeform director note to a card's source fields. The model only
# ever rewrites values of fields we hand it; it cannot add fields, touch the
# schema, or reach any other card.

ENV_PATH = os.path.join(HERE, ".env")
_model_cache = {}


def load_env_file():
    """Read KEY=VALUE from .env. Takes precedence over the process env,
    since a stale/invalid exported key is the common failure here."""
    out = {}
    if not os.path.exists(ENV_PATH):
        return out
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def anthropic_key():
    return (load_env_file().get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def check_key(key):
    """Catch the common paste mistakes before spending a round trip."""
    where = "production/generator/.env"
    if not key:
        if os.path.exists(ENV_PATH):
            return (f"{where} exists but has no ANTHROPIC_API_KEY line. Each "
                    "line must look like ANTHROPIC_API_KEY=sk-ant-… — if you "
                    "pasted something else over it, retype that line.")
        return f"No Anthropic API key. Create {where} with ANTHROPIC_API_KEY=sk-ant-…"
    if key == "PASTE_KEY_HERE" or key.startswith("sk-ant-..."):
        return f"{where} still holds the placeholder — paste your real key over it."
    if not key.startswith("sk-ant"):
        return (f"The ANTHROPIC_API_KEY in {where} doesn't look like an "
                f"Anthropic key (it starts with {key[:6]!r}). Check you pasted "
                "the key and not something else from the clipboard.")
    return None


def anthropic_request(path, payload=None, method="GET", timeout=120):
    key = anthropic_key()
    problem = check_key(key)
    if problem:
        raise RuntimeError(problem)
    req = urllib.request.Request(
        "https://api.anthropic.com" + path, method=method,
        data=json.dumps(payload).encode() if payload else None,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        if e.code in (401, 403):
            raise RuntimeError(
                f"Anthropic rejected the key ({detail}). Update "
                "ANTHROPIC_API_KEY in production/generator/.env.")
        raise RuntimeError(f"Anthropic error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Anthropic: {e.reason}")


def pick_model():
    """Configured model, else the newest Sonnet the key can see."""
    env_model = load_env_file().get("ANTHROPIC_MODEL")
    if env_model:
        return env_model
    if "id" in _model_cache:
        return _model_cache["id"]
    data = anthropic_request("/v1/models?limit=50").get("data", [])
    ids = [m["id"] for m in data]
    chosen = next((i for i in ids if "sonnet" in i), None) or (ids[0] if ids else None)
    if not chosen:
        raise RuntimeError("The key can't list any Anthropic models.")
    _model_cache["id"] = chosen
    return chosen


REWRITE_SYSTEM = """\
You are a script supervisor maintaining the source data behind a film's \
image-prompt bible. You make SURGICAL edits to existing prompt source text.

HOW TO READ THE REQUEST. You are given the ASSEMBLED PROMPT (what is actually \
sent to the image model) and the SOURCE FIELDS it was built from. The \
director's note describes what they SAW in the rendered image. Your job is \
diagnostic before it is editorial:

A. First classify the note into ONE of three shapes. They need opposite \
fixes, and misreading the shape is the most common failure:

  SHAPE 1 — UNWANTED ELEMENT. "It keeps rendering a work apron." Something \
  appears that should not. The cause is text that INVITES it, usually an \
  implication rather than a literal mention: "dressed for the wood shop", \
  "a butcher's setup" and "ready to weld" all summon an apron without ever \
  saying "apron". Occupational, activity and setting phrases drag their \
  stereotypical costume along. Fix by neutralising the inviting phrase — \
  reword it so the intent survives and the association dies. Never answer \
  "the word does not appear, so there is nothing to remove."

  SHAPE 2 — WRONG ATTRIBUTE. "Chris is a Black man, renders are of a white \
  man." A property of the subject is being rendered incorrectly. There are \
  TWO possible causes and usually BOTH are present:
    (a) CONTRADICTING DESCRIPTORS — wording that implies the wrong value. \
    "ruddy cheeks", "pale eyes", "rosy", "fair", "sunburned" all imply white \
    skin. "willowy" implies slight build. Adjust or remove them.
    (b) SILENCE — the text never states the attribute at all, so the image \
    model fills it with its own default. AN UNSTATED ATTRIBUTE IS A CAUSE, \
    NOT AN ABSENCE OF ONE. This is the single most important thing to \
    understand: you are not only searching for offending text, you are also \
    searching for a MISSING statement. Fix by adding an explicit, plain \
    statement of the correct value.
  Identity attributes default aggressively when unstated: ethnicity, skin \
  tone, age, gender, body type, hair. If the note reports one of these \
  wrong, the fix almost always includes stating it explicitly.

  SHAPE 3 — MISSING ELEMENT. "He should be wearing gloves." Add it, in the \
  register of the surrounding text.

B. Before editing a character, check the OTHER entities' text for house \
convention and match it. If a sibling character's base opens "a 22-year-old \
Latino-American man", then the correct fix for a race note is the same \
construction in the same position — not a euphemism, not a scattering of \
hints. State it plainly and early, where the model weights it most.
C. Do not manufacture a cause. Never make an unrelated or cosmetic edit just \
to look responsive — a wrong edit is worse than no edit.
C2. Rate your diagnosis with a "confidence" field:
  "high" — any of: you can quote a phrase whose ordinary association \
  produces the complained-of element ("dressed for the wood shop" → apron); \
  OR the note states a fact about the subject that the text contradicts; OR \
  the note states a fact the text never establishes and the fix is to state \
  it. Shape 2 and Shape 3 notes are nearly always "high", because the \
  director is telling you the correct value directly — there is nothing to \
  guess.
  "low" — you had to build a chain of inferences, the phrase is merely \
  vague, or you are speculating about how a model resolves ambiguity. A note \
  naming an element with no trigger in the text AND no attribute to state is \
  genuinely "low".
Confidence "low" means your edits will NOT be applied — only your diagnosis \
is shown to the operator. Do not inflate confidence to force a change \
through. But do not hide behind "low" either: if the director has told you \
what the subject IS, you have everything you need and the answer is "high".
C3. "That is a generation seed or face-lock problem, not a textual one" is \
almost never the right answer for a subject attribute. The text is the only \
lever available here. Use it before blaming the pipeline.
D. Text that appears in the assembled prompt but in NO source field is \
generator doctrine (camera, lens, film stock, lighting, era, studio look). \
You cannot edit it. If the cause lives there, say so explicitly instead of \
reporting no change.

Rules you must follow:
1. Apply the director's note and nothing else. Change the fewest words \
possible. Everything the note does not concern must survive verbatim.
2. Preserve the existing voice, register, sentence rhythm and level of \
physical detail. You are editing, not rewriting.
3. NEVER add camera, lens, film-stock, lighting, era or resolution language. \
A downstream generator appends all of that automatically. Source fields \
describe only the subject: who/what it is, wardrobe, wear, staging, action.
4. Never invent wardrobe, props or characters the note did not ask for. This \
bans UNREQUESTED additions only — when the note asks for something to be \
added or corrected, adding the words that establish it is the required fix, \
not a violation. If the note says to remove something, remove it and repair \
the grammar so the sentence still reads naturally.
5. A field marked SHARED feeds many cards. Prefer the state-specific field \
when the cause is genuinely there. But if your diagnosis locates the cause \
in a shared field, edit the shared field — that is the correct fix — and say \
plainly in the summary that it changes every card for this entity.
6. If the note is ambiguous or you cannot apply it safely, return an empty \
edits list and explain why in the summary.

Return STRICT JSON only, no prose, no markdown fence:
{"diagnosis":"<the exact phrase you identified as the cause, and why>",\
"confidence":"high"|"low","edits":[{"path":<the exact path array you were \
given>,"value":"<full new field value>"}],"summary":"<one sentence, what you \
changed and why>"}
Return a field only if its text actually changes. `value` is the COMPLETE new \
text of that field, not a diff."""


def apply_note_with_claude(slug_name, card_key, card, note, fields):
    editable = [f for f in fields if isinstance(f["value"], str)]
    if not editable:
        raise RuntimeError("This card has no editable source fields.")
    payload_fields = [{"label": f["label"], "path": f["path"],
                       "shared": bool(f["shared"]), "value": f["value"]}
                      for f in editable]
    assembled = card.get("prompt") or ""
    if isinstance(assembled, list):
        assembled = "\n\n".join(assembled)
    user = (
        f"CARD: {card.get('num', '?')} — {card.get('title', card_key)}\n"
        f"CARD KEY: {card_key}\n\n"
        f"DIRECTOR'S NOTE (describes what they saw in the render):\n{note}\n\n"
        f"ASSEMBLED PROMPT ACTUALLY SENT TO THE IMAGE MODEL — read this "
        f"critically to diagnose the cause:\n\"\"\"\n{assembled}\n\"\"\"\n\n"
        f"QC LINE: {card.get('qc', '')}\n\n"
        f"SOURCE FIELDS (the only editable text; anything in the assembled "
        f"prompt that is absent here is generator doctrine you cannot "
        f"change):\n"
        + json.dumps(payload_fields, ensure_ascii=False, indent=2))

    resp = anthropic_request("/v1/messages", {
        "model": pick_model(),
        "max_tokens": 8000,
        "system": REWRITE_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }, method="POST")

    text = "".join(b.get("text", "") for b in resp.get("content", [])).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    if resp.get("stop_reason") == "max_tokens":
        raise RuntimeError(
            "Claude's reply was cut off before it finished. Nothing was "
            "changed. Try a narrower note, or split it into two notes.")
    try:
        out = json.loads(text)
    except ValueError:
        raise RuntimeError("Claude did not return usable JSON: " + text[:200])

    allowed = {json.dumps(f["path"]): f for f in editable}
    edits, changes = [], []
    # A low-confidence diagnosis is a guess. Surface the reasoning but refuse
    # to write it, so a speculative edit can never reach the source data.
    if (out.get("confidence") or "").strip().lower() != "high":
        why = (out.get("diagnosis") or "").strip()
        return [], [], ("No confident cause found in the editable source "
                        "text, so nothing was changed." +
                        (" " + why if why else "")), why
    for e in out.get("edits") or []:
        pk = json.dumps(e.get("path"))
        if pk not in allowed:
            continue  # refuse any path we did not offer
        old = allowed[pk]["value"]
        new = e.get("value")
        if not isinstance(new, str) or new == old:
            continue
        edits.append({"path": e["path"], "value": new})
        changes.append({"label": allowed[pk]["label"], "shared": allowed[pk]["shared"],
                        "path": e["path"], "before": old, "after": new})
    return (edits, changes, (out.get("summary") or "").strip(),
            (out.get("diagnosis") or "").strip())


def push_undo(slug_name, card_key, changes):
    path = ppath(slug_name, "undo.json")
    stack = load_json(path, [])
    stack.append({"key": card_key,
                  "edits": [{"path": c["path"], "value": c["before"]}
                            for c in changes]})
    save_json(path, stack[-20:])


def pop_undo(slug_name):
    path = ppath(slug_name, "undo.json")
    stack = load_json(path, [])
    if not stack:
        return None
    entry = stack.pop()
    save_json(path, stack)
    return entry


def save_key(key):
    """Write ANTHROPIC_API_KEY into .env, preserving any other settings,
    then prove it works by listing models."""
    key = (key or "").strip().strip('"').strip("'")
    problem = check_key(key)
    if problem:
        raise RuntimeError(problem)
    lines, written = [], False
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8").read().splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY=") and not written:
                lines.append(f"ANTHROPIC_API_KEY={key}")
                written = True
            else:
                lines.append(line)
    if not written:
        lines.append(f"ANTHROPIC_API_KEY={key}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.chmod(ENV_PATH, 0o600)
    _model_cache.pop("id", None)
    return pick_model()          # raises if the key is rejected


def key_ok():
    return check_key(anthropic_key()) is None


def project_payload(slug_name):
    cards_doc = load_json(ppath(slug_name, "cards.json"),
                          {"generated": "—", "cards": []})
    return {"active": slug_name,
            "projects": list_projects(),
            "generated": cards_doc.get("generated", "—"),
            "title": cards_doc.get("title", slug_name),
            "key_ok": key_ok(),
            "cards": cards_doc.get("cards", []),
            "reviews": load_reviews(slug_name),
            "has_story": bool(load_story(slug_name).strip()),
            "has_proposal": os.path.exists(
                ppath(slug_name, "ingest_proposal.json"))}


# ---------------------------------------------------------------- source map

WARDROBE_SLOT_LABELS = [
    ("order", "layer order"),
    ("head", "head, eyewear and headgear"),
    ("torso", "torso layers"),
    ("hands", "hands"),
    ("legs", "legs"),
    ("feet", "feet"),
    ("carried", "carried items"),
    ("closing", "closing set — what is NOT worn"),
]


def resolve_fields(key, data):
    """Map a stable card key back to its editable matrix_data.json fields.
    Returns a list of {label, path, value, shared}."""
    segs = key.split("/")
    eid = segs[0]
    fields = []

    def add(label, path, shared=False):
        cur = data
        for p in path:
            cur = cur[p]
        fields.append({"label": label, "path": path, "value": cur,
                       "shared": shared})

    def add_light(container):
        lid = container.get("light") or container.get("group_light")
        if lid and lid in data["LIGHTING"]:
            add(f"Lighting {lid} (SHARED — edits every plate that uses it)",
                ["LIGHTING", lid], shared=True)

    for ci, ch in enumerate(data["CHARACTERS"]):
        if ch["id"] == eid:
            add("Character base — identity/physiognomy "
                "(SHARED across all states)",
                ["CHARACTERS", ci, "base"], shared=True)
            for sl, lbl in WARDROBE_SLOT_LABELS:
                if (ch.get("wardrobe") or {}).get(sl):
                    add(f"Wardrobe — {lbl} (SHARED across all states)",
                        ["CHARACTERS", ci, "wardrobe", sl], shared=True)
            if len(segs) >= 2:
                for si, st in enumerate(ch["states"]):
                    if st["id"] == segs[1]:
                        add(f"State delta — {st['name']}",
                            ["CHARACTERS", ci, "states", si, "delta"])
            return fields

    for fi, fa in enumerate(data["FACTIONS"]):
        if fa["id"] == eid:
            if len(segs) >= 3 and segs[1] == "var":
                for vi, va in enumerate(fa.get("variants", [])):
                    if slug(va["name"]) == segs[2]:
                        add(f"Variant description — {va['name']}",
                            ["FACTIONS", fi, "variants", vi, "base"])
            elif len(segs) >= 2 and segs[1] == "group":
                add("Group plate prompt", ["FACTIONS", fi, "group"])
                add("Group QC line", ["FACTIONS", fi, "group_qc"])
                add_light(fa)
            else:
                add("Faction base (SHARED: rep sheets + class identity)",
                    ["FACTIONS", fi, "base"], shared=True)
            return fields

    for coll, prompt_field in (("CREATURES", "prompt"), ("MOVERS", "prompt")):
        for ei, en in enumerate(data[coll]):
            if en["id"] == eid:
                if coll == "MOVERS" and len(segs) >= 2 and segs[1] == "sheet":
                    add("Vehicle base description", [coll, ei, "base"])
                    return fields
                if len(segs) >= 2:
                    for si, st in enumerate(en["states"]):
                        if slug(st["name"]) == segs[1]:
                            add(f"Plate prompt — {st['name']}",
                                [coll, ei, "states", si, prompt_field])
                            add("QC line", [coll, ei, "states", si, "qc"])
                            add_light(st)
                return fields

    for pi, pr in enumerate(data["PROPS"]):
        if pr["id"] == eid:
            add("Prop base (SHARED across states)",
                ["PROPS", pi, "base"], shared=True)
            if len(segs) >= 2:
                for si, st in enumerate(pr["states"]):
                    if slug(st["name"]) == segs[1]:
                        add(f"State delta — {st['name']}",
                            ["PROPS", pi, "states", si, "delta"])
                        if "qc" in st:
                            add("QC line", ["PROPS", pi, "states", si, "qc"])
            return fields

    for li, lo in enumerate(data["LOCATIONS"]):
        if lo["id"] == eid:
            add("Geography lock slice (SHARED across states)",
                ["LOCATIONS", li, "geo"], shared=True)
            add("Anchor object (SHARED across states)",
                ["LOCATIONS", li, "anchor"], shared=True)
            if len(segs) >= 2:
                for si, st in enumerate(lo["states"]):
                    if slug(st["name"]) == segs[1]:
                        add(f"State delta — {st['name']}",
                            ["LOCATIONS", li, "states", si, "delta"])
                        add("QC line", ["LOCATIONS", li, "states", si, "qc"])
                        add_light(st)
            return fields

    for xi, lp in enumerate(data["LOOK_PLATES"]):
        if lp["id"] == eid:
            add("Look plate prompt", ["LOOK_PLATES", xi, "prompt"])
            add("QC line", ["LOOK_PLATES", xi, "qc"])
            return fields

    for si, sh in enumerate(data.get("SHOTS", [])):
        if sh["id"] == eid:
            # Every card variant for the same shot (first / last / video)
            # shares the same editable shot record — swap in the fields
            # that survive across all three so a director note can land on
            # any card and correct all three at once.
            add("Shot intent (one-line why this shot exists)",
                ["SHOTS", si, "intent"])
            frames = sh.get("frames") or {}
            if "first" in frames:
                add("First-frame blocking",
                    ["SHOTS", si, "frames", "first"])
            if "last" in frames:
                add("Last-frame blocking",
                    ["SHOTS", si, "frames", "last"])
            add("Physics note", ["SHOTS", si, "physics"])
            add("Character acting note", ["SHOTS", si, "acting"])
            add("Positive constraints", ["SHOTS", si, "constraints"])
            flow = sh.get("flow") or {}
            if "in" in flow:
                add("Flow in (cut-in)", ["SHOTS", si, "flow", "in"])
            if "out" in flow:
                add("Flow out (cut-out)", ["SHOTS", si, "flow", "out"])
            dlg = sh.get("dialogue") or {}
            if "line" in dlg:
                add("Dialogue line",
                    ["SHOTS", si, "dialogue", "line"])
            # Shared GEO zone: editing the zone updates every shot that
            # sits in it, so flag it as SHARED like a lighting id.
            loc = next((lo for lo in data["LOCATIONS"]
                        if lo["id"] == sh.get("loc")), None)
            zid = (loc or {}).get("geo_zone")
            zones = ((data.get("GEO") or {}).get("zones") or {})
            if zid and zid in zones:
                add(f"GEO zone {zid} (SHARED — edits every shot in this "
                    "zone)", ["GEO", "zones", zid, "notes"], shared=True)
            return fields

    return fields


def apply_edits(slug_name, edits):
    """Apply [{path, value}] to the project's matrix, then regenerate it."""
    with _lock:
        data_path = ppath(slug_name, "matrix_data.json")
        data = load_json(data_path, {})
        for e in edits:
            cur = data
            path = e["path"]
            for p in path[:-1]:
                cur = cur[p]
            cur[path[-1]] = e["value"]
        save_json(data_path, data)
        return run_generator(slug_name)


def build_brief(slug_name, reviews, cards):
    by_key = {c["key"]: c for c in cards}
    cfg = load_json(ppath(slug_name, "project.json"), {})
    base = f"production/generator/projects/{slug_name}"
    lines = [
        f"# REVISION BRIEF — {cfg.get('title', slug_name)}",
        "",
        f"Project: `{slug_name}`",
        "",
        "Agent instructions: for each card below, apply the director's note "
        f"by editing `{base}/matrix_data.json` (that project's single "
        "source of truth), then rerun "
        f"`python3 production/generator/generate_prompts.py --project "
        f"{slug_name}`. After applying a note, set that card's status back "
        f"to `pending` in `{base}/review_state.json` and clear or annotate "
        "the note. Card keys are stable: entity-id/state-id/view.",
        "",
    ]
    n = 0
    for key, rv in reviews.items():
        if rv.get("status") == "revise":
            n += 1
            c = by_key.get(key, {})
            lines.append(f"## {c.get('num', '?')} — {c.get('title', key)}")
            lines.append(f"- key: `{key}`")
            note = (rv.get("note") or "").strip() or "(no note left)"
            lines.append(f"- note: {note}")
            lines.append("")
    if n == 0:
        lines.append("_No cards are currently marked as needing revision._")
    return "\n".join(lines), n


# ---------------------------------------------------------------- HTTP layer

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Show Bible Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#101014; --panel:#17171d; --panel2:#1e1e26; --line:#2a2a34;
  --text:#e8e8ee; --dim:#9a9aa8; --faint:#67677a;
  --accent:#7bafe9; --green:#3fa266; --orange:#dd7f76; --blue:#7bafe9;
  --gray:#55555f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:16px 20px 8px}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--dim);font-size:12px}
.bar{height:8px;border-radius:4px;background:var(--panel2);overflow:hidden;
  display:flex;margin:10px 0 4px}
.bar div{height:100%}
.counts{color:var(--dim);font-size:12px;display:flex;gap:14px;flex-wrap:wrap}
.counts b{color:var(--text)}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  padding:8px 20px}
.tabs,.chips{display:flex;gap:6px;flex-wrap:wrap}
button{font:inherit;border:1px solid var(--line);background:var(--panel);
  color:var(--text);border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover{background:var(--panel2)}
button.on{background:var(--accent);border-color:var(--accent);color:#0e1116}
/* House standards: pushed to the far end of the tab row and outlined rather
   than filled, so they read as a permanent reference shelf and not as another
   bucket of work waiting to be done. */
.tabs .tabgap{flex:1 1 auto;min-width:12px}
button.std{background:transparent;border-style:dashed;
  border-color:var(--line);color:var(--dim);letter-spacing:.02em}
button.std::before{content:"\\2630\\00a0\\00a0";opacity:.7}
button.std:hover{background:var(--panel2);color:var(--text)}
button.std.on{background:transparent;color:var(--accent);
  border-color:var(--accent);border-style:solid}
button.primary{background:var(--accent);border-color:var(--accent);
  color:#0e1116;font-weight:600}
button:disabled{opacity:.45;cursor:default}
input[type=search]{font:inherit;background:var(--panel);color:var(--text);
  border:1px solid var(--line);border-radius:6px;padding:5px 10px;width:220px}
main{display:grid;grid-template-columns:minmax(280px,360px) 1fr;gap:14px;
  padding:0 20px 20px;align-items:start}
#list{border:1px solid var(--line);border-radius:8px;background:var(--panel);
  max-height:calc(100vh - 210px);overflow-y:auto;padding:4px}
.row{display:flex;gap:8px;align-items:center;padding:5px 8px;border-radius:6px;
  cursor:pointer;color:var(--dim);font-size:13px}
.row:hover{background:var(--panel2)}
.row.sel{background:var(--panel2);color:var(--text)}
.dot{width:8px;height:8px;border-radius:2px;flex:none}
.row span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#detail{border:1px solid var(--line);border-radius:8px;background:var(--panel);
  max-height:calc(100vh - 210px);overflow-y:auto}
.dhead{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;
  gap:10px;align-items:center;justify-content:space-between;
  position:sticky;top:0;background:var(--panel);z-index:2}
.dhead b{font-size:13px}
.dbody{padding:14px;display:flex;flex-direction:column;gap:12px}
.pills{display:flex;gap:6px;flex-wrap:wrap}
.pill{border:1px solid var(--line);border-radius:999px;padding:2px 10px;
  font-size:12px;color:var(--dim)}
pre{background:var(--panel2);border-radius:6px;padding:12px;margin:0;
  font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
  white-space:pre-wrap;max-height:280px;overflow-y:auto}
.qc{border-left:3px solid var(--blue);background:var(--panel2);
  padding:8px 12px;border-radius:0 6px 6px 0;font-size:12.5px;color:var(--dim);
  white-space:pre-wrap}
.lbl{font-size:12px;font-weight:600;color:var(--dim);text-transform:uppercase;
  letter-spacing:.04em}
textarea{font:inherit;background:var(--panel2);color:var(--text);width:100%;
  border:1px solid var(--line);border-radius:6px;padding:8px;resize:vertical;
  min-height:60px}
textarea.src{font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
  min-height:88px}
.field{display:flex;flex-direction:column;gap:4px}
.field .flbl{font-size:12px;color:var(--dim)}
.field.shared .flbl{color:var(--orange)}
.btnrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#toast{position:fixed;bottom:18px;right:18px;background:var(--panel2);
  border:1px solid var(--line);border-radius:8px;padding:10px 16px;
  font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none;
  max-width:420px}
#toast.show{opacity:1}
.empty{color:var(--faint);padding:14px}
hr{border:0;border-top:1px solid var(--line);margin:2px 0}
.note-status{font-size:11px;color:var(--faint)}
.chg{color:var(--green);font-size:10px;flex:none;margin-left:auto;
  text-transform:uppercase;letter-spacing:.05em}
.warn{color:var(--orange);font-weight:400;text-transform:none;
  letter-spacing:0}
.ok{color:var(--green);font-weight:400;text-transform:none;letter-spacing:0}
.hintbox{border-left:2px solid var(--blue);background:#12161d;
  padding:8px 10px;margin:0 0 8px;font-size:12px;color:var(--faint);
  line-height:1.5;border-radius:0 4px 4px 0}
.hintbox b{color:var(--fg)}
.diffbox{border:1px solid var(--line);border-radius:4px;padding:8px 10px;
  margin-bottom:8px;font-size:12px;line-height:1.55;background:#0f1319}
.diffbox s{color:var(--orange);opacity:.75}
.keybar{border:1px solid var(--orange);border-radius:6px;padding:10px 12px;
  margin-top:10px;font-size:12px;color:var(--faint);line-height:1.5}
.keybar b{color:var(--orange)}
.keybar input{flex:1;min-width:260px;background:#0f1319;color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:6px 9px;
  font-family:inherit;font-size:12px}
.diffbox em{color:var(--green);font-style:normal}
.stale{color:var(--orange);font-size:10px;flex:none;margin-left:auto;
  text-transform:uppercase;letter-spacing:.05em}
.vpill{border:1px solid var(--line);border-radius:20px;padding:2px 8px;
  font-size:11px;color:var(--faint)}
.stalebox{border:1px solid var(--orange);border-radius:6px;padding:8px 10px;
  margin:0 0 10px;font-size:12px;color:var(--orange);line-height:1.5}
.projbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  padding:8px 0 2px;border-bottom:1px solid var(--line);margin-bottom:8px}
.plabel{font-size:11px;color:var(--faint);text-transform:uppercase;
  letter-spacing:.06em}
button.proj{border-radius:6px}
button.proj small{color:var(--faint);margin-left:6px}
button.proj.on small{color:#0e1116}

/* Modal. Cursor's embedded browser suppresses window.prompt/confirm without
   raising an error, so the app has to own its own dialog — otherwise buttons
   like "+ New project" silently do nothing. Kept dead simple: fixed overlay,
   centred card, close on backdrop click. */
.modal-back{position:fixed;inset:0;background:rgba(6,6,10,.72);
  display:flex;align-items:flex-start;justify-content:center;padding:48px 20px;
  z-index:50;overflow-y:auto}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  width:min(680px,100%);box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal h2{margin:0;padding:14px 18px;border-bottom:1px solid var(--line);
  font-size:15px}
.modal .mbody{padding:16px 18px;display:flex;flex-direction:column;gap:14px}
.modal .mfoot{padding:12px 18px;border-top:1px solid var(--line);
  display:flex;gap:8px;justify-content:flex-end}
.modal label{font-size:12px;color:var(--dim);display:flex;flex-direction:column;
  gap:4px}
.modal input[type=text]{font:inherit;background:var(--panel2);color:var(--text);
  border:1px solid var(--line);border-radius:6px;padding:7px 10px}
.modal textarea{min-height:180px}
.modal .radiogrp{display:flex;flex-direction:column;gap:6px;color:var(--text);
  font-size:13px}
.modal .radiogrp label{flex-direction:row;gap:8px;align-items:flex-start;
  color:var(--text);cursor:pointer}
.modal .radiogrp .rhint{color:var(--faint);font-size:12px;display:block;
  margin-top:2px}
.modal .filerow{display:flex;gap:8px;align-items:center;font-size:12px;
  color:var(--dim)}
.modal .filerow input[type=file]{color:var(--dim)}

/* Ingest proposal panel — mirrors the note-apply diff cards visually so it
   reads as "review this before it lands", not as a new UI concept. */
.igrp{border:1px solid var(--line);border-radius:8px;padding:10px 12px;
  background:var(--panel2);margin-bottom:10px}
.igrp>h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;
  letter-spacing:.05em;color:var(--dim)}
.iitem{border:1px solid var(--line);border-radius:6px;padding:8px 10px;
  background:var(--panel);margin-bottom:6px;display:flex;gap:10px;
  align-items:flex-start}
.iitem.rej{opacity:.45}
.iitem .icheck{margin-top:4px}
.iitem .ibody{flex:1;min-width:0}
.iitem .ihead{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;
  margin-bottom:4px}
.iitem .iname{font-weight:600;font-size:13px}
.iitem .iid{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--faint)}
.iitem .igrd{font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  padding:1px 6px;border-radius:999px;border:1px solid var(--line);
  color:var(--dim)}
.iitem .igrd.stated{color:var(--green);border-color:var(--green)}
.iitem .igrd.inferred{color:var(--orange);border-color:var(--orange)}
.iitem .iquote{font-size:12px;color:var(--faint);border-left:2px solid var(--line);
  padding:2px 8px;margin:4px 0 6px}
.iitem .idraft{font-size:12.5px;line-height:1.55;color:var(--dim);
  white-space:pre-wrap}
</style>
</head>
<body>
<header>
  <h1>Show Bible Tracker <span class="sub" id="projTitle"></span></h1>
  <div class="projbar">
    <span class="plabel">Project</span>
    <div class="chips" id="projects"></div>
    <button id="newProjBtn">+ New project</button>
  </div>
  <div class="sub" id="meta"></div>
  <div class="bar" id="bar"></div>
  <div class="counts" id="counts"></div>
  <div id="keybar"></div>
</header>
<div class="toolbar">
  <div class="tabs" id="tabs"></div>
</div>
<div class="toolbar">
  <div class="chips" id="chips"></div>
  <input type="search" id="search" placeholder="Search cards…">
  <span style="flex:1"></span>
  <button id="briefBtn">Copy agent brief</button>
  <button id="ingestBtn" style="display:none"></button>
  <button id="regenBtn">Regenerate all</button>
</div>
<main>
  <div id="list"></div>
  <div id="detail"><div class="empty">Select a card.</div></div>
</main>
<div id="toast"></div>
<script>
const STATUSES = [
  {id:"pending",   label:"Pending",        color:"var(--gray)"},
  {id:"generated", label:"Generated",      color:"var(--blue)"},
  {id:"approved",  label:"Approved",       color:"var(--green)"},
  {id:"revise",    label:"Needs revision", color:"var(--orange)"},
];
let cards = [], reviews = {}, generated = "";
let projects = [], activeProject = "", projectTitle = "", keyOk = false;
let hasStory = false, hasProposal = false;
let section = "All";
let statusFilter = "all";
let query = "";
let selectedKey = "";
let sourceFields = [];
let noteTimer = null;
let lastChanged = new Set();   // card keys updated by the last regeneration

// Per-project view preferences, so switching projects restores where you were.
const pref = (k) => localStorage.getItem(`sbt.${activeProject}.${k}`);
const setPref = (k, v) => localStorage.setItem(`sbt.${activeProject}.${k}`, v);

function adoptPayload(j){
  projects = j.projects || [];
  activeProject = j.active;
  projectTitle = j.title || j.active;
  keyOk = !!j.key_ok;
  cards = j.cards || [];
  reviews = j.reviews || {};
  generated = j.generated;
  hasStory = !!j.has_story;
  hasProposal = !!j.has_proposal;
  section = pref("section") || "All";
  statusFilter = pref("status") || "all";
  selectedKey = pref("selected") || (cards[0] ? cards[0].key : "");
  lastChanged = new Set();
}

function renderIngestButton(){
  const b = $("ingestBtn");
  if (hasProposal){
    b.style.display = ""; b.textContent = "Review ingest proposal";
    b.className = "primary";
  } else if (hasStory){
    b.style.display = ""; b.textContent = "Ingest story";
    b.className = "";
  } else {
    b.style.display = "none"; b.className = "";
  }
}

function diffAndReport(newCards, prefix){
  const before = new Map(cards.map(c => [c.key, c.prompt + "\u0000" + c.qc]));
  cards = newCards;
  lastChanged = new Set(cards
    .filter(c => before.get(c.key) !== c.prompt + "\u0000" + c.qc)
    .map(c => c.key));
  const nums = cards.filter(c => lastChanged.has(c.key)).map(c => c.num);
  const list = nums.slice(0, 10).join(", ") +
    (nums.length > 10 ? ` +${nums.length - 10} more` : "");
  toast(nums.length
    ? `${prefix} — ${nums.length} card(s) updated: ${list}`
    : `${prefix} — no card text changed`);
}

const $ = (id) => document.getElementById(id);
const esc = (s) => s.replace(/[&<>"]/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function toast(msg, ok=true){
  const t = $("toast");
  t.textContent = msg;
  t.style.borderColor = ok ? "var(--line)" : "var(--orange)";
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 7000);
}

async function copyText(t){
  // clipboard API rejects when the document is not focused; fall back so a
  // copy button never silently does nothing
  try { await navigator.clipboard.writeText(t); return true; }
  catch(e){
    const ta = document.createElement("textarea");
    ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch(_){}
    document.body.removeChild(ta);
    return ok;
  }
}

async function api(path, opts){
  // every request is scoped to the active project
  if (opts && opts.body){
    const b = JSON.parse(opts.body);
    if (b.project === undefined) b.project = activeProject;
    opts = {...opts, body: JSON.stringify(b)};
  } else if (activeProject) {
    path += (path.includes("?") ? "&" : "?") +
            "project=" + encodeURIComponent(activeProject);
  }
  const r = await fetch(path, opts);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}

function review(key){ return reviews[key] || {status:"pending", note:""}; }

function isStale(c){
  // verdict was recorded against an older prompt version
  const rv = review(c.key);
  return !!rv.at_version && !!c.version && rv.at_version !== c.version;
}

function counts(){
  const c = {pending:0, generated:0, approved:0, revise:0, stale:0};
  for (const card of cards){
    c[review(card.key).status] += 1;
    if (isStale(card)) c.stale += 1;
  }
  return c;
}

function sections(){
  const s = [];
  for (const c of cards) if (!s.includes(c.section)) s.push(c.section);
  return s;
}

function visibleCards(){
  const q = query.trim().toLowerCase();
  return cards.filter(c =>
    (section === "All" || c.section === section) &&
    (statusFilter === "all" ? true
       : statusFilter === "stale" ? isStale(c)
       : review(c.key).status === statusFilter) &&
    (q === "" || (c.num + " " + c.title).toLowerCase().includes(q)));
}

function statusColor(s){
  return (STATUSES.find(x => x.id === s) || STATUSES[0]).color;
}

function renderProjects(){
  $("projTitle").textContent = "— " + projectTitle;
  document.title = "Show Bible Tracker — " + projectTitle;
  $("projects").innerHTML = projects.map(p =>
    `<button class="proj ${p.slug===activeProject?"on":""}" ` +
    `data-p="${esc(p.slug)}" title="${esc(p.subtitle||p.slug)}">` +
    `${esc(p.title)}<small>${p.cards}</small></button>`).join("");
  $("projects").querySelectorAll("button").forEach(b =>
    b.onclick = () => switchProject(b.dataset.p));
}

function renderKeybar(){
  const el = $("keybar");
  if (keyOk){ el.innerHTML = ""; return; }
  el.innerHTML =
    `<div class="keybar"><b>Claude key needed</b> to apply notes ` +
    `automatically. Paste it here and it is written to ` +
    `<code>production/generator/.env</code> (chmod 600) and verified.` +
    `<div class="btnrow"><input id="keyIn" type="password" ` +
    `placeholder="sk-ant-…" autocomplete="off" spellcheck="false">` +
    `<button class="primary" id="keySave">Save key</button></div></div>`;
  $("keySave").onclick = async () => {
    const v = $("keyIn").value.trim();
    if (!v){ toast("Paste the key first", false); return; }
    const b = $("keySave"); b.disabled = true; b.textContent = "Verifying…";
    try {
      const j = await api("/api/key", {method:"POST",
        body: JSON.stringify({key: v})});
      keyOk = true; $("keyIn").value = "";
      renderKeybar();
      toast("Key verified — using " + j.model);
    } catch(e){
      toast("Key rejected: " + e.message, false);
      b.disabled = false; b.textContent = "Save key";
    }
  };
}

function renderHeader(){
  const c = counts(), total = cards.length;
  const pct = (n) => total ? 100*n/total : 0;
  $("meta").textContent = total
    ? `${total} virgin prompt cards · regenerated ${generated} from ` +
      `projects/${activeProject}/matrix_data.json`
    : `Empty project — populate projects/${activeProject}/matrix_data.json, ` +
      `then hit Regenerate all.`;
  $("bar").innerHTML =
    `<div style="width:${pct(c.approved)}%;background:var(--green)"></div>` +
    `<div style="width:${pct(c.generated)}%;background:var(--blue)"></div>` +
    `<div style="width:${pct(c.revise)}%;background:var(--orange)"></div>`;
  $("counts").innerHTML =
    `<span><b>${c.approved}</b> approved</span>` +
    `<span><b>${c.generated}</b> generated</span>` +
    `<span><b>${c.revise}</b> needs revision</span>` +
    `<span><b>${c.pending}</b> pending</span>`;
}

async function switchProject(p){
  if (p === activeProject) return;
  try {
    const j = await api("/api/project", {method:"POST",
      body: JSON.stringify({project: p})});
    adoptPayload(j);
    $("search").value = ""; query = "";
    render();
    toast(`Switched to ${projectTitle} — ${cards.length} card(s)`);
  } catch(e){ toast("Switch failed: " + e.message, false); }
}

// -------------------------------------------------------------- modal helper
// Cursor's embedded browser silently drops window.prompt/confirm, so anything
// interactive has to live in a real in-page dialog. openModal accepts a
// fragment builder and returns {close}, so callers can drive their own state.
function openModal({title, build, wide=false}){
  const back = document.createElement("div"); back.className = "modal-back";
  const m = document.createElement("div"); m.className = "modal";
  if (wide) m.style.maxWidth = "820px";
  m.innerHTML = `<h2>${esc(title)}</h2><div class="mbody"></div>` +
                `<div class="mfoot"></div>`;
  back.appendChild(m);
  const close = () => back.remove();
  back.onclick = (e) => { if (e.target === back) close(); };
  document.addEventListener("keydown", function onEsc(e){
    if (e.key === "Escape"){ close();
      document.removeEventListener("keydown", onEsc); }
  });
  document.body.appendChild(back);
  build(m.querySelector(".mbody"), m.querySelector(".mfoot"), close);
  return {close};
}

$("newProjBtn").onclick = () => openNewProjectModal();

function openNewProjectModal(){
  openModal({
    title: "New project",
    build: (body, foot, close) => {
      body.innerHTML =
        `<label>Project name` +
        `<input type="text" id="npName" placeholder="e.g. Exposure" autofocus>` +
        `</label>` +
        `<label>House doctrine — era lock, camera package, studio looks` +
        `<div class="radiogrp">` +
          `<label><input type="radio" name="npDoc" value="copy" checked>` +
          `<span>Copy from ${esc(projectTitle)}` +
          `<span class="rhint">Inherit the same period, camera package and ` +
          `look — matches the current project's aesthetic.</span></span>` +
          `</label>` +
          `<label><input type="radio" name="npDoc" value="default">` +
          `<span>Start from house defaults` +
          `<span class="rhint">Blank doctrine — set era, camera and look ` +
          `later in <code>project.json</code>.</span></span></label>` +
        `</div></label>` +
        `<label>Story (optional) — Claude drafts cast, locations, props and ` +
        `factions from this text into a proposal you review before it lands.` +
        `<textarea id="npStory" placeholder="Paste a treatment, novella, ` +
        `short story or synopsis. Leave empty to start with a blank matrix."` +
        `></textarea></label>` +
        `<div class="filerow">…or load from a file: ` +
        `<input type="file" id="npFile" accept=".md,.txt,.markdown,text/*">` +
        `<span id="npFileMeta"></span></div>`;
      body.querySelector("#npFile").onchange = (e) => {
        const f = e.target.files && e.target.files[0]; if (!f) return;
        const meta = body.querySelector("#npFileMeta");
        meta.textContent = "reading…";
        const r = new FileReader();
        r.onload = () => {
          body.querySelector("#npStory").value = r.result || "";
          meta.textContent =
            `${f.name} · ${Math.round((r.result||"").length/1024)} kB`;
        };
        r.onerror = () => meta.textContent = "read failed";
        r.readAsText(f);
      };
      foot.innerHTML =
        `<button id="npCancel">Cancel</button>` +
        `<button class="primary" id="npCreate">Create</button>`;
      foot.querySelector("#npCancel").onclick = close;
      foot.querySelector("#npCreate").onclick = async () => {
        const title = body.querySelector("#npName").value.trim();
        const story = body.querySelector("#npStory").value;
        const doc = body.querySelector(
          "input[name=npDoc]:checked").value;
        if (!title){ toast("Give it a name first", false);
          body.querySelector("#npName").focus(); return; }
        const btn = foot.querySelector("#npCreate");
        btn.disabled = true; btn.textContent = "Creating…";
        try {
          const j = await api("/api/project/new", {method:"POST",
            body: JSON.stringify({title,
              copy_doctrine: doc === "copy" ? activeProject : null,
              story: story.trim() ? story : null,
              project: null})});
          adoptPayload(j);
          render();
          close();
          if (story.trim()){
            // Kick straight into extraction — the story is why they filled
            // that box, and making them click a second button to see the
            // proposal would be pointless friction.
            openIngestModal();
          } else {
            toast(`Created "${projectTitle}" — populate ` +
                  `projects/${activeProject}/matrix_data.json, ` +
                  `then Regenerate all`);
          }
        } catch(e){
          toast("Create failed: " + e.message, false);
          btn.disabled = false; btn.textContent = "Create";
        }
      };
    }
  });
}

// -------------------------------------------------------------- ingest modal
const INGEST_COLLECTIONS = [
  ["lighting",   "Lighting"],
  ["characters", "Characters"],
  ["factions",   "Factions"],
  ["props",      "Props"],
  ["locations",  "Locations"],
];

function ingestItemSummary(coll, item){
  // One-line summary shown under the id. Keep it short — the operator is
  // scanning for wrongness, not reading the item.
  if (coll === "lighting")   return item.desc || "";
  if (coll === "characters") return (item.base || "").slice(0, 240);
  if (coll === "factions")   return (item.base || "").slice(0, 240);
  if (coll === "props")      return (item.base || "").slice(0, 240);
  if (coll === "locations")  return (item.anchor || "").slice(0, 240);
  return "";
}

function ingestItemDetail(coll, item){
  // The full drafted text, shown as a `<details>` so operators can drill in
  // when the summary isn't enough. Every collection has different fields, so
  // this stays a switch rather than a generic dump.
  const parts = [];
  if (coll === "characters"){
    parts.push(`<div class="lbl">BASE</div><div class="idraft">${esc(item.base||"")}</div>`);
    const wd = item.wardrobe || {};
    parts.push(`<div class="lbl">WARDROBE</div>`);
    for (const k of ["order","head","torso","hands","legs","feet","carried","closing"]){
      parts.push(`<div class="idraft"><b>${k}:</b> ${esc(wd[k]||"")}</div>`);
    }
    if ((item.states||[]).length){
      parts.push(`<div class="lbl">STATES</div>`);
      for (const s of item.states){
        parts.push(`<div class="idraft"><b>${esc(s.name||s.id||"?")}</b> — ${esc(s.delta||"")}</div>`);
      }
    }
  } else if (coll === "factions"){
    parts.push(`<div class="lbl">BASE</div><div class="idraft">${esc(item.base||"")}</div>`);
    parts.push(`<div class="lbl">WARDROBE</div><div class="idraft">${esc(item.wardrobe||"")}</div>`);
    if ((item.states||[]).length){
      parts.push(`<div class="lbl">STATES</div>`);
      for (const s of item.states){
        parts.push(`<div class="idraft"><b>${esc(s.name||"?")}</b> — ${esc(s.delta||"")}</div>`);
      }
    }
  } else if (coll === "props"){
    parts.push(`<div class="lbl">BASE</div><div class="idraft">${esc(item.base||"")}</div>`);
    if ((item.states||[]).length){
      parts.push(`<div class="lbl">STATES</div>`);
      for (const s of item.states){
        parts.push(`<div class="idraft"><b>${esc(s.name||"?")}</b> — ${esc(s.delta||"")}</div>`);
      }
    }
  } else if (coll === "locations"){
    parts.push(`<div class="lbl">ANCHOR</div><div class="idraft">${esc(item.anchor||"")}</div>`);
    if (item.needs_reverse){
      parts.push(`<div class="idraft"><b>reverse angle:</b> yes</div>`);
    }
    if ((item.states||[]).length){
      parts.push(`<div class="lbl">STATES</div>`);
      for (const s of item.states){
        parts.push(`<div class="idraft"><b>${esc(s.name||"?")}</b> — ${esc(s.delta||"")}</div>`);
      }
    }
  } else if (coll === "lighting"){
    parts.push(`<div class="idraft">${esc(item.desc||"")}</div>`);
  }
  if ((item._issues||[]).length){
    parts.push(`<div class="lbl warn">SCHEMA ISSUES (fix in the matrix after applying)</div>`);
    parts.push(`<div class="idraft">${item._issues.map(esc).join("<br>")}</div>`);
  }
  return parts.join("");
}

async function openIngestModal(){
  const build = (body, foot, close) => {
    body.innerHTML = `<div class="empty">Loading proposal…</div>`;
    foot.innerHTML = `<button id="igClose">Close</button>`;
    foot.querySelector("#igClose").onclick = close;
    return { setBody: (h) => { body.innerHTML = h; },
             setFoot: (h) => { foot.innerHTML = h; } };
  };
  let handles;
  openModal({title: "Ingest story into matrix",
    wide: true,
    build: (b, f, close) => { handles = build(b, f, close); handles._close = close; }});

  // Step 1: fetch or generate a proposal. If one is already staged on disk
  // we show that instead of spending a Claude call.
  let doc;
  try {
    if (hasProposal){
      doc = await api("/api/ingest");
      if (!doc.proposal) doc = null;
    }
    if (!doc){
      if (!keyOk){
        handles.setBody(
          `<div class="empty">A Claude key is needed to draft a proposal. ` +
          `Paste one at the top of the page, then reopen this dialog.</div>`);
        return;
      }
      handles.setBody(
        `<div class="empty">Asking Claude to draft the identity layer… ` +
        `this takes about 30 seconds for a short story.</div>`);
      const j = await api("/api/ingest", {method:"POST", body:"{}"});
      hasProposal = true;
      doc = {proposal: j.proposal, stats: j.stats, model: j.model};
      // Adopt any updated payload fields (has_story, etc.) so the header
      // button refreshes if the modal is closed and re-opened.
      adoptPayload(j);
      renderIngestButton();
    }
  } catch(e){
    handles.setBody(`<div class="empty">Draft failed: ${esc(e.message)}</div>`);
    return;
  }

  // Step 2: render the review panel. Every item defaults to accepted; the
  // operator's job is to uncheck the wrong ones, which is faster.
  const proposal = doc.proposal || {};
  const totals = INGEST_COLLECTIONS.reduce(
    (s, [k]) => s + (proposal[k]||[]).length, 0);
  const accepted = new Set();
  for (const [k] of INGEST_COLLECTIONS){
    for (const it of (proposal[k]||[])) accepted.add(it.id);
  }

  const paint = () => {
    const groups = INGEST_COLLECTIONS.map(([coll, label]) => {
      const items = proposal[coll] || [];
      if (!items.length) return "";
      const rows = items.map(it => {
        const on = accepted.has(it.id);
        const grd = (it.grounding === "stated") ? "stated" : "inferred";
        const quote = it.source_quote
          ? `<div class="iquote">${esc(it.source_quote)}</div>`
          : `<div class="iquote">(no source quote — Claude inferred this)</div>`;
        return `<div class="iitem${on?"":" rej"}">` +
          `<input class="icheck" type="checkbox" data-id="${esc(it.id)}"` +
          `${on?" checked":""}>` +
          `<div class="ibody">` +
            `<div class="ihead">` +
              `<span class="iname">${esc(it.name||it.id)}</span>` +
              `<span class="iid">${esc(it.id)}</span>` +
              `<span class="igrd ${grd}">${grd}</span>` +
            `</div>` +
            `${quote}` +
            `<div class="idraft">${esc(ingestItemSummary(coll, it))}</div>` +
            `<details style="margin-top:6px"><summary style="cursor:pointer;` +
            `color:var(--dim);font-size:12px">Full draft</summary>` +
            `<div style="margin-top:6px;display:flex;flex-direction:column;` +
            `gap:6px">${ingestItemDetail(coll, it)}</div></details>` +
          `</div></div>`;
      }).join("");
      const n = items.length;
      const on = items.filter(i => accepted.has(i.id)).length;
      return `<div class="igrp"><h4>${esc(label)} — ${on}/${n} accepted ` +
        `<button data-selall="${coll}" style="margin-left:8px;font-size:11px">All</button> ` +
        `<button data-selnone="${coll}" style="font-size:11px">None</button>` +
        `</h4>${rows}</div>`;
    }).join("");
    handles.setBody(
      `<div class="sub" style="color:var(--dim);font-size:12px;` +
      `margin-bottom:8px">Model: ${esc(doc.model||"—")} · ` +
      `${totals} item(s) drafted. Tick the ones to merge into ` +
      `<code>matrix_data.json</code>. Nothing is written until you click ` +
      `Apply.</div>${groups || '<div class="empty">Empty proposal.</div>'}`);
    // Wire per-item checkboxes and select-all/none buttons.
    handles._body().querySelectorAll(".icheck").forEach(cb => {
      cb.onchange = () => {
        if (cb.checked) accepted.add(cb.dataset.id);
        else accepted.delete(cb.dataset.id);
        cb.closest(".iitem").classList.toggle("rej", !cb.checked);
        // Refresh only the group counts to avoid full repaint jitter.
        handles._body().querySelectorAll(".igrp>h4").forEach(h => {
          const all = h.parentElement.querySelectorAll(".icheck");
          const on  = h.parentElement.querySelectorAll(".icheck:checked");
          h.firstChild.nodeValue = h.firstChild.nodeValue.replace(
            /—\s*\d+\/\d+ accepted/, `— ${on.length}/${all.length} accepted`);
        });
      };
    });
    handles._body().querySelectorAll("[data-selall]").forEach(btn => {
      btn.onclick = () => {
        for (const it of (proposal[btn.dataset.selall]||[])) accepted.add(it.id);
        paint();
      };
    });
    handles._body().querySelectorAll("[data-selnone]").forEach(btn => {
      btn.onclick = () => {
        for (const it of (proposal[btn.dataset.selnone]||[])) accepted.delete(it.id);
        paint();
      };
    });
  };
  // Give handles a body() accessor since setBody replaces .innerHTML and
  // the modal-back element itself is transient.
  handles._body = () => document.querySelector(".modal-back .mbody");
  paint();

  handles.setFoot(
    `<button id="igDiscard">Discard proposal</button>` +
    `<span style="flex:1"></span>` +
    `<button id="igCancel">Cancel</button>` +
    `<button class="primary" id="igApply">Apply accepted</button>`);
  document.getElementById("igCancel").onclick = handles._close;
  document.getElementById("igDiscard").onclick = async () => {
    if (!confirm("Discard this proposal? The story stays on disk; you " +
      "can re-run ingest later.")) return;
    try {
      const j = await api("/api/ingest/discard", {method:"POST", body:"{}"});
      adoptPayload(j); renderIngestButton();
      handles._close(); render();
      toast("Proposal discarded.");
    } catch(e){ toast("Discard failed: " + e.message, false); }
  };
  document.getElementById("igApply").onclick = async () => {
    if (accepted.size === 0){
      toast("Nothing ticked to apply", false); return;
    }
    const btn = document.getElementById("igApply");
    btn.disabled = true; btn.textContent = "Applying…";
    try {
      const j = await api("/api/ingest/apply", {method:"POST",
        body: JSON.stringify({accepted_ids: [...accepted]})});
      const c = j.counts || {};
      const total = Object.values(c).reduce((s,n) => s+n, 0);
      adoptPayload(j);
      diffAndReport(j.cards, `Ingested ${total} item(s) from story`);
      handles._close(); render();
    } catch(e){
      toast("Apply failed: " + e.message, false);
      btn.disabled = false; btn.textContent = "Apply accepted";
    }
  };
}

function renderTabs(){
  // A section of reference cards is a fixed house standard, not a queue of
  // work. It is always there and never gets worked through, so it sits apart
  // from the production tabs and carries no progress count — otherwise it
  // reads as one more category of things left to make.
  const isStd = s => {
    const inSec = cards.filter(c => c.section === s);
    return inSec.length > 0 && inSec.every(c => c.kind === "reference");
  };
  const all = sections();
  const work = all.filter(s => !isStd(s));
  const std  = all.filter(isStd);

  const tab = (s, extra, label) =>
    `<button data-s="${esc(s)}" class="${s===section?"on":""}${extra}">` +
    `${esc(label)}</button>`;

  let html = ["All", ...work].map(s => {
    const n = s === "All" ? cards.filter(c => c.kind !== "reference").length
      : cards.filter(c => c.section === s).length;
    return tab(s, "", `${s} (${n})`);
  }).join("");
  if (std.length){
    html += `<span class="tabgap"></span>`;
    html += std.map(s => tab(s, " std", s)).join("");
  }
  $("tabs").innerHTML = html;
  $("tabs").querySelectorAll("button").forEach(b =>
    b.onclick = () => { section = b.dataset.s;
      setPref("section", section); render(); });

  const c = counts();
  const chips = [{id:"all", label:`All statuses (${cards.length})`},
    ...STATUSES.map(s => ({id:s.id, label:`${s.label} (${c[s.id]})`})),
    {id:"stale", label:`Stale (${c.stale})`}];
  $("chips").innerHTML = chips.map(ch =>
    `<button data-s="${ch.id}" class="${ch.id===statusFilter?"on":""}">` +
    `${ch.label}</button>`).join("");
  $("chips").querySelectorAll("button").forEach(b =>
    b.onclick = () => { statusFilter = b.dataset.s;
      setPref("status", statusFilter); render(); });
}

function renderList(){
  const vis = visibleCards();
  if (!vis.some(c => c.key === selectedKey) && vis.length)
    selectedKey = vis[0].key;
  $("list").innerHTML = vis.length ? vis.map(c =>
    `<div class="row ${c.key===selectedKey?"sel":""}" data-k="${esc(c.key)}">` +
    `<div class="dot" style="background:${statusColor(review(c.key).status)}"></div>` +
    `<span>${c.num} · ${esc(c.title)}</span>` +
    (isStale(c) ? `<b class="stale">stale v${review(c.key).at_version}` +
       `\u2192v${c.version}</b>` : ``) +
    (lastChanged.has(c.key) ? `<b class="chg">updated</b>` : ``) +
    `</div>`).join("")
    : (cards.length
        ? `<div class="empty">No cards match the filters.</div>`
        : `<div class="empty">This project has no cards yet.</div>`);
  $("list").querySelectorAll(".row").forEach(r =>
    r.onclick = () => selectCard(r.dataset.k));
}

async function selectCard(key){
  selectedKey = key;
  setPref("selected", key);
  renderList();
  await renderDetail();
}

async function renderDetail(){
  const c = cards.find(x => x.key === selectedKey);
  if (!c){
    $("detail").innerHTML = cards.length
      ? `<div class="empty">Select a card.</div>`
      : `<div class="empty"><b>${esc(projectTitle)}</b> is empty.<br><br>` +
        `Populate <code>projects/${esc(activeProject)}/matrix_data.json</code> ` +
        `with characters, locations, props and the rest (an agent can do this ` +
        `from a script or novella), then click <b>Regenerate all</b>. ` +
        `House doctrine — era lock, camera package, studio looks, section ` +
        `order — lives in <code>projects/${esc(activeProject)}/project.json</code>.` +
        `</div>`;
    return; }
  const rv = review(c.key);
  const stale = isStale(c);
  let html = `
  <div class="dhead"><b>${c.num} — ${esc(c.title)}</b>
    <span class="vpill" title="Prompt version, last changed ${esc(c.updated||"")}">v${c.version||1}</span>
    <span class="pill" style="border-color:${statusColor(rv.status)};
      color:${statusColor(rv.status)}">${esc(rv.status)}</span></div>
  <div class="dbody">` +
    (stale ? `<div class="stalebox"><b>Prompt changed since you marked this
      ${esc(rv.status)}.</b> You worked from <b>v${rv.at_version}</b>; the
      card is now <b>v${c.version}</b> (changed ${esc(c.updated||"")}).
      Re-run it, or set the status again to accept the current version.</div>`
      : ``) + `
    <div class="pills">
      ${(c.specPills||[]).map(p=>`<span class="pill">${esc(p)}</span>`).join("")}
    </div>
    <pre id="promptPre">${esc(c.prompt)}</pre>
    <div class="btnrow">
      <button class="primary" id="copyP">Copy prompt</button>
      <button id="copyPQ">Copy prompt + QC</button>` +
      (c.altCopy ? `<button id="copyAlt">${esc(c.altCopy.label)}</button>` : ``) + `
    </div>
    <div class="qc"><b>QC:</b> ${esc(c.qc)}</div>
    <hr>
    <div class="lbl">Status</div>
    <div class="btnrow" id="statusBtns">` +
    STATUSES.map(s =>
      `<button data-s="${s.id}" class="${rv.status===s.id?"on":""}">` +
      `${s.label}</button>`).join("") + `
    </div>
    <div class="lbl">Director notes</div>
    <div class="hintbox">Write the fix in plain language and hit
      <b>Apply note &amp; regenerate</b> — Claude edits the source fields
      below, the prompt rebuilds, and you get an undo. For a precise
      wording change, edit the <b>Source fields</b> directly instead.</div>
    <textarea id="note" placeholder="e.g. lose the work apron; he should look colder; move him closer to the lift…">${esc(rv.note||"")}</textarea>
    <div class="note-status" id="noteStatus"></div>
    <div class="btnrow">
      <button class="primary" id="applyNote">Apply note &amp; regenerate</button>
      <button id="undoBtn">Undo last apply</button>
      <button id="askAgent">Copy as chat request</button>
    </div>
    <div id="applyOut"></div>
    <hr>
    <div class="lbl">Source fields (matrix_data.json)
      <span class="ok">— editing these DOES rewrite the prompt</span></div>
    <div id="srcFields"><div class="empty">Loading…</div></div>
    <div class="btnrow">
      <button class="primary" id="saveSrc">Save &amp; regenerate</button>
      <span class="note-status">Rewrites matrix_data.json, reruns the
      generator, refreshes all outputs. Fields marked in orange are shared.</span>
    </div>
  </div>`;
  $("detail").innerHTML = html;

  $("applyNote").onclick = async () => {
    const note = $("note").value.trim();
    if (!note) { toast("Write the fix in the notes box first", false); return; }
    const b = $("applyNote");
    b.disabled = true; b.textContent = "Claude is editing…";
    $("applyOut").innerHTML = "";
    try {
      const j = await api("/api/apply_note", {method:"POST",
        body: JSON.stringify({key: c.key, note})});
      generated = j.generated; projects = j.projects || projects;
      const dx = j.diagnosis
        ? `<div class="hintbox"><b>Diagnosis:</b> ${esc(j.diagnosis)}</div>` : ``;
      if (!j.changes.length) {
        $("applyOut").innerHTML = dx +
          `<div class="hintbox"><b>No change made.</b> ${esc(j.summary)}</div>`;
        toast("Claude made no change — see the explanation", false);
      } else {
        $("applyOut").innerHTML = dx +
          `<div class="hintbox"><b>Applied:</b> ${esc(j.summary)}</div>` +
          j.changes.map(ch => `<div class="lbl">${esc(ch.label)}` +
            (ch.shared ? ` <span class="warn">(shared)</span>` : ``) + `</div>` +
            `<div class="diffbox"><s>${esc(ch.before)}</s><br><br>` +
            `<em>${esc(ch.after)}</em></div>`).join("");
        diffAndReport(j.cards, "Note applied");
        renderHeader(); renderTabs(); renderList();
      }
    } catch(e){ toast("Apply failed: " + e.message, false); }
    b.disabled = false; b.textContent = "Apply note & regenerate";
  };

  $("undoBtn").onclick = async () => {
    try {
      const j = await api("/api/undo", {method:"POST", body:"{}"});
      generated = j.generated;
      diffAndReport(j.cards, "Undone");
      $("applyOut").innerHTML = "";
      renderHeader(); renderTabs(); renderList(); await renderDetail();
    } catch(e){ toast("Undo failed: " + e.message, false); }
  };

  $("askAgent").onclick = async () => {
    const note = $("note").value.trim();
    if (!note) { toast("Leave a note first", false); return; }
    const base = `production/generator/projects/${activeProject}`;
    const req =
      `In the JESUS_IS_SKIING/FILM workspace, apply this Show Bible ` +
      `tracker note.\n\nProject: ${activeProject}\n` +
      `Card: ${c.num} — ${c.title}\nCard key: ${c.key}\n\n` +
      `Note: ${note}\n\n` +
      `Edit ${base}/matrix_data.json (never the generated files), then ` +
      `rerun python3 production/generator/generate_prompts.py ` +
      `--project ${activeProject}. Verify the change landed in this card, ` +
      `set its status back to pending in ${base}/review_state.json, and ` +
      `tell me exactly what you changed.`;
    const ok = await copyText(req);
    toast(ok ? "Fix request copied — paste it into Cursor chat"
             : "Could not copy — click the page once, then retry", ok);
  };

  if (c.altCopy) $("copyAlt").onclick = async () => {
    toast(await copyText(c.altCopy.text) ? c.altCopy.label.replace(/^Copy /,"") +
          " copied" : "Could not copy — click the page once, then retry");
  };
  $("copyP").onclick = async () => {
    toast(await copyText(c.prompt) ? "Prompt copied"
      : "Could not copy — click the page once, then retry"); };
  $("copyPQ").onclick = async () => {
    toast(await copyText(c.prompt + "\n\nQC: " + c.qc)
      ? "Prompt + QC copied"
      : "Could not copy — click the page once, then retry"); };
  $("statusBtns").querySelectorAll("button").forEach(b =>
    b.onclick = () => setStatus(c.key, b.dataset.s));
  $("note").oninput = () => {
    clearTimeout(noteTimer);
    $("noteStatus").textContent = "…";
    noteTimer = setTimeout(() => saveNote(c.key, $("note").value), 600);
  };

  try {
    const j = await api("/api/source?key=" + encodeURIComponent(c.key));
    sourceFields = j.fields;
    $("srcFields").innerHTML = sourceFields.length ? sourceFields.map((f,i) =>
      `<div class="field ${f.shared?"shared":""}">
         <div class="flbl">${esc(f.label)}</div>
         <textarea class="src" data-i="${i}">${esc(f.value)}</textarea>
       </div>`).join("")
      : `<div class="empty">No editable source fields resolved for this card.</div>`;
  } catch(e){
    $("srcFields").innerHTML =
      `<div class="empty">Could not load source fields: ${esc(e.message)}</div>`;
  }
  $("saveSrc").onclick = saveSource;
}

async function setStatus(key, status){
  reviews[key] = {...review(key), status};
  renderHeader(); renderTabs(); renderList(); renderDetail();
  try { await api("/api/review", {method:"POST",
    body: JSON.stringify({key, status})}); }
  catch(e){ toast("Save failed: " + e.message, false); }
}

async function saveNote(key, note){
  reviews[key] = {...review(key), note};
  try {
    await api("/api/review", {method:"POST", body: JSON.stringify({key, note})});
    $("noteStatus").textContent = "note saved";
  } catch(e){ $("noteStatus").textContent = "save failed"; }
}

async function saveSource(){
  const edits = [];
  document.querySelectorAll("textarea.src").forEach(t => {
    const f = sourceFields[+t.dataset.i];
    if (t.value !== f.value) edits.push({path: f.path, value: t.value});
  });
  if (!edits.length){ toast("No source changes to save"); return; }
  const btn = $("saveSrc");
  btn.disabled = true; btn.textContent = "Regenerating…";
  try {
    const j = await api("/api/source", {method:"POST",
      body: JSON.stringify({edits})});
    generated = j.generated; projects = j.projects || projects;
    diffAndReport(j.cards, `Saved ${edits.length} field(s)`);
    renderHeader(); renderTabs(); renderList(); await renderDetail();
  } catch(e){
    toast("Regeneration failed: " + e.message, false);
    btn.disabled = false; btn.textContent = "Save & regenerate";
  }
}

$("regenBtn").onclick = async () => {
  const b = $("regenBtn"); b.disabled = true; b.textContent = "Regenerating…";
  try {
    const j = await api("/api/regenerate", {method:"POST", body:"{}"});
    generated = j.generated; projects = j.projects || projects;
    diffAndReport(j.cards, "Regenerated from matrix_data.json");
    render();
  } catch(e){ toast("Regeneration failed: " + e.message, false); }
  b.disabled = false; b.textContent = "Regenerate all";
};

$("briefBtn").onclick = async () => {
  try {
    const j = await api("/api/brief", {method:"POST", body:"{}"});
    await copyText(j.text);
    toast(j.count
      ? `Brief for ${j.count} card(s) copied + written to ${j.path} — paste it to the agent in Cursor chat`
      : "No cards are marked as needing revision");
  } catch(e){ toast("Brief failed: " + e.message, false); }
};

$("search").oninput = (e) => { query = e.target.value; renderList(); };

function render(){ renderProjects(); renderKeybar(); renderHeader();
  renderIngestButton();
  renderTabs(); renderList(); renderDetail(); }

$("ingestBtn").onclick = () => openIngestModal();

(async function init(){
  adoptPayload(await api("/api/state"));
  render();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _slug(self, body_or_query):
        """Project slug from request, defaulting to the active project."""
        want = body_or_query.get("project")
        if isinstance(want, list):
            want = want[0] if want else None
        return want if want in known_slugs() else active_slug()

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            self._send(200, INDEX_HTML, "text/html")
        elif url.path == "/api/state":
            self._json(project_payload(self._slug(query)))
        elif url.path == "/api/source":
            key = (query.get("key") or [""])[0]
            data = load_json(ppath(self._slug(query), "matrix_data.json"), {})
            try:
                self._json({"fields": resolve_fields(key, data)})
            except (KeyError, IndexError, TypeError):
                self._json({"fields": []})
        elif url.path == "/api/ingest":
            sl = self._slug(query)
            doc = load_json(ppath(sl, PROPOSAL_FILENAME), None)
            self._json({"proposal": (doc or {}).get("proposal"),
                        "stats": (doc or {}).get("stats"),
                        "model": (doc or {}).get("model"),
                        "has_story": bool(load_story(sl).strip())})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad json"}, 400)
        sl = self._slug(body)

        if url.path == "/api/project":
            want = body.get("project")
            if want not in known_slugs():
                return self._json({"error": f"unknown project '{want}'"}, 400)
            set_active(want)
            return self._json(project_payload(want))

        if url.path == "/api/project/new":
            title = (body.get("title") or "").strip()
            if not title:
                return self._json({"error": "a project needs a name"}, 400)
            try:
                with _lock:
                    new_slug = create_project(
                        title,
                        copy_doctrine_from=body.get("copy_doctrine"),
                        story=body.get("story"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            set_active(new_slug)
            payload = project_payload(new_slug)
            payload["has_story"] = bool(load_story(new_slug).strip())
            return self._json(payload)

        if url.path == "/api/review":
            key = body.get("key")
            if not key:
                return self._json({"error": "key required"}, 400)
            with _lock:
                reviews = load_reviews(sl)
                rv = reviews.get(key, {"status": "pending", "note": ""})
                if "status" in body:
                    rv["status"] = body["status"]
                    # Stamp the prompt version this verdict was made against,
                    # so a later prompt edit shows up as drift instead of
                    # silently invalidating the approval.
                    if body["status"] in ("generated", "approved"):
                        cards_doc = load_json(ppath(sl, "cards.json"),
                                              {"cards": []})
                        card = next((c for c in cards_doc.get("cards", [])
                                     if c["key"] == key), None)
                        if card and card.get("version"):
                            rv["at_version"] = card["version"]
                    else:
                        rv.pop("at_version", None)
                if "note" in body:
                    rv["note"] = body["note"]
                reviews[key] = rv
                save_json(ppath(sl, "review_state.json"), reviews)
            return self._json({"ok": True})

        if url.path == "/api/source":
            edits = body.get("edits") or []
            if not edits:
                return self._json({"error": "no edits"}, 400)
            try:
                ok, msg = apply_edits(sl, edits)
            except (KeyError, IndexError, TypeError) as e:
                return self._json({"error": f"bad path: {e}"}, 400)
            if not ok:
                return self._json({"error": msg}, 500)
            return self._json({"ok": True, **project_payload(sl)})

        if url.path == "/api/key":
            try:
                model = save_key(body.get("key"))
            except RuntimeError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "model": model,
                               **project_payload(sl)})

        if url.path == "/api/apply_note":
            card_key = body.get("key")
            cards_doc = load_json(ppath(sl, "cards.json"), {"cards": []})
            card = next((c for c in cards_doc.get("cards", [])
                         if c["key"] == card_key), None)
            if card is None:
                return self._json({"error": "unknown card"}, 400)
            note = (body.get("note") or "").strip()
            if not note:
                return self._json({"error": "leave a note first"}, 400)
            try:
                with _lock:
                    data = load_json(ppath(sl, "matrix_data.json"), {})
                    fields = resolve_fields(card_key, data)
                    edits, changes, summary, diagnosis = apply_note_with_claude(
                        sl, card_key, card, note, fields)
                    if not edits:
                        return self._json({"ok": True, "changes": [],
                                           "summary": summary or
                                           "Claude made no change.",
                                           "diagnosis": diagnosis,
                                           **project_payload(sl)})
                    push_undo(sl, card_key, changes)
                    ok, msg = apply_edits(sl, edits)
                if not ok:
                    return self._json({"error": msg}, 500)
            except RuntimeError as e:
                return self._json({"error": str(e)}, 502)
            except (KeyError, IndexError, TypeError) as e:
                return self._json({"error": f"bad field path: {e}"}, 400)
            return self._json({"ok": True, "changes": changes,
                               "summary": summary, "diagnosis": diagnosis,
                               **project_payload(sl)})

        if url.path == "/api/undo":
            entry = pop_undo(sl)
            if not entry:
                return self._json({"error": "nothing to undo"}, 400)
            try:
                with _lock:
                    ok, msg = apply_edits(sl, entry["edits"])
            except (KeyError, IndexError, TypeError) as e:
                return self._json({"error": f"bad field path: {e}"}, 400)
            if not ok:
                return self._json({"error": msg}, 500)
            return self._json({"ok": True, "key": entry["key"],
                               **project_payload(sl)})

        if url.path == "/api/regenerate":
            with _lock:
                ok, msg = run_generator(sl)
            if not ok:
                return self._json({"error": msg}, 500)
            return self._json({"ok": True, **project_payload(sl)})

        if url.path == "/api/ingest":
            # Two modes:
            # 1. body["story"] is present  -> save as SOURCE_STORY.md and
            #    extract from it (lets an operator ingest into a project
            #    that was created before this feature existed).
            # 2. story is absent           -> extract from the existing
            #    SOURCE_STORY.md on disk.
            sys.path.insert(0, HERE)
            import story_ingest
            story = body.get("story")
            if story is not None:
                save_story(sl, story)
            text = load_story(sl)
            if not text.strip():
                return self._json({"error":
                    "No story to ingest. Paste one in the New project modal "
                    "or drop text into SOURCE_STORY.md first."}, 400)
            title = load_json(ppath(sl, "project.json"),
                              {}).get("title", sl)
            try:
                model = pick_model()
                proposal, stats, _raw = story_ingest.extract(
                    text, title, anthropic_request, model)
            except RuntimeError as e:
                return self._json({"error": str(e)}, 502)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            with _lock:
                save_json(ppath(sl, PROPOSAL_FILENAME),
                          {"proposal": proposal, "stats": stats,
                           "model": model, "title": title})
            return self._json({"ok": True, "proposal": proposal,
                               "stats": stats, "model": model,
                               **project_payload(sl)})

        if url.path == "/api/ingest/discard":
            path = ppath(sl, PROPOSAL_FILENAME)
            if os.path.exists(path):
                os.remove(path)
            return self._json({"ok": True, **project_payload(sl)})

        if url.path == "/api/ingest/apply":
            sys.path.insert(0, HERE)
            import story_ingest
            path = ppath(sl, PROPOSAL_FILENAME)
            proposal_doc = load_json(path, None)
            if not proposal_doc:
                return self._json({"error":
                    "No pending proposal for this project."}, 400)
            proposal = proposal_doc.get("proposal", {})
            accepted = set(body.get("accepted_ids") or [])
            if not accepted:
                return self._json({"error":
                    "Tick at least one item before applying."}, 400)
            with _lock:
                matrix_path = ppath(sl, "matrix_data.json")
                # Save the pre-merge matrix so /api/ingest/undo can restore
                # it verbatim without needing per-field diffs.
                before = load_json(matrix_path, {})
                save_json(ppath(sl, "ingest_undo.json"),
                          {"matrix": before})
                after = json.loads(json.dumps(before))  # deep copy
                counts = story_ingest.merge_into_matrix(
                    after, proposal, accepted)
                save_json(matrix_path, after)
                os.remove(path)
                ok, msg = run_generator(sl)
            if not ok:
                return self._json({"error": msg}, 500)
            return self._json({"ok": True, "counts": counts,
                               **project_payload(sl)})

        if url.path == "/api/ingest/undo":
            path = ppath(sl, "ingest_undo.json")
            snapshot = load_json(path, None)
            if not snapshot:
                return self._json({"error":
                    "Nothing to undo — no ingest has been applied."}, 400)
            with _lock:
                save_json(ppath(sl, "matrix_data.json"),
                          snapshot["matrix"])
                os.remove(path)
                ok, msg = run_generator(sl)
            if not ok:
                return self._json({"error": msg}, 500)
            return self._json({"ok": True, **project_payload(sl)})

        if url.path == "/api/brief":
            reviews = load_reviews(sl)
            cards_doc = load_json(ppath(sl, "cards.json"), {"cards": []})
            text, n = build_brief(sl, reviews, cards_doc.get("cards", []))
            path = brief_path(sl)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return self._json({"ok": True, "count": n, "text": text,
                               "path": path})

        return self._json({"error": "not found"}, 404)


def main():
    for p in list_projects():
        load_reviews(p["slug"])  # create review_state.json on first run
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Show Bible Tracker running →  http://127.0.0.1:{PORT}")
    print(f"Projects: {', '.join(known_slugs()) or '(none)'} "
          f"· active: {active_slug()}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
