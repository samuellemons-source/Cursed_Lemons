#!/usr/bin/env python3
"""Story ingestion — first-pass identity extraction for a new project.

Given a short story, treatment or novella, ask Claude to draft the identity
layer of a production matrix: lighting, characters, factions, props and
locations. The result is a STAGED PROPOSAL that never touches
`matrix_data.json` until the operator accepts it in the review panel.

Why this file exists separately from `app.py`
---------------------------------------------
- `app.py` is already ~1600 lines plus embedded HTML. The extractor has a
  large system prompt, its own JSON schema, and its own validator. Splitting
  it out keeps both files reviewable.
- The Anthropic call layer (`anthropic_request`, `pick_model`) already lives
  in `app.py`. Both callables are passed in so this module has no import
  cycle back into the app and can be exercised in isolation.

Contract
--------
Claude returns a single JSON object of this shape (order-preserving; the
tracker treats missing keys as empty):

    {
      "lighting":   [{"id":"LIT-…","desc":"one sentence","grounding":"stated|inferred","source_quote":"…"}],
      "characters": [{"id":"CH-…","name":"…","base":"…","wardrobe":{…8 slots…},
                     "states":[{"id":"CH-…-A","name":"…","delta":"…"}],
                     "grounding":"stated|inferred","source_quote":"…"}],
      "factions":   [{"id":"FAC-…","name":"…","base":"…","wardrobe":"…",
                     "states":[{"name":"…","delta":"…"}],
                     "grounding":"…","source_quote":"…"}],
      "props":      [{"id":"PRP-…","name":"…","base":"…",
                     "states":[{"name":"…","delta":"…"}],
                     "grounding":"…","source_quote":"…"}],
      "locations":  [{"id":"LOC-…","name":"…","anchor":"…",
                     "states":[{"name":"…","delta":"…"}],
                     "needs_reverse":false,
                     "grounding":"…","source_quote":"…"}]
    }

Everything is a DRAFT. The tracker's review panel is where individual items
are accepted or rejected. Nothing here writes to disk.
"""

import json
import re

# Wardrobe slots the generator's T1 soul factory reads. All eight must be
# filled with a non-empty sentence, otherwise the generated soul card leaks
# instructional prose ("nothing on the head") in the wrong slots.
WARDROBE_SLOTS = ("order", "head", "torso", "hands", "legs", "feet",
                  "carried", "closing")

# Every collection prefix is enforced. A prefix mismatch is a schema bug
# rather than a stylistic quibble: the shot factory keys off id prefixes to
# resolve Element callouts.
PREFIX = {"lighting": "LIT-", "characters": "CH-", "factions": "FAC-",
          "props": "PRP-", "locations": "LOC-"}

SYSTEM_BASE = """\
You are a script supervisor drafting the identity layer of a film production \
matrix from a written story. Your output is a STAGED PROPOSAL: the operator \
will review each item, accept or reject it, and only then does anything get \
written to the source data. Draft accurately, ground everything you can, and \
do not invent whole entities that are not in the text.

ID DISCIPLINE (STRICT)
Prefixes are enforced by the pipeline:
    lighting   LIT-<UPPER-KEY>       LIT-STAGE, LIT-STREET, LIT-BOOTH
    character  CH-<UPPER-KEY>        CH-VANE, CH-HATCH
    faction    FAC-<UPPER-KEY>       FAC-PIT, FAC-BAR, FAC-VIP
    prop       PRP-<UPPER-KEY>       PRP-EGG, PRP-MIC
    location   LOC-<UPPER-KEY>       LOC-DOOR, LOC-STAGE
Keys are ASCII uppercase, digits and hyphens only. Short, mnemonic, unique.

GROUNDING (required on every item)
    grounding      "stated" if the story explicitly describes the entity, \
                   "inferred" if you reasoned it from context.
    source_quote   the sentence or short passage from the story that grounds \
                   it; empty string when grounding is "inferred".
The operator uses these to review fast — they should accept or reject each \
item at a glance without re-reading the story.

BEING FAITHFUL VS BEING USEFUL
This is a FIRST DRAFT, not the shipping bible. Keep every description as \
short as possible while still being usable — one or two sentences is right, \
long paragraphs are wrong. The operator has a downstream loop that corrects \
specific details from render feedback; your job is to give them a coherent \
starting point, not to over-invent.

ANTI-PATTERNS
- Do not invent entities the story never suggests.
- Do not describe camera, lens, film stock or lighting look in the fields \
  — those belong to project doctrine.
- Do not include readable text on garments or signage; describe visible text \
  as "hand-painted, illegible" or similar.
- Do not write more than 3 states per entity.

OUTPUT
Return STRICT JSON only, matching the shape given in the user message. No \
prose. No markdown fence. Only the requested top-level key.
"""


