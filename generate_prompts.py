#!/usr/bin/env python3
"""Show-bible prompt generator (multi-project).

Each project lives in its own isolated folder under `projects/<slug>/`:

    projects/<slug>/project.json      title, output paths, house doctrine,
                                      section order  (edit to retune a film)
    projects/<slug>/matrix_data.json  SOURCE OF TRUTH — nouns x states
    projects/<slug>/cards.json        generated card data for the tracker app
    projects/<slug>/review_state.json operator progress + director notes

For the selected project the generator walks the production matrix and emits:

1. The project's prompt document — virgin prompt cards (text-only, zero
   attachments) for every entity x state x view, in dependency order.
2. The project's `cards.json`, consumed by the tracker app (`app.py`).
3. Optionally a Cursor presentation canvas with the same cards embedded.

Card keys are stable (entity-id/state-id/view), so tracker progress and
notes survive regeneration.

Usage:
    python3 generate_prompts.py                 # the active project
    python3 generate_prompts.py --project foo   # one project by slug
    python3 generate_prompts.py --all           # every project
    python3 generate_prompts.py --list          # list known projects
"""

import argparse
import datetime
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(HERE, "projects")
APP_STATE_PATH = os.path.join(HERE, "app_state.json")
# No preferred default slug in a shared build — the first available project
# wins, and calling the generator with no projects at all is an error.
DEFAULT_SLUG = None

COLLECTIONS = ["SCORE", "CHARACTERS", "FACTIONS", "CREATURES", "PROPS", "MOVERS",
               "LOCATIONS", "LOOK_PLATES", "SHOTS", "LAYOUTS"]

# Fallback house doctrine. A project.json "doctrine" block overrides any of
# these per project, so a different film can carry a different era and
# camera package without touching this file.
DEFAULT_DOCTRINE = {
    "period_lock": ("Strictly 1988: no modern buildings, no modern vehicles, "
                    "no modern gear, no modern signage."),
    "crowd_line": "The spectators are all adults.",
    "photo_lock": "No text, no labels, no graphics — pure photography.",
    "sheet_look": ("Neutral gray seamless studio background, flat even studio "
                   "light with soft shadows, photorealistic skin with visible "
                   "pores and natural texture, no retouching, no film grain, "
                   "no color grade."),
    "eye_line": ("Each pupil carries a small bright catch-light reflection so "
                 "the eyes read alive."),
    "prop_look": ("Neutral light-gray seamless studio background, soft even "
                  "product-photography light from upper left, true-to-life "
                  "materials and wear, photorealistic, no film grain, no "
                  "stylized grade."),
    # Cinematography layer. Per doctrine the cinema look lives ONLY in
    # in-world plates; identity/prop sheets stay clean and get neutral lens
    # anchors instead.
    "cine_look": ("Cinematography: photographed on a Panavision 35mm "
                  "motion-picture film camera with vintage Panavision "
                  "anamorphic lenses. Widescreen anamorphic frame with gentle "
                  "oval bokeh, subtle horizontal flares where hard light "
                  "strikes the lens, softly vignetted corners, organic 35mm "
                  "film grain, Kodachrome-leaning color — the texture of a "
                  "1988 feature film, not video and not clean digital."),
    "cine_qc": ("35mm anamorphic film look present (oval bokeh, organic "
                "grain, horizontal flares only where motivated)"),
    "face_lens": ("Shot at eye level on an 85mm portrait lens, the face in "
                  "critical sharp focus."),
    "body_lens": ("Shot from chest height on a 50mm lens, camera level and "
                  "square to the subject, no wide-angle distortion, the full "
                  "figure in sharp focus head to boots."),
    "prop_lens": ("Shot on a 100mm product-photography lens, camera level "
                  "with the object, deep focus, every material detail "
                  "sharp."),
    # Wardrobe continuity sheets. These exist to pin costume down so it
    # survives regeneration, so they are deliberately flat and literal.
    "grid_look": ("Every panel shares one identical neutral gray seamless "
                  "background and one identical flat even studio lighting "
                  "setup, so the garment colors read exactly the same in all "
                  "of them. Panels are equal in size and evenly spaced with "
                  "thin uniform gutters between them. No borders, no frames, "
                  "no captions, no numbers, no arrows, no annotation of any "
                  "kind."),
    "flat_look": ("Overhead flat-lay photograph looking straight down at a "
                  "neutral light-gray seamless surface. The garments are laid "
                  "out flat and empty — no body, no person, no mannequin, no "
                  "dress form, no hanger — arranged in a neat evenly spaced "
                  "grid with each item fully separated from the others and "
                  "nothing overlapping. Soft even light from the upper left, "
                  "no hard shadows, true-to-life color and material."),
    "detail_lens": ("Shot on a 100mm lens at close range, camera square to "
                    "the garment, every seam, stitch, closure and edge in "
                    "critical sharp focus."),
    "macro_lens": ("Shot on a 100mm macro lens filling the frame with the "
                   "cloth itself, the weave, nap and surface wear in critical "
                   "sharp focus, raking light from the left to reveal "
                   "texture."),
    # Location plates. `loc_view` is the framing sentence and `world_line`
    # names the world, so a POV-locked film can refuse the oblique
    # establishing plate outright rather than generating one nobody may cut.
    "loc_view": ("Photorealistic photograph, three-quarter oblique "
                 "establishing view — never flat frontal — of the following "
                 "location"),
    "world_line": "at a 1988 Lake Tahoe ski resort.",
    # "cine" wraps each plate in the full camera package and period lock, which
    # is right when the plate is a shot. "plate" strips all of it back to the
    # place itself, which is right when the plate is describing a location for
    # the art department and the framing is not yet decided.
    "loc_style": "cine",
    "plate_look": ("Shot to show the space clearly, natural perspective, "
                   "everything in focus, true colour."),
    "plate_lock": ("Everything in frame belongs to the period: no later "
                   "technology, clothing, vehicles or signage. No text, "
                   "lettering or logos anywhere."),
}

# The lean tier stack is what fresh projects run against — every collection
# here consumes fields the ingest schema actually produces (base, wardrobe
# slots, states, anchor). The old rich sections (CHARACTERS/FACTIONS/…) are
# still available for existing projects because those projects list their
# own sections in project.json; they are just no longer the default for a
# fresh project, since they expect fields (`group`, `crowd`, `pov`, `look`)
# that a story ingest does not draft.
#
# SHOTS is left out on purpose: shot cards need a hand-authored SHOTS list
# and a GEO block; a fresh project has neither, and the tier is added by
# copying an existing project or by editing project.json when the shot list
# is drafted.
DEFAULT_SECTIONS = [
    {"collection": "T1_SOULS", "short": "T1 Souls",
     "md_title": "T1 · CHARACTER SOULS — mint every Higgsfield Element "
                 "from these",
     "blurb": "One chest-up portrait per named character."},
    {"collection": "T1F_FACTIONS", "short": "T1F Factions",
     "md_title": "T1F · CROWD FACTIONS — one styling sheet per group",
     "blurb": "Group plate per faction, six to eight figures."},
    {"collection": "T2_PLATES", "short": "T2 Plates",
     "md_title": "T2 · LOCATION PLATES — one master per location × state",
     "blurb": "Establishing plate for every location state in the matrix."},
    {"collection": "T5_PROPS", "short": "T5 Props",
     "md_title": "T5 · HERO PROPS — one product sheet per hero prop",
     "blurb": "Product-style sheet per hero prop."},
    # Always last, and present in every project: these are house standards
    # rather than a queue of work, and a new project should inherit them.
    {"collection": "LAYOUTS", "short": "Technical Layout Format Headers",
     "md_title": "TECHNICAL LAYOUT FORMAT HEADERS — house standards, no "
                 "subject attached",
     "blurb": "Reusable technical layouts with the subject left blank."},
]

EMPTY_MATRIX = {c: ({} if c == "LIGHTING" else []) for c in COLLECTIONS}
EMPTY_MATRIX["LIGHTING"] = {}
# GEO and ELEMENTS are cross-reference dicts consumed by the SHOTS factory —
# GEO is the geography spine (11 canon zones with anchor/neighbours/compass),
# ELEMENTS maps matrix ids to their Higgsfield callouts and ready status so
# a shot degrades gracefully from `@callout` to full prose per identity.
EMPTY_MATRIX["GEO"] = {}
EMPTY_MATRIX["ELEMENTS"] = {}

# Populated by load_project(); every card factory reads these.
PROJECT = {}
SCORE = []
LIGHTING = {}
CHARACTERS = FACTIONS = CREATURES = PROPS = MOVERS = LOCATIONS = []
SCORE = []
LOOK_PLATES = []
SHOTS = []
GEO = {}
ELEMENTS = {}
PERIOD_LOCK = CROWD_LINE = PHOTO_LOCK = ""
SHEET_LOOK = EYE_LINE = PROP_LOOK = ""
CINE_LOOK = CINE_QC = FACE_LENS = BODY_LENS = PROP_LENS = ""
LOC_VIEW = WORLD_LINE = LOC_STYLE = PLATE_LOOK = PLATE_LOCK = ""

_seen_keys = set()


def project_dir(slug):
    return os.path.join(PROJECTS_DIR, slug)


def list_projects():
    """Slugs of every project folder holding a project.json, sorted."""
    if not os.path.isdir(PROJECTS_DIR):
        return []
    return sorted(
        d for d in os.listdir(PROJECTS_DIR)
        if os.path.isfile(os.path.join(PROJECTS_DIR, d, "project.json")))


def active_slug():
    """The project the app last selected, falling back sensibly."""
    known = list_projects()
    try:
        with open(APP_STATE_PATH, encoding="utf-8") as f:
            slug = json.load(f).get("active")
        if slug in known:
            return slug
    except (OSError, ValueError):
        pass
    if DEFAULT_SLUG and DEFAULT_SLUG in known:
        return DEFAULT_SLUG
    return known[0] if known else (DEFAULT_SLUG or "")


def resolve_path(path, slug):
    """Project paths may be absolute, ~-anchored, or project-dir relative."""
    if not path:
        return None
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(project_dir(slug), path))