# --------- per-collection specs --------------------------------------------
# Each spec drives one Claude call. Keeping them isolated lets an operator get
# partial results even when one collection blows a budget or fails a schema
# check.

_LIGHTING_SPEC = """\
Extract the LIGHTING layer. Return {"lighting": [ {id, desc, grounding, \
source_quote}, … ]}.

Fields:
- desc  ONE sentence describing the mood and shape of the light for that \
        setup. NO camera or lens language.

Include only lighting states the story actually implies. 2-8 items is normal; \
a story that lives in one room can have as few as one."""

_PROPS_SPEC = """\
Extract hero PROPS — objects the plot turns on. Return \
{"props": [ {id, name, base, states, grounding, source_quote}, … ]}.

Not set dressing. Not every glass on a table. The egg the plot revolves \
around: yes. A hero microphone: yes. A generic can of beer: no.

Fields:
- name    on-screen name in the story's own capitalisation
- base    1-2 sentences describing the object itself
- states  0-3 items, each {name, delta}. States are how a hero prop changes \
          across the film (wet, cracked, hatched, etc.). Empty array if the \
          prop is unchanging."""

_LOCATIONS_SPEC = """\
Extract LOCATIONS — distinct spaces the action visits. Return \
{"locations": [ {id, name, anchor, states, needs_reverse, grounding, \
source_quote}, … ]}.

Fields:
- anchor         1-2 sentences: the ONE unchanging visual truth of the space.
- states         0-3 items, each {name, delta}: lit/dressed variants.
- needs_reverse  true ONLY if the story clearly requires the opposite angle \
                 as well as the master view."""

_FACTIONS_SPEC = """\
Extract FACTIONS — named groups, cliques or crowds the story treats as a \
group. Return {"factions": [ {id, name, base, wardrobe, states, grounding, \
source_quote}, … ]}.

NOT a duplicate of every extra. A faction is worth listing when the story \
treats it as a shared identity ("the pit", "the police").

Fields:
- base       1-2 sentences describing the crowd as a coherent group
- wardrobe   1-2 sentences on the shared styling rule (single string, NOT \
             an object)
- states     0-3 items, each {name, delta}"""

_CHARACTERS_SPEC = """\
Extract CHARACTERS — every named or clearly present speaking/acting entity. \
Return {"characters": [ {id, name, base, wardrobe, states, grounding, \
source_quote}, … ]}.

Fields:
- name      on-screen name in the story's own capitalisation
- base      1-2 sentences: species, build, face, condition. NO clothing \
            (that is what wardrobe is for).
- wardrobe  object with EXACTLY these 8 keys, EACH a SINGLE short sentence \
            ending in a period (aim for 8-20 words per slot):
              order    layering rule from skin outward
              head     headwear, hair, throat/collar
              torso    torso layers, innermost to outermost
              hands    hands, wrists, rings, gloves
              legs     lower garments and belts
              feet     footwear and hosiery
              carried  what is held or slung; "Nothing is carried." if empty
              closing  what is NOT worn plus condition tag
- states    0-3 items, each {id, name, delta}. `id` is "<CH-ID>-<A|B|C>". \
            Include only states the story actually shows.

Fill wardrobe slots completely even when the story is silent — that is what \
"inferred" grounding is for. Keep each slot to ONE tight sentence."""