def load_project(slug):
    """Load one project's config + matrix into the module globals."""
    global LOC_VIEW, WORLD_LINE, LOC_STYLE, PLATE_LOOK, PLATE_LOCK
    global SCORE
    global PROJECT, LIGHTING, CHARACTERS, FACTIONS, CREATURES, PROPS
    global MOVERS, LOCATIONS, LOOK_PLATES, _seen_keys
    global SHOTS, GEO, ELEMENTS
    global PERIOD_LOCK, CROWD_LINE, PHOTO_LOCK, SHEET_LOOK, EYE_LINE
    global PROP_LOOK, CINE_LOOK, CINE_QC, FACE_LENS, BODY_LENS, PROP_LENS
    global GRID_LOOK, FLAT_LOOK, DETAIL_LENS, MACRO_LENS

    pdir = project_dir(slug)
    with open(os.path.join(pdir, "project.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["slug"] = slug

    matrix_path = os.path.join(pdir, "matrix_data.json")
    if os.path.exists(matrix_path):
        with open(matrix_path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = dict(EMPTY_MATRIX)

    LIGHTING = data.get("LIGHTING", {})
    SCORE = data.get("SCORE", [])
    CHARACTERS = data.get("CHARACTERS", [])
    FACTIONS = data.get("FACTIONS", [])
    CREATURES = data.get("CREATURES", [])
    PROPS = data.get("PROPS", [])
    MOVERS = data.get("MOVERS", [])
    LOCATIONS = data.get("LOCATIONS", [])
    LOOK_PLATES = data.get("LOOK_PLATES", [])
    SHOTS = data.get("SHOTS", [])
    GEO = data.get("GEO", {})
    ELEMENTS = data.get("ELEMENTS", {})

    doctrine = dict(DEFAULT_DOCTRINE)
    doctrine.update(cfg.get("doctrine") or {})
    cfg["doctrine"] = doctrine
    PERIOD_LOCK = doctrine["period_lock"]
    CROWD_LINE = doctrine["crowd_line"]
    PHOTO_LOCK = doctrine["photo_lock"]
    SHEET_LOOK = doctrine["sheet_look"]
    EYE_LINE = doctrine["eye_line"]
    PROP_LOOK = doctrine["prop_look"]
    CINE_LOOK = doctrine["cine_look"]
    CINE_QC = doctrine["cine_qc"]
    FACE_LENS = doctrine["face_lens"]
    BODY_LENS = doctrine["body_lens"]
    PROP_LENS = doctrine["prop_lens"]
    GRID_LOOK = doctrine["grid_look"]
    FLAT_LOOK = doctrine["flat_look"]
    DETAIL_LENS = doctrine["detail_lens"]
    MACRO_LENS = doctrine["macro_lens"]
    LOC_VIEW = doctrine["loc_view"]
    WORLD_LINE = doctrine["world_line"]
    LOC_STYLE = doctrine["loc_style"]
    PLATE_LOOK = doctrine["plate_look"]
    PLATE_LOCK = doctrine["plate_lock"]

    cfg.setdefault("sections", DEFAULT_SECTIONS)
    cfg.setdefault("title", slug)
    cfg.setdefault("subtitle", "")
    cfg.setdefault("source_note", "the production matrix")
    outputs = cfg.setdefault("outputs", {})
    cfg["paths"] = {
        "dir": pdir,
        "matrix": matrix_path,
        "cards": os.path.join(pdir, "cards.json"),
        "reviews": os.path.join(pdir, "review_state.json"),
        "markdown": resolve_path(outputs.get("markdown")
                                 or "SHOW_BIBLE_PROMPTS.md", slug),
        "brief": resolve_path(outputs.get("brief")
                              or "REVISION_BRIEF.md", slug),
        "canvas": resolve_path(outputs.get("canvas"), slug),
    }
    PROJECT = cfg
    _seen_keys = set()
    return cfg


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "x"


def with_cine_qc(qc):
    """Append the anamorphic-look QC clause without double punctuation."""
    return qc.rstrip(". ") + "; " + CINE_QC + "."


def card(key, title, model, ar, res, takes, prompt, qc,
         spec_md=None, spec_pills=None, alt_copy=None, kind="render"):
    """Build one prompt-card record. `key` must be stable across regens.

    `kind` separates the work queue from the house standards. "render" is
    something to generate and track; "reference" is a fixed standard that is
    always there and is never worked through, which the tracker presents
    differently so the two are not mistaken for each other.

    `spec_md` / `spec_pills` let a non-image card relabel its own spec line —
    an audio cue has no aspect ratio and no resolution, and mislabelling them
    is how a bible starts lying to the person holding it."""
    if key in _seen_keys:
        n = 2
        while f"{key}-{n}" in _seen_keys:
            n += 1
        key = f"{key}-{n}"
    _seen_keys.add(key)
    return {
        "key": key,
        "title": title,
        "kind": kind,
        "model": model,
        "ar": ar,
        "res": res,
        "takes": takes,
        "prompt": [p.strip() for p in prompt],
        "qc": qc,
        "spec_md": spec_md or (
            f"**Model:** {model} · **AR:** {ar} · **Resolution:** {res} · "
            f"**Takes:** {takes} · **Attachments:** NONE (virgin)"),
        "spec_pills": spec_pills or [
            model, f"AR {ar}", str(res), f"{takes} takes",
            "virgin — no attachments"],
        # A second one-click copy target, for tools that take two fields.
        "alt_copy": alt_copy,
    }


# Wardrobe is held as addressable slots rather than one prose blob so a
# note can target the boots without disturbing the jacket, and so sheets can
# draw only the slots they actually show.
WARDROBE_SLOTS = ["order", "head", "torso", "hands", "legs", "feet",
                  "carried", "closing"]


def wardrobe_text(ch, slots=None):
    w = ch.get("wardrobe") or {}
    return " ".join((w.get(k) or "").strip()
                    for k in (slots or WARDROBE_SLOTS)
                    if (w.get(k) or "").strip())


def subject(ch, slots=None):
    """Identity plus wardrobe — the phrase every character prompt hangs on."""
    dress = wardrobe_text(ch, slots)
    return ch["base"].strip() + (" " + dress if dress else "")


def stated_sex(ch):
    """Read the sex the character's own base text already states.

    The sheet names it twice — in the opening line and again in the CHARACTER
    block — because one mention drifts across a three-panel layout. It is only
    ever read from the base, never inferred from a name or a role: if the base
    does not say, the prompt stays neutral rather than guessing.
    """
    b = ch["base"].lower()
    if re.search(r"\b(woman|women|female|latina|she|her|hers)\b", b):
        return "female"
    if re.search(r"\b(man|men|male|he|his|him)\b", b):
        return "male"
    return None


# --------------------------------------------------------------------------
# LAYOUT HEADERS
#
# A technical layout — how many panels, framed how, posed how, lit how — is
# independent of who or what is in it. Keeping that language here rather than
# inline in each factory means one definition drives both the generated cards
# and the standalone header you paste into the browser by hand, so the two can
# never drift apart.
#
# Each entry supplies:
#   frame   list of subject-agnostic paragraphs; "{who}" is substituted
#   slots   the subject blocks the caller must supply, with the guidance text
#           the standalone version shows in their place
#   render  callable returning the lighting/lens paragraphs (reads doctrine,
#           which rebinds per project, so it must stay lazy)
#   qc      what to check before accepting a take
# --------------------------------------------------------------------------
SUB = "\u2039"   # guillemets bracket the blanks in a standalone header, so
SUB_ = "\u203a"  # what you must replace is never mistaken for prompt text.


def _blank(text):
    return f"{SUB}{text}{SUB_}"


LAYOUTS = {
    "three_up": {
        "title": "Three-up character sheet — bust, full front, full back",
        "note": ("The standard identity hand-off: one landscape frame, three "
                 "columns, same person and wardrobe in all of them."),
        "model": "Soul 2.0", "ar": "16:9", "res": "4K", "takes": 4,
        "who": "character",
        "frame": [
            "Character reference sheet of a single consistent {who}, presented "
            "on a pure clean deep neutral grey (#3a3a3c) seamless studio "
            "background, clean editorial layout arranged in three vertical "
            "sections, horizontal landscape composition read left to right, "
            "with identical character identity, identical wardrobe, identical "
            "lighting and identical color grading across every panel for "
            "perfect consistency.",

            "COLUMN 1 (largest, leftmost): chest-up portrait, front view, head "
            "and upper chest in frame, sharp focus on the eyes, soft "
            "catchlights in both eyes, neutral calm relaxed expression with "
            "the lips closed.",

            "COLUMN 2 (centre): full-body front view, standing relaxed neutral "
            "A-pose, arms slightly away from the body, weight evenly "
            "distributed, the full figure head-to-toe inside the frame with "
            "even margins.",

            "COLUMN 3 (rightmost): full-body back view, the same standing pose "
            "mirrored, showing hair fall, back posture, garment fit across the "
            "shoulders and the backs of the boots.",
        ],
        "slots": [
            ("CHARACTER (must remain identical in all three panels): ",
             "the fixed physical description of your subject — build, face, "
             "hair or hide, colouring, distinguishing marks. Body only, no "
             "clothing in this block"),
            ("WARDROBE (identical in all three panels, described from the "
             "skin outward): ",
             "the complete costume layer by layer from the skin outward, "
             "ending with whatever is carried"),
        ],
        # The carried kit stays in: other slots cross-reference it, and a model
        # sheet that omits the board or the microphone is not a complete
        # reference. It is pinned to one hand so the pose holds.
        "slot_tail": {
            1: ("Anything described here as carried or held is carried exactly "
                "as described in all three panels, held low and close at one "
                "side so it never crosses the body or hides the garments, and "
                "it is never set down, swapped between hands or left out of a "
                "panel."),
        },
        "render": lambda: [
            "LIGHTING & RENDER: clean soft even studio lighting, large "
            "diffused key light with gentle fill, soft natural shadows, no "
            "harsh highlights, true-to-life skin tones with realistic natural "
            "texture, neutral white balance, polished professional model-sheet "
            "aesthetic, shot on a full-frame camera with an 85mm lens look, "
            "crisp tack-sharp detail, high dynamic range, 8k. Photorealistic "
            "skin with visible pores and natural texture, no retouching, no "
            "film grain and no color grade — this is a reference sheet, not a "
            "frame from the film. The three panels "
            "are equal in height and evenly spaced with thin uniform gutters "
            "between them. No borders, no frames, no captions, no numbers, no "
            "arrows and no annotation of any kind. " + EYE_LINE],
        "qc": ("Exactly three columns, left to right: chest-up portrait, "
               "full-body front A-pose, full-body back; the same person, "
               "wardrobe, lighting and grade in all three; both full-body "
               "panels complete head to boots with even margins; any carried "
               "item present and held the same way in all three panels without "
               "hiding the garments; deep neutral grey seamless background "
               "throughout; no captions, numbers, borders or annotation; no "
               "film grain and no color grade."),
    },

    "turnaround": {
        "title": "Five-angle wardrobe turnaround",
        "note": ("Costume continuity. Five full-body panels rotating front to "
                 "back so the garment reads from every side."),
        "model": "Soul 2.0", "ar": "16:9", "res": "4K", "takes": 4,
        "who": "person",
        "frame": [
            "Photorealistic costume-continuity turnaround sheet: five "
            "full-body studio photographs of the same single {who} wearing "
            "identical wardrobe under identical lighting, arranged left to "
            "right in one horizontal row. Reading left to right the person "
            "turns a quarter turn at a time: square to camera, three-quarter "
            "front, exact side profile, three-quarter back, then directly away "
            "from camera. The pose is the same relaxed symmetrical stance in "
            "all five panels, arms hanging at the sides, weight even on both "
            "feet, head to boots fully in frame in every panel.",
        ],
        "slots": [
            ("The person in all five panels is ",
             "the subject's fixed physical description followed by the "
             "complete wardrobe from the skin outward"),
        ],
        "render": lambda: [BODY_LENS + " " + GRID_LOOK + " " + SHEET_LOOK],
        "qc": ("Exactly five panels in one row; identical person, garments, "
               "colors and light across all five; rotation steps evenly front "
               "to back; same stance throughout; no captions, numbers or "
               "borders."),
    },

    "flatlay": {
        "title": "Costume flat-lay",
        "note": ("The wardrobe unworn and separated, so every item can be "
                 "counted and colour-matched without a body in the way."),
        "model": "Nano Banana 2", "ar": "4:3", "res": "4K", "takes": 4,
        "who": "character",
        "frame": [
            "Photorealistic overhead costume flat-lay: one {who}'s complete "
            "wardrobe laid out unworn and empty on a neutral surface, every "
            "garment and item separated and fully visible, nothing overlapping "
            "and nothing folded over on itself. Outer garments are laid open "
            "and face up so their closures, linings and chest detail read "
            "clearly; gloves are laid as a matched pair; boots stand as a pair "
            "with laces dressed.",
        ],
        "slots": [
            ("The complete set of items laid out: ",
             "every item in the costume, listed once each, head to feet, "
             "ending with whatever is carried"),
        ],
        "render": lambda: [DETAIL_LENS + " " + FLAT_LOOK],
        "qc": ("No person, mannequin, dress form or hanger anywhere; every "
               "listed item present exactly once and nothing extra; colors and "
               "hardware match the turnaround sheet; no captions or labels."),
    },

    "upper": {
        "title": "Upper-body construction detail",
        "note": ("Head cropped out so the frame is all collar, closure, cuffs "
                 "and chest detail — how the garment is actually built."),
        "model": "Soul 2.0", "ar": "4:3", "res": "4K", "takes": 4,
        "who": "person",
        "frame": [
            "Photorealistic costume-construction detail photograph of the "
            "upper body only, framed from the top of the thighs to the base of "
            "the chin with the head cropped above the frame, standing square "
            "to camera. The frame is filled by collar, chest, front closure, "
            "insignia, pocket openings, cuffs and hands so that the "
            "construction of the garments reads unambiguously.",
        ],
        "slots": [
            ("The garments worn, described from the skin outward: ",
             "the layer order, then the torso garments, then the hands, then "
             "anything that must be excluded"),
        ],
        "render": lambda: [DETAIL_LENS + " " + SHEET_LOOK],
        "qc": ("Head cropped out of frame; collar, closure, insignia and cuffs "
               "all legible; layer order correct; colors and hardware match "
               "the turnaround sheet; no logos or lettering."),
    },

    "macro": {
        "title": "Fabric and wear macro",
        "note": ("Cloth only, edge to edge. Pins the material and the degree "
                 "of wear so every other card can be matched against it."),
        "model": "Nano Banana 2", "ar": "1:1", "res": "4K", "takes": 4,
        "who": "garment",
        "frame": [
            "Photorealistic extreme close-up material study: the cloth of the "
            "outermost torso {who} described below fills the entire frame edge "
            "to edge, shot flat-on so the weave structure, surface nap, "
            "pilling, seam stitching and genuine wear are all readable. Cloth "
            "only — no person, no face, no hands, no complete garment shape, "
            "no background visible around the cloth.",
        ],
        "slots": [
            ("The garment whose cloth fills the frame: ",
             "the single outermost torso garment, described by material, "
             "colour and condition"),
        ],
        "render": lambda: [MACRO_LENS],
        "qc": ("Frame is entirely cloth; weave, nap and wear clearly resolved; "
               "color matches the turnaround sheet exactly; no person or "
               "background; no logos or lettering."),
    },
}

LAYOUT_ORDER = ["three_up", "turnaround", "flatlay", "upper", "macro"]


def layout_frame(name, who=None):
    """The subject-agnostic header paragraphs for a technical layout."""
    lay = LAYOUTS[name]
    who = who or lay["who"]
    return [p.replace("{who}", who) for p in lay["frame"]]


def layout_body(name, *subjects, who=None):
    """Full prompt paragraphs for a layout: header, then the caller's subject
    blocks dropped into the layout's slots, then the render paragraphs.

    Pass one subject string per slot, in slot order."""
    lay = LAYOUTS[name]
    if len(subjects) != len(lay["slots"]):
        raise ValueError(
            f"layout {name!r} takes {len(lay['slots'])} subject block(s), "
            f"got {len(subjects)}")
    paras = layout_frame(name, who)
    tails = lay.get("slot_tail", {})
    for i, ((label, _guide), text) in enumerate(zip(lay["slots"], subjects)):
        para = label + text.strip()
        if i in tails:
            para = para.rstrip() + " " + tails[i]
        paras.append(para)
    return paras + lay["render"]()


def layout_header_cards(name):
    """The standalone, paste-it-yourself version of a technical layout.

    Reference material rather than a render order: the subject blocks are left
    as bracketed blanks, so this is the thing you copy when you are setting up
    a sheet by hand in the browser for something the bible does not track."""
    lay = LAYOUTS[name]
    paras = layout_frame(name)
    tails = lay.get("slot_tail", {})
    for i, (label, guide) in enumerate(lay["slots"]):
        para = label + _blank(guide)
        if i in tails:
            para = para + " " + tails[i]
        paras.append(para)
    paras += lay["render"]()
    paras.append(f"{_lean_period()} {_lean_photo()}".strip())
    spec = (f"**Layout:** {lay['title']} · **AR:** {lay['ar']} · "
            f"**Resolution:** {lay['res']} · **Suggested model:** "
            f"{lay['model']} · **Attachments:** NONE (virgin)")
    return [card(
        f"layout/{name}",
        f"LAYOUT · {lay['title']}",
        lay["model"], lay["ar"], lay["res"], lay["takes"], paras,
        lay["qc"] + f"  USAGE: replace each {SUB}bracketed blank{SUB_} with "
        "your own subject text and delete the brackets; if the subject's sex "
        f"is fixed, change \"{lay['who']}\" in the first paragraph to \"male "
        f"{lay['who']}\" or \"female {lay['who']}\".",
        spec_md=spec,
        spec_pills=[lay["ar"], lay["res"], "reference", "virgin"],
        kind="reference")]


def sheet_card(ch):
    """The official three-column character sheet.

    Portrait, full front and full back in one landscape frame, on the house
    grey. This is the sheet that gets handed to anyone who needs to know who
    this person is; the soul card remains the clean anchor a face ID is minted
    from.
    """
    sex = stated_sex(ch)
    who = f"{sex} character" if sex else "character"
    return card(
        f"{ch['id']}/sheet",
        f"{ch['name']} / OFFICIAL character sheet — three-column  [{ch['id']}]",
        ch.get("model", "Soul 2.0"), "16:9", "4K", 4,
        layout_body("three_up", ch["base"].strip(), wardrobe_text(ch),
                    who=who)
        + [PERIOD_LOCK + " " + PHOTO_LOCK],
        LAYOUTS["three_up"]["qc"])


def soul_card(ch):
    """The identity anchor every other card of this character must match.

    State-free and neutral on purpose: this is the portrait a Soul ID gets
    minted from, so it carries no damage, no action and no state props.
    """
    return card(
        f"{ch['id']}/soul",
        f"{ch['name']} / SOUL identity anchor  [{ch['id']}]",
        ch.get("model", "Soul 2.0"), "4:3", "4K", 4,
        ["Photorealistic studio identity portrait, head and shoulders only, "
         "the subject square to the camera and looking straight into the "
         "lens. The expression is neutral and relaxed with the lips closed, "
         "both eyes open and fully visible, the head upright and level, the "
         "face evenly lit and completely unobstructed by hair, hands or "
         "equipment. This is the master reference portrait that fixes who "
         "this person is, so the skin is clean and unmarked, the hair is "
         "settled, and the pose is symmetrical and still.",
         f"The person: {ch['base'].strip()}",
         f"Visible from the chest up: "
         f"{wardrobe_text(ch, ['head', 'torso', 'closing'])}",
         FACE_LENS + " " + SHEET_LOOK + " " + EYE_LINE,
         PERIOD_LOCK + " " + PHOTO_LOCK],
        "Neutral expression, lips closed, head level and square to camera; "
        "both eyes open with catch-lights; face unobstructed; skin clean and "
        "unmarked with no injury, dirt or state damage; no action and no "
        "state props; flat studio sheet with no grain and no grade.")


def wardrobe_cards(ch):
    """Continuity sheets that pin the costume down before any state work."""
    if not ch.get("wardrobe"):
        return []
    name, cid = ch["name"], ch["id"]
    model = ch.get("model", "Soul 2.0")
    out = []

    doc = [PERIOD_LOCK + " " + PHOTO_LOCK]

    out.append(card(
        f"{cid}/wardrobe/turnaround",
        f"{name} / wardrobe / five-angle turnaround  [{cid}]",
        model, "16:9", "4K", 4,
        layout_body("turnaround", subject(ch)) + doc,
        LAYOUTS["turnaround"]["qc"]))

    out.append(card(
        f"{cid}/wardrobe/flatlay",
        f"{name} / wardrobe / costume flat-lay  [{cid}]",
        "Nano Banana 2", "4:3", "4K", 4,
        layout_body("flatlay", wardrobe_text(
            ch, ["head", "torso", "hands", "legs", "feet", "carried",
                 "closing"])) + doc,
        LAYOUTS["flatlay"]["qc"]))

    out.append(card(
        f"{cid}/wardrobe/upper",
        f"{name} / wardrobe / upper-body construction detail  [{cid}]",
        model, "4:3", "4K", 4,
        layout_body("upper", wardrobe_text(
            ch, ["order", "torso", "hands", "closing"])) + doc,
        LAYOUTS["upper"]["qc"]))

    out.append(card(
        f"{cid}/wardrobe/fabric",
        f"{name} / wardrobe / fabric and wear macro  [{cid}]",
        "Nano Banana 2", "1:1", "4K", 4,
        layout_body("macro", wardrobe_text(ch, ["torso"])) + doc,
        LAYOUTS["macro"]["qc"]))
    return out


def character_cards(ch):
    out = [sheet_card(ch), soul_card(ch)] + wardrobe_cards(ch)
    for st in ch["states"]:
        base = subject(ch)
        # A head-and-shoulders frame can only show what sits above the chest,
        # so it carries those slots instead of the full kit.
        head_subject = subject(ch, ["head", "torso", "closing"])
        delta = st["delta"]
        sid = st["id"]
        kroot = f"{ch['id']}/{sid}"
        # face close-up
        out.append(card(
            f"{kroot}/face",
            f"{ch['name']} / {st['name']} / face close-up  [{sid}]",
            ch.get("model", "Soul 2.0"), "4:3", "2K", 4,
            [f"Photorealistic studio portrait photograph, head-and-shoulders "
             f"close-up of {head_subject}",
             f"In this state: {delta}",
             FACE_LENS + " " + SHEET_LOOK + " " + EYE_LINE,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            "Identity matches locked face; eyes have catch-lights; state details "
            "correct; no grain/grade in the sheet."))
        # full body front (headless via deterministic crop in post)
        out.append(card(
            f"{kroot}/front",
            f"{ch['name']} / {st['name']} / full body front  [{sid}]",
            ch.get("model", "Soul 2.0"), "3:4", "2K", 4,
            [f"Photorealistic full-body studio photograph, standing, facing "
             f"the camera squarely, head to boots fully in frame, of {base}",
             f"In this state: {delta}",
             BODY_LENS + " " + SHEET_LOOK,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            "Full figure in frame (headless plate is produced later by "
            "deterministic crop — do not prompt for headless); costume/equipment "
            "state correct top to bottom."))
        # full body back
        out.append(card(
            f"{kroot}/back",
            f"{ch['name']} / {st['name']} / full body back  [{sid}]",
            ch.get("model", "Soul 2.0"), "3:4", "2K", 4,
            [f"Photorealistic full-body studio photograph, standing, seen "
             f"directly from behind, head to boots fully in frame, of {base}",
             f"In this state: {delta}",
             BODY_LENS + " " + SHEET_LOOK,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            "Back silhouette, costume and equipment consistent with the front "
            "plate; no face visible."))
    return out


def faction_cards(fa):
    out = []
    # extras lookbook variants: one full-body front card each
    for va in fa.get("variants", []):
        out.append(card(
            f"{fa['id']}/var/{slug(va['name'])}",
            f"{fa['name']} / {va['name']}  [{fa['id']}]",
            fa.get("model", "Soul 2.0"), "3:4", "2K", 4,
            [f"Photorealistic full-body studio photograph, standing, "
             f"facing the camera squarely, head to boots fully in frame, "
             f"of {va['base']}.",
             BODY_LENS + " " + SHEET_LOOK,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            "Adult; wardrobe exactly as specified; 1988 only; no readable "
            "text anywhere."))
    if fa.get("variants"):
        # variants replace the single representative 3-view
        out.append(card(
            f"{fa['id']}/group",
            f"{fa['name']} / group plate  [{fa['id']}]",
            "Soul Cinema", "21:9", "2K", 4,
            [fa["group"],
             LIGHTING[fa["group_light"]],
             CINE_LOOK,
             (PERIOD_LOCK + (" " + CROWD_LINE if fa.get("crowd") else "")
              + " " + PHOTO_LOCK)],
            with_cine_qc(fa["group_qc"])))
        return out
    # representative 3-view
    for view_key, view, ar, view_desc, lens, qc in [
        ("face", "face/head close-up", "4:3",
         "head-and-shoulders close-up", FACE_LENS,
         "Head detail matches the class spec"),
        ("front", "full body front", "3:4",
         "full-body, standing, facing the camera squarely, head to boots in frame",
         BODY_LENS,
         "Full figure; kit correct top to bottom (headless plate via crop in post)"),
        ("back", "full body back", "3:4",
         "full-body, standing, seen directly from behind", BODY_LENS,
         "Back silhouette consistent with front"),
    ]:
        out.append(card(
            f"{fa['id']}/rep/{view_key}",
            f"{fa['name']} / representative / {view}  [{fa['id']}]",
            fa.get("model", "Soul 2.0"), ar, "2K", 4,
            [f"Photorealistic studio photograph, {view_desc}, of one "
             f"representative member of the following: {fa['base']}",
             lens + " " + SHEET_LOOK,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            qc + "; single figure only."))
    # group plate
    out.append(card(
        f"{fa['id']}/group",
        f"{fa['name']} / group plate  [{fa['id']}]",
        "Soul Cinema", "21:9", "2K", 4,
        [fa["group"],
         LIGHTING[fa["group_light"]],
         CINE_LOOK,
         (PERIOD_LOCK + (" " + CROWD_LINE if fa.get("crowd") else "")
          + " " + PHOTO_LOCK)],
        with_cine_qc(fa["group_qc"])))
    return out


def creature_cards(cr):
    out = []
    for st in cr["states"]:
        out.append(card(
            f"{cr['id']}/{slug(st['name'])}",
            f"{cr['name']} / {st['name']}  [{cr['id']}]",
            "Soul Cinema", "21:9", "2K", 4,
            [st["prompt"],
             LIGHTING[st["light"]],
             CINE_LOOK,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            with_cine_qc(st["qc"])))
    return out


def prop_cards(pr):
    out = []
    for st in pr["states"]:
        out.append(card(
            f"{pr['id']}/{slug(st['name'])}",
            f"{pr['name']} / {st['name']}  [{pr['id']}]",
            pr.get("model", "Nano Banana 2"), "4:3", "2K", 4,
            [f"Photorealistic product-style studio photograph of {pr['base']}",
             f"State: {st['delta']}",
             PROP_LENS + " " + PROP_LOOK,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            st.get("qc", "Materials, wear and state details correct; object "
                         "isolated and fully in frame.")))
    return out


def mover_cards(mv):
    out = []
    # studio sheet
    out.append(card(
        f"{mv['id']}/sheet",
        f"{mv['name']} / vehicle sheet  [{mv['id']}]",
        "Nano Banana 2", "4:3", "2K", 4,
        [f"Photorealistic product-style studio photograph, three-quarter "
         f"front view, of {mv['base']}",
         PROP_LENS + " " + PROP_LOOK,
         PERIOD_LOCK + " " + PHOTO_LOCK],
        "Machine design locked: color, era, details; no rider unless specified."))
    # in-world states
    for st in mv["states"]:
        out.append(card(
            f"{mv['id']}/{slug(st['name'])}",
            f"{mv['name']} / {st['name']}  [{mv['id']}]",
            "Soul Cinema", "21:9", "2K", 4,
            [st["prompt"],
             LIGHTING[st["light"]],
             CINE_LOOK,
             PERIOD_LOCK + " " + PHOTO_LOCK],
            with_cine_qc(st["qc"])))
    return out


def score_cards(cu):
    """One card per music cue. Suno takes two fields, so the prompt body is the
    Style text alone — Copy prompt goes straight into it — and Exclude Styles
    rides on the card's second copy button rather than being buried in prose."""
    settings = cu.get("settings", "")
    body = (f"Side of the perception line: {cu['side']}. {cu['job']}\n"
            f"Suno custom mode, instrumental toggle ON. "
            f"{settings}\n"
            f"Generate {cu.get('generate', 'two minutes')} and cut "
            f"{cu['length']} from it — Suno will not hand you a short cue.\n\n"
            f"TREATMENT, after the download, in your DAW — Suno ignores mixing "
            f"jargon, so none of this belongs in the Style field: "
            f"{cu['treatment']}\n\n"
            f"VERIFY: {cu['qc']}")
    return [card(
        f"{cu['id']}", f"{cu['id']} — {cu['name']}  [{cu['tc']}]",
        cu.get("model", "Suno v4.5+"), "—", cu["length"], cu.get("takes", 6),
        [cu["style"]], body,
        alt_copy={"label": "Copy Exclude", "text": cu["exclude"]},
        spec_md=(f"**Tool:** {cu.get('model', 'Suno v4.5+')} · "
                 f"**Cue:** {cu['tc']} · **Length:** {cu['length']} · "
                 f"**Auditions:** {cu.get('takes', 6)} · "
                 f"**Vocals:** {cu.get('vocals', 'instrumental')}"),
        spec_pills=[cu.get("model", "Suno v4.5+"), cu["tc"], cu["length"],
                    f"{cu.get('takes', 6)} auditions",
                    cu.get("vocals", "instrumental")])]


def location_cards(lo):
    if LOC_STYLE == "plate":
        return [card(
            f"{lo['id']}/{slug(st['name'])}",
            f"{lo['name']} / {st['name']}  [{lo['id']}]",
            "Soul Cinema", "21:9", "2K", 4,
            # A state may re-date the plate. A floor dressed for an earlier
            # year must not inherit the project's year or its period lock, and
            # its anchor may be a different object in that dressing.
            [f"{LOC_VIEW} {st.get('world') or WORLD_LINE} {lo['geo']}",
             f"{st['delta']} Include "
             f"{st.get('anchor') or lo['anchor']}.",
             f"{LIGHTING[st['light']]} {PLATE_LOOK} "
             f"{st.get('lock') or PLATE_LOCK}"],
            st["qc"]) for st in lo["states"]]
    out = []
    for st in lo["states"]:
        out.append(card(
            f"{lo['id']}/{slug(st['name'])}",
            f"{lo['name']} / {st['name']}  [{lo['id']}]",
            "Soul Cinema", "21:9", "2K", 4,
            [f"{LOC_VIEW} {WORLD_LINE} {lo['geo']}",
             f"State of the location in this plate: {st['delta']} "
             f"The anchor object of this location — {lo['anchor']} — is "
             f"clearly visible and correctly placed.",
             LIGHTING[st["light"]],
             st.get("look") or CINE_LOOK,
             (PERIOD_LOCK + (" " + CROWD_LINE if st.get("crowd") else "")
              + " " + PHOTO_LOCK)],
            # A state that overrides the house look must not inherit the
            # house look's QC line, or the note contradicts the prompt.
            st["qc"] if st.get("look") else with_cine_qc(st["qc"])))
    return out


def look_cards(lp):
    return [card(
        f"{lp['id']}",
        f"LOOK / {lp['name']}  [{lp['id']}]",
        lp.get("model", "Soul Cinema"), "21:9", "2K", 4,
        [lp["prompt"],
         PERIOD_LOCK],
        lp["qc"])]


# ---------------------------------------------------------------- shot factory
#
# Every shot record emits up to three cards: a first-frame image plate, a
# last-frame image plate (skipped when frames.last is absent), and a
# 15-section Hell Grind video card. Cast / mover / prop references degrade
# from `@callout` to full-prose subject when the ELEMENTS registry says the
# reference is not `ready`, and every card stamps DEGRADED on its QC line so
# a rerun target is visible from the tracker without digging.


def _element(mid):
    """Return the ELEMENTS row for a matrix id, or None. The `_doc` key is a
    comment on the registry itself, not a real element."""
    if not mid or mid.startswith("_"):
        return None
    row = (ELEMENTS or {}).get(mid)
    return row if isinstance(row, dict) else None


def _char(cid):
    return next((c for c in CHARACTERS if c["id"] == cid), None)


def _char_state(ch, sid):
    if not ch or not sid:
        return None
    return next((s for s in ch.get("states", []) if s.get("id") == sid), None)


def _loc(lid):
    return next((lo for lo in LOCATIONS if lo["id"] == lid), None)


def _loc_state(lo, name):
    if not lo or not name:
        return None
    return next((s for s in lo.get("states", []) if s.get("name") == name),
                None)


def _prop(pid):
    return next((p for p in PROPS if p["id"] == pid), None)


def _mover(mid):
    return next((m for m in MOVERS if m["id"] == mid), None)


def cast_reference(cast_ref):
    """(phrase, degraded_flag) for one cast entry.

    When the character has a ready ELEMENT, returns `@Callout in <state name>`
    — no wardrobe prose, matching the SHOT_PROMPT_TEMPLATE rule that a plate
    already carries the costume. When the element is missing or not yet ready,
    falls back to the full identity + wardrobe subject with the state delta,
    and flags the shot as degraded so the tracker can surface a rerun."""
    ch = _char(cast_ref["id"])
    if ch is None:
        return f"[UNKNOWN CHARACTER {cast_ref['id']}]", True
    st = _char_state(ch, cast_ref.get("state"))
    state_label = st["name"] if st else ""
    el = _element(cast_ref["id"])
    if el and el.get("status") == "ready":
        cite = el["callout"]
        return (f"{cite} in {state_label}" if state_label else cite), False
    prose = subject(ch)
    if st:
        prose += (f", in the {state_label} state: "
                  f"{(st.get('delta') or '').strip()}")
    return prose, True


def aux_reference(mid, lookup):
    """Ready → callout; otherwise → the item's own base description. `lookup`
    is _mover or _prop."""
    el = _element(mid)
    item = lookup(mid)
    if el and el.get("status") == "ready":
        return el["callout"], False
    if item is None:
        return f"[UNKNOWN {mid}]", True
    return (item.get("base") or item.get("name") or mid).strip(), True


def cast_line(shot):
    """The 'EXACTLY N CHARACTERS — NO DUPLICATES' line for SCENE CONTEXT.
    Singular grammar when N=1 so the prompt does not read 'Exactly 1
    characters'."""
    cast = shot.get("cast") or []
    if not cast:
        return "No named characters in frame."
    names = [(_char(c["id"]) or {}).get("name", c["id"]) for c in cast]
    noun = "character" if len(names) == 1 else "characters"
    return (f"Exactly {len(names)} {noun} — no duplicates: "
            f"{', '.join(names)}.")


def _plate_ref(loc):
    """A location name in QC copy without the double-article glitch:
    'the The sky tear plate' becomes 'the The sky tear plate' → 'the sky
    tear plate' by only prefixing 'the' when the name doesn't start with
    it already."""
    if not loc:
        return "the named location plate"
    n = loc.get("name") or ""
    return (f"the {n} plate" if not n.lower().startswith("the ")
            else f"{n} plate")


def geo_map_line(loc):
    """LOCATION MAP one-liner, sourced from GEO so a director ruling is
    changed in one place and 101 prompts follow. Falls back to the
    location's own anchor when the location has no GEO zone attached
    (e.g. LOC-SKY is a look, not a resort zone)."""
    if not loc:
        return ""
    zid = loc.get("geo_zone")
    zones = (GEO or {}).get("zones") or {}
    z = zones.get(zid) if zid else None
    if not z:
        return (f"{(loc.get('anchor') or 'The anchor').capitalize()} sits "
                f"inside the space described in the location plate.")
    nb = [zones.get(n, {}).get("name", n) for n in (z.get("neighbours") or [])]
    nb_line = ", ".join(nb) if nb else "the rest of the resort"
    return (f"{z['anchor'].capitalize()} sits {z.get('compass', '')} "
            f"(elevation: {z.get('elevation', '?')}); adjacent to {nb_line}.")


def frame_plate(shot, which):
    """Emit the first- or last-frame image plate for one shot.

    Returns None when the requested frame is not defined — a pure locked
    plate can skip the last-frame card by omitting frames.last from the
    shot record."""
    frames = shot.get("frames") or {}
    text = (frames.get(which) or "").strip()
    if not text:
        return None
    loc = _loc(shot["loc"])
    lo_state = _loc_state(loc, shot.get("loc_state"))
    cam = shot.get("camera") or {}
    light_id = shot.get("light")

    cast_bits, degraded = [], False
    for c in shot.get("cast", []):
        phr, deg = cast_reference(c)
        cast_bits.append(phr)
        degraded = degraded or deg

    aux_bits = []
    for mid in shot.get("movers", []):
        phr, deg = aux_reference(mid, _mover)
        aux_bits.append(phr)
        degraded = degraded or deg
    for pid in shot.get("props", []):
        phr, deg = aux_reference(pid, _prop)
        aux_bits.append(phr)
        degraded = degraded or deg

    para = []
    para.append(
        f"Photorealistic {which}-frame plate for shot {shot['id']} — "
        f"{shot.get('title', '')} — a single 21:9 anamorphic film still "
        f"captured at the {'opening' if which == 'first' else 'closing'} "
        f"instant of a {shot.get('dur', 0):.1f}-second take, no cuts before "
        f"or after this frame.")
    if cast_bits:
        para.append("In frame: " + "; ".join(cast_bits) + ".")
    if aux_bits:
        para.append("Also present: " + "; ".join(aux_bits) + ".")
    if loc:
        para.append(
            f"Location: {loc['name']} in the {shot.get('loc_state', '')} "
            f"state; carry only the space and texture from the location "
            f"plate — do not inherit its composition, angle or pose. "
            f"Geography lock: {geo_map_line(loc)}")
    para.append(f"Blocking at this exact instant: {text}")
    para.append(
        f"Camera: {cam.get('lens', '50mm')}, at {cam.get('height', 'chest')} "
        f"height, {cam.get('angle', 'level')} angle. This card renders the "
        f"still frame only — the {cam.get('move', 'locked')} movement of the "
        f"take is on the companion video card.")
    if light_id and light_id in LIGHTING:
        para.append(LIGHTING[light_id])
    para.append(CINE_LOOK)
    para.append(PERIOD_LOCK
                + (" " + CROWD_LINE if lo_state and lo_state.get("crowd")
                   else "")
                + " " + PHOTO_LOCK)

    qc = with_cine_qc(
        f"Geography matches {_plate_ref(loc)} and its GEO zone; the anchor "
        f"object is present and correctly placed; exactly the named cast in "
        f"frame; "
        f"{shot.get('constraints', 'no other constraints noted').rstrip('. ')}")
    if degraded:
        qc += (" [DEGRADED: one or more references are pending / ineligible "
               "and rendered from prose — re-run once the element lands.]")

    return card(
        f"{shot['id']}/{which}",
        (f"{shot['id']} — {shot.get('title', '')} / {which}-frame plate  "
         f"[{shot.get('seq', '?')} · {shot.get('tc', '?')}]"),
        "Soul Cinema", "21:9", "2K", 4,
        para, qc)


def video_card(shot):
    """Emit the 15-section Hell Grind video card for one shot. The prompt
    body is one long string that pastes directly into Seedance's box in the
    canonical section order."""
    loc = _loc(shot["loc"])
    lo_state = _loc_state(loc, shot.get("loc_state"))
    cam = shot.get("camera") or {}
    dur = float(shot.get("dur") or 0.0)
    method = shot.get("method", "i2v")
    light_id = shot.get("light")

    cast_bits, degraded = [], False
    for c in shot.get("cast", []):
        phr, deg = cast_reference(c)
        cast_bits.append(phr)
        degraded = degraded or deg
    aux_bits = []
    for mid in shot.get("movers", []):
        phr, deg = aux_reference(mid, _mover)
        aux_bits.append(phr)
        degraded = degraded or deg
    for pid in shot.get("props", []):
        phr, deg = aux_reference(pid, _prop)
        aux_bits.append(phr)
        degraded = degraded or deg

    action = shot.get("action") or []
    action_block = ("\n".join(f"{a['t']}s — {a['what']}" for a in action)
                    or "0.0s–end — held.")

    dlg = shot.get("dialogue") or {}
    dlg_line = ""
    if dlg.get("line"):
        speaker = (_char(dlg.get("who")) or {}).get("name",
                                                    dlg.get("who", ""))
        dlg_line = f'Voice ({speaker}): "{dlg["line"]}"'

    sfx = ", ".join(shot.get("sfx") or [])

    ref_lines = []
    for b in cast_bits:
        ref_lines.append(f"— {b} for character reference.")
    for b in aux_bits:
        ref_lines.append(f"— {b} for object reference.")
    if loc:
        ref_lines.append(
            f"— {loc['name']} for location reference in the "
            f"{shot.get('loc_state', '')} state — take only the space and "
            f"the texture; do not use as a starting frame, do not inherit "
            f"composition, angle or pose.")
    active_refs = "\n".join(ref_lines) if ref_lines else "— none"

    audio_line = ("Diegetic only unless scored. "
                  + (dlg_line or "No dialogue.")
                  + ((" SFX: " + sfx + ".") if sfx else "")
                  + " No music in-generation unless intentional.")

    positive = ((shot.get("constraints", "").rstrip(". ") + ". ")
                if shot.get("constraints") else "")
    positive += (
        f"Camera stays on the {cam.get('side', 'same')} side of the space "
        f"for the whole take. Photoreal, {dur:.1f}s"
        + (" with dialogue as written" if dlg_line else ", SFX only")
        + ", NO CUT.")

    sections = [
        ("SCENE CONTEXT",
         f"{cast_line(shot)} {shot.get('intent', '').strip()} "
         f"{dur:.1f}-second continuous take, no cuts."),
        ("ACTIVE REFERENCES", active_refs),
        ("LOCATION MAP", geo_map_line(loc)),
        ("FIRST FRAME AND SPATIAL BLOCKING",
         (shot.get("frames") or {}).get("first", "")),
        ("LENS AND CAMERA WORK",
         f"{cam.get('lens', '50mm')}, {cam.get('angle', 'level')} angle, "
         f"camera at {cam.get('height', 'chest')} height, {dur:.1f}s real "
         f"time."),
        ("OPTICS",
         "Focus plan: the named cast reads in critical focus at the anchor "
         "object; background falls off gently; no rack focus unless the "
         "action timing calls for one."),
        ("CAMERA",
         f"{cam.get('move', 'locked')}. Never drone, whip, zoom or "
         "handheld shake beyond what is specified above."),
        ("ACTION TIMING", action_block),
        ("PHYSICS",
         shot.get("physics",
                  "Weights, contacts and sprays behave with real inertia.")),
        ("LIGHT",
         (LIGHTING.get(light_id, "").strip()
          + ((" " + CROWD_LINE)
             if lo_state and lo_state.get("crowd") else ""))),
        ("AUDIO", audio_line),
        ("CHARACTER ACTING",
         shot.get("acting",
                  "No named acting — this beat is a still or has no named "
                  "cast.")),
        ("STYLE", CINE_LOOK),
        ("QUALITY",
         "4K detail, no jitter, no flicker; faces and kits stay exactly "
         "their references at every distance. Aspect 21:9. "
         + with_cine_qc("Anamorphic film look present through the whole "
                        "take")),
        ("POSITIVE CONSTRAINTS", positive),
        ("FLOW (edit brief only — do not paste into the prompt body)",
         f"In: {(shot.get('flow') or {}).get('in', '—')}\n"
         f"Out: {(shot.get('flow') or {}).get('out', '—')}"),
    ]
    body = "\n\n".join(f"{h}\n\n{t}" for h, t in sections if t and t.strip())

    qc = ("Blocking matches the first-frame plate and resolves into the "
          "last-frame plate; timing lands on the marks; no unspecified "
          "motion; the camera stays on the same side for the whole take.")
    if degraded:
        qc += (" [DEGRADED: one or more references are pending / ineligible "
               "and rendered from prose — re-run once the element lands.]")

    return card(
        f"{shot['id']}/video",
        (f"{shot['id']} — {shot.get('title', '')} / video  "
         f"[{shot.get('seq', '?')} · {shot.get('tc', '?')} · {dur:.1f}s]"),
        "Seedance 2.0 Fast", "21:9", f"{dur:.1f}s", 4,
        [body], qc,
        spec_md=(f"**Model:** Seedance 2.0 Fast · **AR:** 21:9 · "
                 f"**Duration:** {dur:.1f}s · **Method:** {method} · "
                 f"**Takes:** 4 · **Attachments:** NONE (virgin)"),
        spec_pills=["Seedance 2.0 Fast", "AR 21:9", f"{dur:.1f}s", method,
                    "4 takes", "virgin — no attachments"])


def shot_cards(shot):
    """Up to three cards per shot: first-frame plate, last-frame plate
    (optional), and the 15-section video card. A frame plate is emitted
    only when its blocking text is defined on the shot record."""
    out = []
    for which in ("first", "last"):
        c = frame_plate(shot, which)
        if c is not None:
            out.append(c)
    out.append(video_card(shot))
    return out


# --------------------------------------------------------------------------
# LEAN TIER FACTORIES — sprint mode (see production/SPRINT_2026-08-10_SPEC.md)
#
# These emit tightly scoped cards under a T#-tagged title so a contest-window
# project can render only what is mandatory to ship the film. The old
# factories above stay in place for the historical projects — nothing is
# removed, sprint projects opt in via their `sections` list in project.json.
# Every tier body follows the shape locked in the sprint spec: no re-pasted
# wardrobe on shot cards, no cine block on grey-background sheets, no
# aspect/resolution/lens metadata inside the sentence, one period lock line,
# one compliance line, one photo lock line.
# --------------------------------------------------------------------------

# These read the active project's doctrine so a second film can carry a
# different era and camera package. `lean_*` doctrine keys are the short
# sprint-card forms; they fall back to the long-form locks when unset.
def _doc(key, fallback=""):
    return ((PROJECT.get("doctrine") or {}).get(key) or fallback)


def _lean_period(): return _doc("lean_period_lock", PERIOD_LOCK)
def _lean_adults(): return _doc("lean_crowd_line", CROWD_LINE)
def _lean_photo():  return _doc("lean_photo_lock", PHOTO_LOCK)


def _lean_text():
    """Opt-in readable-text guard for in-world cards (plates and shots).

    A set dressed in flyers, posters and graffiti is a text magnet, and
    generated lettering is the fastest way to lose a frame. Projects whose
    world contains signage set `lean_text_lock`; projects that do not simply
    omit the key and their cards are unchanged."""
    return _doc("lean_text_lock", "")


def _compliance(*parts):
    """Join the compliance sentences that are non-empty, so an unset doctrine
    key never leaves a double space or a dangling clause."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def _lean_cine():
    """One-sentence camera package for cinematic cards only (T2 plates, T3
    frame plates, T4 videos) — never on identity sheets or product plates."""
    return _doc("lean_cine_look", _LEAN_CINE_DEFAULT)


_LEAN_CINE_DEFAULT = ("Panavision anamorphic look on 35mm-equivalent film: "
                      "gentle oval bokeh, subtle horizontal flare only where "
                      "hard light hits the lens, organic 35mm grain, "
                      "Kodachrome-leaning color.")


def _lean_wardrobe(ch):
    """Wardrobe collapsed into a single skin-outward sentence for the T1 card.
    Concatenates the addressable slots with single spaces so the sentence
    reads as one description rather than eight."""
    parts = [((ch.get("wardrobe") or {}).get(k) or "").strip()
             for k in WARDROBE_SLOTS]
    parts = [p for p in parts if p]
    return " ".join(parts) or "(wardrobe unspecified)"


def _lean_zone(loc):
    """One compass fact for a location, drawn from GEO so it stays canonical."""
    if not loc:
        return ""
    zid = loc.get("geo_zone")
    z = (GEO or {}).get("zones", {}).get(zid) if zid else None
    if not z:
        return ""
    return (f"Geography: {z.get('compass', '').strip()} "
            f"(elevation {z.get('elevation', '?')}).").replace("  ", " ")


def _lean_camera(shot):
    cam = shot.get("camera") or {}
    bits = [cam.get("lens", ""), f"{cam.get('height','')} height",
            cam.get("angle", ""), cam.get("move", "")]
    side = (cam.get("side") or "").strip()
    line = ", ".join(b for b in bits if b and b.strip() != " height")
    if side:
        line += f", 180-line: {side}"
    return line


def _lean_light(shot):
    """One-line light state pulled from LIGHTING if the shot names one.
    LIGHTING values may be plain strings or dicts with `desc`/`name` — accept
    either so matrices from different films stay compatible."""
    lid = shot.get("light")
    if not lid:
        return ""
    lit = (LIGHTING or {}).get(lid)
    if isinstance(lit, str):
        desc = lit.strip()
    elif isinstance(lit, dict):
        desc = (lit.get("desc") or lit.get("name") or "").strip()
    else:
        desc = ""
    desc = desc.rstrip(".")
    return f"Light: {desc}." if desc else ""


def _lean_cast_reference(cast_ref):
    """Sprint-mode cast reference.

    Ready → `@Callout in <state>` (unchanged from cast_reference).
    Not ready → short pointer: `NAME in <state> [see T1 · NAME / SOUL]`.
    The full character subject stays where it belongs, on the T1 card —
    shot cards must not re-teach the model who this person is."""
    ch = _char(cast_ref["id"])
    if ch is None:
        return f"[UNKNOWN CHARACTER {cast_ref['id']}]", True
    st = _char_state(ch, cast_ref.get("state"))
    state_label = st["name"] if st else ""
    el = _element(cast_ref["id"])
    if el and el.get("status") == "ready":
        cite = el["callout"]
        return (f"{cite} in {state_label}" if state_label else cite), False
    name = ch.get("name", cast_ref["id"])
    state_bit = f" in {state_label}" if state_label else ""
    return (f"{name}{state_bit} [see T1 · {name} / SOUL — element not ready]",
            True)


def _lean_aux_reference(mid, lookup, kind):
    """Sprint-mode mover/prop reference. Ready → callout; else → name +
    pointer to the T5 (props) or matrix row. `kind` labels the pointer."""
    el = _element(mid)
    item = lookup(mid)
    if el and el.get("status") == "ready":
        return el["callout"], False
    if item is None:
        return f"[UNKNOWN {mid}]", True
    name = item.get("name") or mid
    return f"{name} [see {kind} sheet {mid} — element not ready]", True


def _lean_cast_line(shot):
    """CAST line — callouts + state labels only; falls back to a short
    T1-pointer when the element is not ready, stamped [DEGRADED]."""
    cast = shot.get("cast") or []
    if not cast:
        return "CAST: no named characters in frame.", False
    bits, degraded = [], False
    for c in cast:
        phr, deg = _lean_cast_reference(c)
        bits.append(phr)
        degraded = degraded or deg
    return "CAST: " + "; ".join(bits) + ".", degraded


def _lean_aux_line(shot):
    """Movers and props in the shot, callouts when ready, short pointers
    when not."""
    out, degraded = [], False
    for mid in shot.get("movers", []):
        p, d = _lean_aux_reference(mid, _mover, "mover")
        out.append(p); degraded = degraded or d
    for pid in shot.get("props", []):
        p, d = _lean_aux_reference(pid, _prop, "T5 · prop")
        out.append(p); degraded = degraded or d
    if not out:
        return "", False
    return "PROPS/MOVERS: " + "; ".join(out) + ".", degraded


def _lean_qc(paras, extras=""):
    """A tight QC line built from the payload so an operator can eyeball a
    render against it — no boilerplate, just what the card promised."""
    core = "; ".join(x for x in extras.split(" | ") if x).strip("; ")
    return (core or "the prompt reads as one shot with no dropped elements") + "."


def t1_soul_cards(ch):
    """T1 · SOUL — single-face portrait per character, one card each.

    This is the image a Higgsfield Element is minted from. Chest-up, one face
    on screen, grey background, no cine grade — anything more will teach the
    animator to drift by scene five (per Adil's tutorial and our own review
    of the pre-sprint sheet cards)."""
    sex = stated_sex(ch)
    who = f"{sex} character" if sex else "character"
    base = (ch.get("base") or "").strip().rstrip(".")
    wardrobe = _lean_wardrobe(ch)
    paras = [
        f"Portrait photograph of one {who}, chest up, facing camera, "
        "neutral relaxed expression, eyes level with the lens, on a clean "
        "deep neutral grey (#3a3a3c) seamless studio background.",

        f"IDENTITY: {base}.",

        f"WARDROBE (skin outward): {wardrobe}",

        "LIGHT & LENS: soft even studio key from front-left with gentle "
        "fill, catch-light in both eyes, photographed at eye level on an "
        "85mm portrait lens, tack-sharp focus on the eyes.",

        f"{_lean_adults()} {_lean_period()} {_lean_photo()}",
    ]
    return [card(
        f"{ch['id']}/T1-soul",
        f"T1 · {ch['name']} / SOUL identity  [{ch['id']}]",
        "Soul 2.0", "1:1", "4K", 4, paras,
        _lean_qc(paras,
                 "one face on screen | grey background | 85mm portrait | "
                 "wardrobe reads as one sentence | no cine grade or grain"))]


def t1f_faction_cards(fa):
    """T1F · FACTION — one crowd styling sheet per faction.

    A background crowd does not need the three-view turnaround that a named
    character gets; it needs one plate that pins the creature register and the
    wardrobe rule so every shot the faction appears in dresses the same way.
    Group framing rather than a single figure, because the faction is only ever
    seen as a mass."""
    base = (fa.get("base") or "").strip().rstrip(".")
    wardrobe = (fa.get("wardrobe") or "").strip().rstrip(".")
    states = fa.get("states") or []
    state_line = ""
    if states:
        bits = "; ".join(f"{s['name']} — {(s.get('delta') or '').rstrip('.')}"
                         for s in states if s.get("name"))
        state_line = f"STATES this faction is cut in: {bits}."
    paras = [
        f"Photorealistic reference group photograph of {fa['name']} — six to "
        "eight of them together, standing, three-quarter to camera, full "
        "figures head to feet in frame, on a clean deep neutral grey "
        "(#3a3a3c) seamless studio background.",

        f"WHO THEY ARE: {base}.",

        f"WARDROBE RULE (applies to every member): {wardrobe}.",

        state_line,

        "LIGHT & LENS: flat even studio light, photographed from chest height "
        "on a 50mm lens, camera square to the group, no wide-angle "
        "distortion, every garment and material detail sharp.",

        f"{_lean_adults()} {_lean_period()} {_lean_photo()}",
    ]
    paras = [p for p in paras if p]
    return [card(
        f"{fa['id']}/T1F-faction",
        f"T1F · {fa['name']} / crowd sheet  [{fa['id']}]",
        "Soul 2.0", "3:4", "4K", 4, paras,
        _lean_qc(paras,
                 "six to eight figures | one consistent creature register | "
                 "wardrobe rule visible on every member | grey background | "
                 "no cine grade or grain"))]


def t2_plate_cards(loc):
    """T2 · PLATE — three-quarter oblique establishing plate per named state.

    Emits one card per LOCATION state so a location that reads morning vs
    evening vs storm carries three plates. Adds a reverse-angle variant when
    the location matrix flags `needs_reverse: true` — the fix for the
    dialogue-scene background drift Adil's tutorial demonstrates."""
    out = []
    states = loc.get("states") or [{"name": "master", "delta": ""}]
    zone_line = _lean_zone(loc)
    anchor = (loc.get("anchor") or "").strip().rstrip(".")
    needs_rev = bool(loc.get("needs_reverse"))
    for st in states:
        state_name = (st.get("name") or "master").strip()
        delta = (st.get("delta") or "").strip().rstrip(".")
        delta_line = f" State delta: {delta}." if delta else ""
        loc_body = [
            f"Photorealistic three-quarter oblique establishing photograph "
            f"of {loc['name']} at {state_name}, camera set back and slightly "
            f"raised so the space reads with real depth.{delta_line}",

            f"ANCHOR: {anchor}." if anchor else "",

            zone_line,

            f"CAMERA: {_lean_cine()}",

            _compliance(_lean_adults(), _lean_period(), _lean_text()),
        ]
        loc_body = [p for p in loc_body if p]
        out.append(card(
            f"{loc['id']}/T2-plate/{slug(state_name)}",
            f"T2 · {loc['name']} / plate — {state_name}  [{loc['id']}]",
            "Soul Cinema", "21:9", "4K", 4, loc_body,
            _lean_qc(loc_body,
                     "three-quarter oblique | anchor object present | "
                     "period-lock holds | anamorphic look present")))

        if needs_rev:
            rev_body = [
                f"Photorealistic three-quarter oblique reverse-angle "
                f"photograph of {loc['name']} at {state_name}, camera set "
                f"opposite the master plate so the two frames intercut "
                f"across the 180-line without background drift.",

                f"ANCHOR (reverse side): {anchor}." if anchor else "",

                zone_line,

                f"CAMERA: {_lean_cine()}",

                _compliance(_lean_adults(), _lean_period(), _lean_text()),
            ]
            rev_body = [p for p in rev_body if p]
            out.append(card(
                f"{loc['id']}/T2-plate-rev/{slug(state_name)}",
                f"T2 · {loc['name']} / plate reverse — {state_name}  "
                f"[{loc['id']}]",
                "Soul Cinema", "21:9", "4K", 4, rev_body,
                _lean_qc(rev_body,
                         "reverse angle of the master | same wall/edge "
                         "geometry | intercuts cleanly")))
    return out


def t5_prop_cards(pr):
    """T5 · PROP — one product-style sheet per hero prop.

    Emitted only for props story-load-bearing enough to sit on a matrix row.
    Grey background, product lens, no cine grade — this is a reference, not
    a frame from the film."""
    base = (pr.get("base") or pr.get("name") or "").strip().rstrip(".")
    paras = [
        f"Product-style photograph of one {pr['name']}, centered on a "
        "clean deep neutral grey (#3a3a3c) seamless studio surface.",

        f"OBJECT: {base}.",

        "LIGHT & LENS: soft even product-photography light from upper "
        "left, photographed on a 100mm product lens, camera square to the "
        "object, every material detail sharp.",

        f"{_lean_period()} No text, labels, logos or graphics on the "
        "object unless named in the description.",
    ]
    return [card(
        f"{pr['id']}/T5-prop",
        f"T5 · {pr['name']} / prop sheet  [{pr['id']}]",
        "Nano Banana 2", "1:1", "4K", 4, paras,
        _lean_qc(paras,
                 "one object on screen | grey background | product lens | "
                 "no film grain | no unnamed text/logos"))]


def _lean_shot_head(shot, kind_label):
    """Shared head paragraphs for T3/T3L/T4 — location, cast, camera, physics."""
    loc = _loc(shot["loc"])
    lo_state = shot.get("loc_state") or ""
    plate_hint = (f"see plate {loc['id']}/T2-plate/{slug(lo_state)}" if loc
                  else "")
    loc_bit = (f"LOCATION: at {loc['name']} [{lo_state}] — {plate_hint}."
               if loc else "LOCATION: unspecified.")
    zone_bit = _lean_zone(loc)
    cast_line, deg_cast = _lean_cast_line(shot)
    aux_line, deg_aux = _lean_aux_line(shot)
    degraded = deg_cast or deg_aux
    cam = _lean_camera(shot)
    light_bit = _lean_light(shot)
    return {
        "loc_bit": loc_bit,
        "zone_bit": zone_bit,
        "cast_line": cast_line,
        "aux_line": aux_line,
        "cam": cam,
        "light_bit": light_bit,
        "degraded": degraded,
    }


def _lean_beats(shot):
    """BEATS section: '-  <t>: <what>.' bullets, capped at 5 for readability.
    Seedance reads a small handful of ordered beats better than a paragraph."""
    beats = shot.get("action") or []
    lines = []
    for b in beats[:5]:
        t = (b.get("t") or "").strip()
        what = (b.get("what") or "").strip().rstrip(".")
        if not what:
            continue
        prefix = f"- {t}: " if t else "- "
        lines.append(f"{prefix}{what}.")
    if not lines:
        return "BEATS: continuous action per the intent line."
    return "BEATS:\n" + "\n".join(lines)


def _shot_title_tag(shot):
    """A short title fragment for a shot card — the intent's first sentence
    or the shot title, whichever is shorter and non-empty."""
    for src in (shot.get("title"), shot.get("intent")):
        if not src:
            continue
        first = (src.split(". ")[0]).strip()
        if 6 <= len(first) <= 80:
            return first
    return shot.get("id") or "shot"


def t3_first_cards(shot):
    """T3 · FIRST — first-frame image plate, emitted only when the shot has
    `frames.first` blocking text worth seeding."""
    text = ((shot.get("frames") or {}).get("first") or "").strip()
    if not text:
        return []
    h = _lean_shot_head(shot, "first")
    paras = [
        f"Photorealistic 21:9 still — first frame of shot {shot['id']}: "
        f"{text.rstrip('.')}.",
        h["loc_bit"],
        h["zone_bit"],
        h["cast_line"],
        h["aux_line"],
        f"CAMERA: {h['cam']}. {_lean_cine()}",
        h["light_bit"],
        _compliance(_lean_adults(), _lean_period(), _lean_text()),
    ]
    paras = [p for p in paras if p]
    tag = "[DEGRADED elements] " if h["degraded"] else ""
    return [card(
        f"{shot['id']}/T3-first",
        f"T3 · {shot['id']} / first frame · {tag}{_shot_title_tag(shot)}",
        "Soul Cinema", "21:9", "4K", 4, paras,
        _lean_qc(paras,
                 "composition matches frames.first | "
                 "callouts and state labels present | anamorphic look present"))]


def t3_last_cards(shot):
    """T3L · LAST — last-frame image plate. Only emitted when the shot's
    `frames.last` blocking is defined AND differs from `frames.first`."""
    frames = shot.get("frames") or {}
    first = (frames.get("first") or "").strip()
    text = (frames.get("last") or "").strip()
    if not text or text == first:
        return []
    h = _lean_shot_head(shot, "last")
    paras = [
        f"Photorealistic 21:9 still — last frame of shot {shot['id']}: "
        f"{text.rstrip('.')}.",
        h["loc_bit"],
        h["zone_bit"],
        h["cast_line"],
        h["aux_line"],
        f"CAMERA: {h['cam']}. {_lean_cine()}",
        h["light_bit"],
        _compliance(_lean_adults(), _lean_period(), _lean_text()),
    ]
    paras = [p for p in paras if p]
    tag = "[DEGRADED elements] " if h["degraded"] else ""
    return [card(
        f"{shot['id']}/T3L-last",
        f"T3L · {shot['id']} / last frame · {tag}{_shot_title_tag(shot)}",
        "Soul Cinema", "21:9", "4K", 4, paras,
        _lean_qc(paras,
                 "composition matches frames.last | "
                 "handoff pose readable | anamorphic look present"))]


def t4_video_cards(shot):
    """T4 · VIDEO — the Seedance shot prompt. One card per shot, always.

    Nine tight paragraphs: setup, location, cast, camera, beats, physics,
    acting, sound (reference), compliance. This is the shape that survives
    manual paste into Cinema Studio without dead prose the model ignores."""
    intent = (shot.get("intent") or shot.get("title") or "").strip().rstrip(".")
    h = _lean_shot_head(shot, "video")
    physics = (shot.get("physics") or "").strip().rstrip(".")
    acting  = (shot.get("acting")  or "").strip().rstrip(".")
    sfx = shot.get("sfx") or []
    sfx_line = "SOUND (edit reference, not generation): " + ", ".join(sfx) + "." \
        if sfx else ""
    constraints = (shot.get("constraints") or "").strip().rstrip(".")
    constraints_line = f"CONSTRAINTS: {constraints}." if constraints else ""

    paras = [
        f"SETUP: {intent}.",
        h["loc_bit"],
        h["zone_bit"],
        h["cast_line"],
        h["aux_line"],
        f"CAMERA: {h['cam']}. {_lean_cine()}",
        h["light_bit"],
        _lean_beats(shot),
        f"PHYSICS: {physics}." if physics else "",
        f"ACTING: {acting}." if acting else "",
        sfx_line,
        constraints_line,
        _compliance(_lean_adults(), _lean_period(), _lean_text(),
                    "Continuous shot, no cuts."),
    ]
    paras = [p for p in paras if p]
    tag = "[DEGRADED elements] " if h["degraded"] else ""
    return [card(
        f"{shot['id']}/T4-video",
        f"T4 · {shot['id']} / video · {tag}{_shot_title_tag(shot)}",
        "Seedance 2.0 Fast", "21:9", "1080p", 4, paras,
        _lean_qc(paras,
                 "one continuous shot | callouts and state labels present | "
                 "beats time correctly | anamorphic look present | "
                 "period-lock holds"))]


def build_sections():
    """Returns [(md_title, short_name, blurb, [card dicts])].

    Driven by the active project's `sections` list, so a project can drop a
    section it has no use for simply by omitting it.
    """
    factories = {
        "SCORE": (lambda: SCORE, score_cards),
        "CHARACTERS": (lambda: CHARACTERS, character_cards),
        "FACTIONS": (lambda: FACTIONS, faction_cards),
        "CREATURES": (lambda: CREATURES, creature_cards),
        "PROPS": (lambda: PROPS, prop_cards),
        "MOVERS": (lambda: MOVERS, mover_cards),
        "LOCATIONS": (lambda: LOCATIONS, location_cards),
        "LOOK_PLATES": (lambda: LOOK_PLATES, look_cards),
        "SHOTS": (lambda: SHOTS, shot_cards),
        # Lean sprint factories (see production/SPRINT_2026-08-10_SPEC.md).
        "T1_SOULS":  (lambda: CHARACTERS, t1_soul_cards),
        "T1F_FACTIONS": (lambda: FACTIONS, t1f_faction_cards),
        "T2_PLATES": (lambda: LOCATIONS,  t2_plate_cards),
        "T5_PROPS":  (lambda: PROPS,      t5_prop_cards),
        "T3_FIRST":  (lambda: SHOTS,      t3_first_cards),
        "T3L_LAST":  (lambda: SHOTS,      t3_last_cards),
        "T4_VIDEO":  (lambda: SHOTS,      t4_video_cards),
        # Project-independent house standards, not render orders.
        "LAYOUTS":   (lambda: LAYOUT_ORDER, layout_header_cards),
    }
    out = []
    for sec in PROJECT.get("sections", DEFAULT_SECTIONS):
        entry = factories.get(sec["collection"])
        if entry is None:
            continue
        get_items, factory = entry
        cards = [c for item in get_items() for c in factory(item)]
        out.append((sec["md_title"], sec["short"], sec["blurb"], cards))
    return out


def render_markdown(sections, today):
    slug = PROJECT["slug"]
    order = " → ".join(s[1].lower() for s in sections)
    header = f"""# SHOW BIBLE PROMPTS — {PROJECT['title']}

**GENERATED FILE — do not hand-edit.** Regenerate with
`python3 production/generator/generate_prompts.py --project {slug}` after
editing `production/generator/projects/{slug}/matrix_data.json` (directly,
or via the tracker app: `python3 production/generator/app.py`).

Generated {today} from {PROJECT['source_note']}.
Every card is a **virgin prompt** — fully self-contained, zero attachments —
per the `virgin-prompt-standard` skill. Sections are in Day-1 dependency
order: {order}.

Universal manual-input rules:
- Set model, aspect ratio and resolution as **platform parameters**, never in text.
- Run the stated take count; pick by the QC line; verify small details with zoomed crops.
- Character/faction sheets: flat studio look — the cinema look lives in locations and video prompts.
- After an asset locks in-window, later shots may reference it in-project (legal chaining).

"""
    body = [header]
    for md_title, _short, blurb, cards in sections:
        body.append(f"\n## {md_title}\n\n{blurb}\n\n---\n")
        for c in cards:
            lines = []
            lines.append(f"### {c['num']} — {c['title']}  `v{c['version']}`")
            lines.append("")
            lines.append(c["spec_md"])
            lines.append("")
            lines.append(f"**Prompt version:** v{c['version']} "
                         f"(last changed {c['updated']})")
            lines.append("")
            lines.append("**Prompt:**")
            lines.append("")
            for para in c["prompt"]:
                lines.append(f"    {para}")
                lines.append("")
            lines.append("**QC:** " + c["qc"])
            lines.append("")
            lines.append("---")
            lines.append("")
            body.append("\n".join(lines))
    return "\n".join(body)


CANVAS_TEMPLATE = r"""// GENERATED FILE — do not hand-edit. Regenerate with:
// python3 production/generator/generate_prompts.py (JESUS_IS_SKIING/FILM).
// Progress + notes persist in the .canvas.data.json sidecar, keyed by
// stable card keys, and survive regeneration.
import {
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  Pill,
  Row,
  Spacer,
  Stack,
  Stat,
  Swatch,
  Text,
  TextArea,
  TextInput,
  UsageBar,
  useCanvasAction,
  useCanvasState,
  useHostTheme,
} from "cursor/canvas";

type CardData = {
  key: string;
  num: string;
  section: string;
  title: string;
  model: string;
  ar: string;
  res: string;
  takes: number;
  specPills: string[];
  prompt: string;
  qc: string;
};

const GENERATED_AT = "__GENERATED_AT__";
const MD_PATH = "__MD_PATH__";
const PROJECT_TITLE = "__PROJECT_TITLE__";
const CARDS: CardData[] = __CARDS_JSON__;

const SECTIONS: string[] = [];
for (const c of CARDS) {
  if (!SECTIONS.includes(c.section)) SECTIONS.push(c.section);
}

type Status = "pending" | "generated" | "approved" | "revise";
type Review = { status: Status; note: string };

const EMPTY_REVIEW: Review = { status: "pending", note: "" };

const STATUS_META: {
  id: Status;
  label: string;
  color: "gray" | "blue" | "green" | "orange";
}[] = [
  { id: "pending", label: "Pending", color: "gray" },
  { id: "generated", label: "Generated", color: "blue" },
  { id: "approved", label: "Approved", color: "green" },
  { id: "revise", label: "Needs revision", color: "orange" },
];

function statusColor(s: Status): "gray" | "blue" | "green" | "orange" {
  return STATUS_META.find((m) => m.id === s)?.color ?? "gray";
}

function statusLabel(s: Status): string {
  return STATUS_META.find((m) => m.id === s)?.label ?? s;
}

const AGENT_PROMPT =
  "Process my Show Bible prompt tracker feedback for the __SLUG__ project. " +
  "Read production/generator/projects/__SLUG__/review_state.json in the " +
  "JESUS_IS_SKIING/FILM workspace (card keys are entity-id/state-id/view). " +
  "For every card whose status is 'revise', apply my note by editing " +
  "production/generator/projects/__SLUG__/matrix_data.json, then rerun " +
  "python3 production/generator/generate_prompts.py --project __SLUG__ so " +
  "the prompt document, cards.json and this canvas refresh. After applying " +
  "a note, set that card's status back to 'pending' so I can re-run it, and " +
  "summarize what changed card by card.";

export default function ShowBiblePromptTracker() {
  const theme = useHostTheme();
  const dispatch = useCanvasAction();
  const [reviews, setReviews] = useCanvasState<Record<string, Review>>(
    "reviews",
    {},
  );
  const [section, setSection] = useCanvasState<string>("section", "All");
  const [statusFilter, setStatusFilter] = useCanvasState<string>(
    "statusFilter",
    "all",
  );
  const [search, setSearch] = useCanvasState<string>("search", "");
  const [selectedKey, setSelectedKey] = useCanvasState<string>(
    "selectedKey",
    CARDS[0]?.key ?? "",
  );

  const review = (key: string): Review => reviews[key] ?? EMPTY_REVIEW;
  const patchReview = (key: string, patch: Partial<Review>) =>
    setReviews((prev) => ({
      ...prev,
      [key]: { ...(prev[key] ?? EMPTY_REVIEW), ...patch },
    }));

  const counts: Record<Status, number> = {
    pending: 0,
    generated: 0,
    approved: 0,
    revise: 0,
  };
  for (const c of CARDS) counts[review(c.key).status] += 1;
  const total = CARDS.length;
  const done = counts.approved;

  const q = search.trim().toLowerCase();
  const visible = CARDS.filter(
    (c) =>
      (section === "All" || c.section === section) &&
      (statusFilter === "all" || review(c.key).status === statusFilter) &&
      (q === "" || (c.num + " " + c.title).toLowerCase().includes(q)),
  );

  const selected: CardData | undefined =
    CARDS.find((c) => c.key === selectedKey) ?? visible[0] ?? CARDS[0];
  const selReview = selected ? review(selected.key) : EMPTY_REVIEW;

  const sectionCount = (s: string): number =>
    s === "All" ? total : CARDS.filter((c) => c.section === s).length;

  return (
    <Stack gap={16} style={{ padding: 16, maxWidth: 1200 }}>
      <Stack gap={4}>
        <H1>Show Bible Prompt Tracker</H1>
        <Text tone="secondary" size="small">
          {PROJECT_TITLE} · {total} virgin prompt cards (zero
          attachments) · regenerated {GENERATED_AT} from matrix_data.json
        </Text>
      </Stack>

      <UsageBar
        total={total}
        topLeftLabel={`${done} of ${total} approved`}
        topRightLabel={`${counts.revise} awaiting revision`}
        segments={[
          { id: "approved", value: counts.approved, color: "green" },
          { id: "generated", value: counts.generated, color: "blue" },
          { id: "revise", value: counts.revise, color: "orange" },
        ]}
      />

      <Grid columns={4} gap={16}>
        <Stat value={String(counts.pending)} label="Pending" />
        <Stat value={String(counts.generated)} label="Generated" tone="info" />
        <Stat value={String(counts.approved)} label="Approved" tone="success" />
        <Stat
          value={String(counts.revise)}
          label="Needs revision"
          tone="warning"
        />
      </Grid>

      <Row gap={6} wrap>
        {["All", ...SECTIONS].map((s) => (
          <span key={s} style={{ display: "inline-flex" }}>
            <Pill active={s === section} onClick={() => setSection(s)}>
              {s} ({sectionCount(s)})
            </Pill>
          </span>
        ))}
      </Row>

      <Row gap={8} align="center" wrap>
        <Pill
          size="sm"
          active={statusFilter === "all"}
          onClick={() => setStatusFilter("all")}
        >
          All statuses
        </Pill>
        {STATUS_META.map((m) => (
          <span key={m.id} style={{ display: "inline-flex" }}>
            <Pill
              size="sm"
              active={statusFilter === m.id}
              onClick={() => setStatusFilter(m.id)}
            >
              {m.label} ({counts[m.id]})
            </Pill>
          </span>
        ))}
        <Spacer />
        <TextInput
          value={search}
          onChange={setSearch}
          placeholder="Search cards…"
          type="search"
          style={{ width: 220 }}
        />
      </Row>

      <Grid columns="minmax(0, 330px) minmax(0, 1fr)" gap={16} align="start">
        <Stack
          gap={2}
          style={{
            maxHeight: 620,
            overflowY: "auto",
            border: `1px solid ${theme.stroke.tertiary}`,
            borderRadius: 8,
            padding: 6,
          }}
        >
          {visible.length === 0 ? (
            <Text tone="tertiary" size="small" style={{ padding: 8 }}>
              No cards match the current filters.
            </Text>
          ) : (
            visible.map((c) => {
              const st = review(c.key).status;
              const isSel = selected !== undefined && c.key === selected.key;
              return (
                <div
                  key={c.key}
                  onClick={() => setSelectedKey(c.key)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "5px 8px",
                    borderRadius: 6,
                    cursor: "pointer",
                    background: isSel ? theme.fill.tertiary : "transparent",
                  }}
                >
                  <Swatch
                    color={statusColor(st)}
                    style={{ width: 8, height: 8, flexShrink: 0 }}
                  />
                  <Text
                    size="small"
                    tone={isSel ? "primary" : "secondary"}
                    truncate
                    style={{ minWidth: 0 }}
                  >
                    {c.num} · {c.title}
                  </Text>
                </div>
              );
            })
          )}
        </Stack>

        {selected !== undefined && (
          <Card>
            <CardHeader
              trailing={
                <Pill size="sm" active>
                  {statusLabel(selReview.status)}
                </Pill>
              }
            >
              {selected.num} — {selected.title}
            </CardHeader>
            <CardBody>
              <Stack gap={12}>
                <Row gap={6} wrap>
                  {selected.specPills.map((p) => (
                    <Pill key={p} size="sm">{p}</Pill>
                  ))}
                </Row>

                <div
                  style={{
                    fontFamily:
                      "ui-monospace, SFMono-Regular, Menlo, monospace",
                    fontSize: 12,
                    lineHeight: 1.6,
                    whiteSpace: "pre-wrap",
                    background: theme.fill.tertiary,
                    color: theme.text.primary,
                    padding: 12,
                    borderRadius: 6,
                    maxHeight: 300,
                    overflowY: "auto",
                  }}
                >
                  {selected.prompt}
                </div>

                <Row gap={8}>
                  <Button
                    variant="primary"
                    onClick={() => {
                      void navigator.clipboard.writeText(selected.prompt);
                    }}
                  >
                    Copy prompt
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => {
                      void navigator.clipboard.writeText(
                        selected.prompt + "\n\nQC: " + selected.qc,
                      );
                    }}
                  >
                    Copy prompt + QC
                  </Button>
                </Row>

                <Callout tone="info" title="QC — verify in your takes">
                  {selected.qc}
                </Callout>

                <Divider />

                <Text size="small" weight="semibold">
                  Status
                </Text>
                <Row gap={6} wrap>
                  {STATUS_META.map((m) => (
                    <span key={m.id} style={{ display: "inline-flex" }}>
                      <Pill
                        active={selReview.status === m.id}
                        onClick={() =>
                          patchReview(selected.key, { status: m.id })
                        }
                      >
                        {m.label}
                      </Pill>
                    </span>
                  ))}
                </Row>

                <Text size="small" weight="semibold">
                  Director notes (drive regeneration)
                </Text>
                <TextArea
                  value={selReview.note}
                  onChange={(v) => patchReview(selected.key, { note: v })}
                  placeholder="What is wrong, what to change, what to keep…"
                  rows={3}
                />

                <Row gap={8} align="center">
                  <Button
                    variant="secondary"
                    disabled={counts.revise === 0}
                    onClick={() =>
                      dispatch({
                        type: "newComposerChat",
                        userPrompt: AGENT_PROMPT,
                      })
                    }
                  >
                    Send {counts.revise} revision note
                    {counts.revise === 1 ? "" : "s"} to agent
                  </Button>
                  <Spacer />
                  <Button
                    variant="ghost"
                    onClick={() =>
                      dispatch({ type: "openFile", path: MD_PATH })
                    }
                  >
                    Open SHOW_BIBLE_PROMPTS.md
                  </Button>
                </Row>
              </Stack>
            </CardBody>
          </Card>
        )}
      </Grid>

      <Text tone="tertiary" size="small">
        Workflow: Copy prompt → run it on higgsfield.ai with the settings
        pills as platform parameters → mark Generated → check the QC line →
        Approve, or set Needs revision with a note and send notes to the
        agent. Progress persists across restarts and regenerations.
      </Text>
    </Stack>
  );
}
"""


def card_fingerprint(f):
    """Hash of everything that changes what gets generated. Display numbers
    and section names are deliberately excluded — renumbering is not a new
    version of the prompt."""
    fields = [f["prompt"], f["qc"], f["model"], f["ar"], f["res"], f["takes"]]
    # Appended only when present, so a card with no second copy field hashes
    # exactly as it did before the field existed and does not churn a version.
    if f.get("altCopy"):
        fields.append(f["altCopy"])
    payload = json.dumps(fields, ensure_ascii=False)
    # `specPills` is display-only for image cards and identical to the four
    # fields above, so it is deliberately not hashed.
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def stamp_versions(flat, today):
    """Assign each card a version that increments only when its generated
    content actually changes. Persisted in the project's versions.json so
    version numbers survive regeneration and reordering."""
    path = os.path.join(PROJECT["paths"]["dir"], "versions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            store = json.load(fh)
    except (OSError, ValueError):
        store = {}

    for f in flat:
        h = card_fingerprint(f)
        rec = store.get(f["key"])
        if rec:
            # Collapse any duplicate-hash history left by older runs, keeping
            # the earliest version number for a given piece of text.
            seen_hashes, deduped = set(), []
            for e in rec.get("history") or []:
                if e.get("hash") in seen_hashes:
                    continue
                seen_hashes.add(e.get("hash"))
                deduped.append(e)
            if len(deduped) != len(rec.get("history") or []):
                rec["history"] = deduped
                match = next((e for e in deduped if e.get("hash") == rec.get("hash")), None)
                if match:
                    rec["version"] = match["v"]
        if rec is None:
            rec = {"version": 1, "hash": h, "updated": today,
                   "history": [{"v": 1, "hash": h, "date": today}]}
        elif rec.get("hash") != h:
            history = rec.get("history") or []
            # Versions are content-addressed: reverting to text this card
            # already carried returns to that version number rather than
            # inventing a new one, so a version always means one exact prompt.
            seen = next((e for e in history if e.get("hash") == h), None)
            if seen:
                rec = {"version": seen["v"], "hash": h,
                       "updated": seen.get("date", today), "history": history}
            else:
                v = max([e.get("v", 0) for e in history] + [rec.get("version", 0)]) + 1
                rec = {"version": v, "hash": h, "updated": today,
                       "history": history[-19:] + [{"v": v, "hash": h,
                                                    "date": today}]}
        store[f["key"]] = rec
        f["version"] = rec["version"]
        f["hash"] = rec["hash"]
        f["updated"] = rec["updated"]

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=1)
    return store


def render_canvas(sections, today):
    flat = []
    num = 0
    for _md_title, short, _blurb, cards in sections:
        for c in cards:
            num += 1
            c["num"] = f"SB-{num:03d}"
            flat.append({
                "key": c["key"],
                "num": c["num"],
                "section": short,
                "kind": c.get("kind", "render"),
                "title": c["title"],
                "model": c["model"],
                "ar": c["ar"],
                "res": c["res"],
                "takes": c["takes"],
                "specPills": c["spec_pills"],
                "altCopy": c.get("alt_copy"),
                "prompt": "\n\n".join(c["prompt"]),
                "qc": c["qc"],
            })
    stamp_versions(flat, today)
    by_key = {f["key"]: f for f in flat}
    for _md, _s, _b, cards in sections:      # so the markdown can print it
        for c in cards:
            c["version"] = by_key[c["key"]]["version"]
            c["updated"] = by_key[c["key"]]["updated"]
    cards_json = json.dumps(flat, ensure_ascii=False, indent=2)
    canvas_src = (CANVAS_TEMPLATE
                  .replace("__GENERATED_AT__", today)
                  .replace("__MD_PATH__", PROJECT["paths"]["markdown"])
                  .replace("__PROJECT_TITLE__", PROJECT["title"])
                  .replace("__SLUG__", PROJECT["slug"])
                  .replace("__CARDS_JSON__", cards_json))
    return canvas_src, flat


def build_project(slug, quiet=False):
    """Generate every output for one project. Returns the flat card list."""
    load_project(slug)
    today = datetime.date.today().isoformat()
    sections = build_sections()
    canvas_src, flat = render_canvas(sections, today)
    paths = PROJECT["paths"]

    def say(msg):
        if not quiet:
            print(msg)

    md_path = paths["markdown"]
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w") as f:
        f.write(render_markdown(sections, today))
    say(f"[{slug}] wrote {len(flat)} prompt cards to {md_path}")

    with open(paths["cards"], "w") as f:
        json.dump({"generated": today, "project": slug,
                   "title": PROJECT["title"], "cards": flat}, f,
                  ensure_ascii=False, indent=1)
    say(f"[{slug}] wrote cards data for the tracker app to {paths['cards']}")

    if paths["canvas"]:
        os.makedirs(os.path.dirname(paths["canvas"]), exist_ok=True)
        with open(paths["canvas"], "w") as f:
            f.write(canvas_src)
        say(f"[{slug}] wrote presentation canvas to {paths['canvas']}")

    return flat


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", "-p", help="project slug (default: active)")
    ap.add_argument("--all", action="store_true", help="build every project")
    ap.add_argument("--list", action="store_true", help="list projects")
    args = ap.parse_args()

    known = list_projects()
    if args.list:
        for s in known:
            mark = "*" if s == active_slug() else " "
            print(f" {mark} {s}")
        return

    if args.all:
        for s in known:
            build_project(s)
        return

    slug = args.project or active_slug()
    if slug not in known:
        raise SystemExit(f"unknown project '{slug}'. Known: "
                         + (", ".join(known) or "(none)"))
    build_project(slug)


if __name__ == "__main__":
    main()