SPECS = {
    "lighting":   _LIGHTING_SPEC,
    "props":      _PROPS_SPEC,
    "locations":  _LOCATIONS_SPEC,
    "factions":   _FACTIONS_SPEC,
    "characters": _CHARACTERS_SPEC,
}


def _build_user_message(story_text, title, coll_spec):
    return (f"PROJECT TITLE: {title}\n\n"
            f"STORY:\n\"\"\"\n{story_text.strip()}\n\"\"\"\n\n"
            f"{coll_spec}\n\nReturn JSON.")


def _strip_fence(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    return text


ID_RE = re.compile(r"^[A-Z]+-[A-Z0-9][A-Z0-9\-]*$")


def _clean_str(v):
    return v.strip() if isinstance(v, str) else ""


def _issues_lighting(lt):
    out = []
    if not ID_RE.match(_clean_str(lt.get("id"))):
        out.append("id must be UPPERCASE-DASH form")
    elif not lt["id"].startswith(PREFIX["lighting"]):
        out.append(f"id must start with {PREFIX['lighting']}")
    if not _clean_str(lt.get("desc")):
        out.append("desc is empty")
    return out


def _issues_character(ch):
    out = []
    cid = _clean_str(ch.get("id"))
    if not ID_RE.match(cid) or not cid.startswith(PREFIX["characters"]):
        out.append(f"id must start with {PREFIX['characters']}")
    if not _clean_str(ch.get("name")):
        out.append("name is empty")
    if not _clean_str(ch.get("base")):
        out.append("base is empty")
    wd = ch.get("wardrobe")
    if not isinstance(wd, dict):
        out.append("wardrobe must be an object with 8 slots")
    else:
        for slot in WARDROBE_SLOTS:
            if not _clean_str(wd.get(slot)):
                out.append(f"wardrobe.{slot} is empty")
    for i, st in enumerate(ch.get("states") or []):
        if not _clean_str(st.get("id")):
            out.append(f"state {i} has no id")
        if not _clean_str(st.get("name")):
            out.append(f"state {i} has no name")
        if not _clean_str(st.get("delta")):
            out.append(f"state {i} has no delta")
    return out


def _issues_faction(fa):
    out = []
    fid = _clean_str(fa.get("id"))
    if not ID_RE.match(fid) or not fid.startswith(PREFIX["factions"]):
        out.append(f"id must start with {PREFIX['factions']}")
    for k in ("name", "base", "wardrobe"):
        if not _clean_str(fa.get(k)):
            out.append(f"{k} is empty")
    return out


def _issues_prop(pr):
    out = []
    pid = _clean_str(pr.get("id"))
    if not ID_RE.match(pid) or not pid.startswith(PREFIX["props"]):
        out.append(f"id must start with {PREFIX['props']}")
    for k in ("name", "base"):
        if not _clean_str(pr.get(k)):
            out.append(f"{k} is empty")
    return out


def _issues_location(lc):
    out = []
    lid = _clean_str(lc.get("id"))
    if not ID_RE.match(lid) or not lid.startswith(PREFIX["locations"]):
        out.append(f"id must start with {PREFIX['locations']}")
    for k in ("name", "anchor"):
        if not _clean_str(lc.get(k)):
            out.append(f"{k} is empty")
    return out


_VALIDATORS = {
    "lighting": _issues_lighting,
    "characters": _issues_character,
    "factions": _issues_faction,
    "props": _issues_prop,
    "locations": _issues_location,
}


def normalise(proposal):
    """Validate and clean a raw proposal.

    - Discards items that are unusably broken (no id, no name) so the review
      panel never shows a card with no handle to accept.
    - Attaches an `_issues` array to items with fixable defects, and stamps
      grounding onto anything missing it. Nothing gets dropped for a fixable
      issue; the operator sees the defect and decides.
    - Enforces id uniqueness across the whole proposal.
    """
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be a JSON object at the top level")

    out = {k: [] for k in _VALIDATORS}
    seen_ids = set()
    stats = {"kept": 0, "dropped": 0, "warned": 0}

    for coll, validator in _VALIDATORS.items():
        raw = proposal.get(coll) or []
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                stats["dropped"] += 1
                continue
            iid = _clean_str(item.get("id"))
            if not iid or not ID_RE.match(iid):
                stats["dropped"] += 1
                continue
            if iid in seen_ids:
                stats["dropped"] += 1
                continue
            seen_ids.add(iid)
            issues = validator(item)
            # Default grounding if Claude forgot to stamp it — better than
            # silently dropping the item on a bookkeeping omission.
            if item.get("grounding") not in ("stated", "inferred"):
                item["grounding"] = "inferred"
            if not isinstance(item.get("source_quote"), str):
                item["source_quote"] = ""
            if issues:
                item["_issues"] = issues
                stats["warned"] += 1
            stats["kept"] += 1
            out[coll].append(item)
    return out, stats


def _one_call(request_fn, model, spec, story_text, title, max_tokens, timeout):
    """Fire one Claude call for a single collection and return the parsed
    top-level dict. Raises RuntimeError with a specific message on any of the
    common failure modes so the caller can attribute the blame per-collection
    instead of failing the whole ingest on one bad answer."""
    resp = request_fn("/v1/messages", {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM_BASE,
        "messages": [{"role": "user",
                      "content": _build_user_message(story_text, title, spec)}],
    }, "POST", timeout=timeout)
    if resp.get("stop_reason") == "max_tokens":
        raise RuntimeError("Claude hit its output token cap.")
    text = "".join(b.get("text", "") for b in resp.get("content", [])).strip()
    text = _strip_fence(text)
    try:
        return json.loads(text)
    except ValueError as e:
        raise RuntimeError(
            f"Claude did not return usable JSON: {e}. First 200 chars: "
            f"{text[:200]}")


# Per-collection token budgets. Characters is much larger than the others
# because 8-slot wardrobes for a full cast is the biggest single output. The
# rest are set generously but sanely, so a wandering model doesn't set fire
# to a wallet on one over-produced field.
BUDGETS = {
    "lighting":   4000,
    "props":      6000,
    "locations":  8000,
    "factions":   8000,
    "characters": 24000,
}

# The order Claude sees the collections. Small/mechanical collections first
# so an operator watching the log sees quick early wins, and character
# extraction — the most expensive call — runs last where its failure does
# not sink the cheaper items.
CALL_ORDER = ("lighting", "props", "locations", "factions", "characters")


def extract(story_text, title, request_fn, model, timeout=360):
    """Extract the identity layer, one collection at a time.

    Returns (proposal, stats, raw_notes). `raw_notes` is a list of per-call
    diagnostics (collection, item count, any error message), useful for the
    review panel and for verification tests.

    Splitting one endpoint into five Claude calls is a deliberate trade-off:
    a single fused call was hitting max_tokens on the Komodo short (a 6k-word
    story with a 22-character cast). Per-collection calls fit their budgets
    comfortably, isolate failures, and give the operator meaningful partial
    output when one collection struggles."""
    if not (story_text or "").strip():
        raise ValueError("story is empty")

    merged = {k: [] for k in _VALIDATORS}
    raw_notes = []
    failed = []

    for coll in CALL_ORDER:
        try:
            raw = _one_call(request_fn, model, SPECS[coll], story_text,
                            title, BUDGETS[coll], timeout)
        except RuntimeError as e:
            failed.append((coll, str(e)))
            raw_notes.append({"collection": coll, "error": str(e)})
            continue
        items = raw.get(coll)
        if not isinstance(items, list):
            failed.append((coll, "response missing the expected key"))
            raw_notes.append({"collection": coll,
                              "error": "response missing the expected key",
                              "first_key": next(iter(raw.keys()), None)})
            continue
        merged[coll].extend(items)
        raw_notes.append({"collection": coll, "count": len(items)})

    proposal, stats = normalise(merged)
    stats["failed_collections"] = [c for c, _ in failed]

    total = sum(len(v) for v in proposal.values())
    if total == 0:
        # Every collection blew. Surface the first error verbatim so the
        # operator can actually debug the ingest.
        detail = failed[0][1] if failed else "unknown reason"
        raise RuntimeError(
            f"Extraction produced nothing usable. First error: {detail}")

    return proposal, stats, raw_notes


def merge_into_matrix(matrix, proposal, accepted_ids):
    """Fold accepted proposal items into an existing matrix in place.

    `accepted_ids` is a set of ids the operator ticked in the review panel;
    everything else is discarded. Returns the count merged per collection so
    the app can surface it in a toast.

    Rules of engagement:
    - Grounding metadata is stripped before writing — it is a review-time
      aid, not shipping data.
    - Existing matrix rows with the same id are REPLACED. The proposal is a
      re-draft; the operator has already opted in to overwriting.
    - Lighting is a map by id; every other collection is a list of dicts.
    - ELEMENTS callouts are (re)generated for the union of every character
      and prop id in the matrix, pending status. The generator flips them to
      `ready` when a Higgsfield reference has been minted; we never touch
      that flag here.
    """
    counts = {k: 0 for k in _VALIDATORS}

    def clean(item):
        return {k: v for k, v in item.items()
                if k not in ("grounding", "source_quote", "_issues")}

    for lt in proposal.get("lighting", []):
        if lt["id"] not in accepted_ids:
            continue
        matrix.setdefault("LIGHTING", {})[lt["id"]] = lt["desc"]
        counts["lighting"] += 1

    def upsert_list(key, coll):
        rows = proposal.get(coll, [])
        accepted = [clean(r) for r in rows if r["id"] in accepted_ids]
        if not accepted:
            return
        existing = matrix.setdefault(key, [])
        by_id = {r.get("id"): i for i, r in enumerate(existing)}
        for row in accepted:
            if row["id"] in by_id:
                existing[by_id[row["id"]]] = row
            else:
                existing.append(row)
            counts[coll] += 1

    upsert_list("CHARACTERS", "characters")
    upsert_list("FACTIONS", "factions")
    upsert_list("PROPS", "props")
    upsert_list("LOCATIONS", "locations")

    # Element callouts — every named character and prop needs a stub so shot
    # cards can cite them. Existing entries are preserved verbatim so any
    # `status: ready` flags survive.
    elems = matrix.setdefault("ELEMENTS", {})
    if "_doc" not in elems:
        elems["_doc"] = ("Higgsfield Element callouts. `status: ready` means "
                         "the browser @-picker resolves the callout to a "
                         "trained reference, and shot cards cite it "
                         "directly. Anything else and the shot factory "
                         "degrades to a short `[see T1 · NAME / SOUL]` "
                         "pointer and stamps the card [DEGRADED]. Flip to "
                         "ready ONLY after minting the reference in "
                         "Higgsfield.")
    def callout_for(name):
        # Match the existing convention: single at-sign followed by the name
        # collapsed to the first token, title-cased. e.g. "BIG SUE" -> "@Sue".
        first = re.split(r"\s+", (name or "").strip(), maxsplit=1)[0] or "X"
        first = re.sub(r"[^A-Za-z0-9]", "", first) or "X"
        return "@" + first[:1].upper() + first[1:].lower()

    for ch in matrix.get("CHARACTERS", []):
        cid = ch.get("id")
        if cid and cid not in elems:
            elems[cid] = {"callout": callout_for(ch.get("name", cid)),
                          "status": "pending"}
    for pr in matrix.get("PROPS", []):
        pid = pr.get("id")
        if pid and pid not in elems:
            elems[pid] = {"callout": callout_for(pr.get("name", pid)),
                          "status": "pending"}

    return counts
