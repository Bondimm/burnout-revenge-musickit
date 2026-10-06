"""musickit command line.

  musickit gui                                   open the GUI
  musickit list [--iso X]                        list the EA Trax songs on a disc
  musickit info <audio>                          show input format + quality notes
  musickit add <audio> --title T --artist A [--album B] [--project P]
  musickit remove <n> [--project P]              remove the n-th queued new song (1-based)
  musickit queue [--iso X] [--project P]         show the pending song list (with --iso or after a change below)
  musickit replace <n> <audio> [--iso X]         new audio for song n (original or added)
  musickit edit <n> [--title T] [--artist A] [--album B] [--title-fr ...] [--iso X]   rename song n
  musickit remove-song <n> [--iso X]             remove song n from the disc (originals too)
  musickit move <n> <up|down|position> [--iso X] move song n
  musickit reset [--project P]                   forget all pending changes
  musickit build [--iso X] [--out Y] [--project P] [--no-normalize] [--unlock-off]
                                                 --unlock-off: every song can be switched OFF in the Song Manager
                                                 (the game normally waits until a song was heard once)
  musickit export <n> <out.wav> [--iso X]        decode an existing song to WAV
  musickit validate <source.iso> <output.iso>    check an output image against its source
Song numbers n refer to the pending list shown by `queue` (the same as `list` until something changed).
"""
import argparse
import json
import os
import sys

from . import core

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROJECT = os.path.join(HERE, "musickit_project.json")


def load_project(path):
    if os.path.exists(path):
        j = json.load(open(path, encoding="utf-8"))
    else:
        j = {}
    j.setdefault("iso", core.DEFAULT_ISO)
    j.setdefault("songs", [])
    return j


def save_project(path, j):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(j, f, indent=1, ensure_ascii=False)


def pending(j, d):
    """The pending song list for disc `d`: the stored list, or the disc's songs + queued new songs."""
    if j.get("list") is not None:
        if os.path.abspath(j.get("list_iso") or "") != os.path.abspath(d.path):
            raise SystemExit("the pending song list was made for %s - run `reset` first" % j.get("list_iso"))
        return [core.item_from_json(x) for x in j["list"]]
    return d.current_list() + [core.NewSong.from_json(x) for x in j["songs"]]


def store(j, d, items):
    j["iso"] = d.path
    j["list_iso"] = d.path
    j["list"] = [it.to_json() for it in items]
    j["songs"] = []


def _line(k, artist, title, album, tags):
    album = album.strip()
    return "%3d  %s - %s%s%s" % (k + 1, artist, title, " (%s)" % album if album else "",
                                 "  [%s]" % ", ".join(tags) if tags else "")


def describe(d, k, it):
    if isinstance(it, core.NewSong):
        return _line(k, it.artist, it.title, it.album, ["new: %s" % it.path])
    tags = []
    if it.src != k:
        tags.append("was #%d" % (it.src + 1))
    if it.audio:
        tags.append("new audio: %s" % it.audio)
    if it.edited(d):
        tags.append("renamed")
    l = d.langs[0]
    return _line(k, it.text(d, l, "artist"), it.text(d, l, "title"), it.text(d, l, "album"), tags)


def _names(a):
    names = {}
    for l in core.LANGS:
        d = {f: getattr(a, "%s_%s" % (f, l.lower())) for f in core.FIELDS}
        d = {k: v for k, v in d.items() if v}
        if d:
            names[l] = d
    return names


def _index(items, n):
    if not 1 <= n <= len(items):
        raise SystemExit("song %d does not exist (1..%d)" % (n, len(items)))
    return n - 1


def _progress(frac, msg):
    sys.stdout.write("\r[%3d%%] %-70s" % (int(frac * 100), msg[:70]))
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="musickit", description="Manage the EA Trax songs of Burnout Revenge (PS2)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("gui"); p.add_argument("--shot", help="render, save a screenshot and exit")
    p.add_argument("--size", help="window size WIDTHxHEIGHT (default 1400x820)")
    p.add_argument("--demo", help="settings JSON to start with (iso, out, queue, form)")
    p = sub.add_parser("list"); p.add_argument("--iso")
    p = sub.add_parser("info"); p.add_argument("audio")
    p = sub.add_parser("add"); p.add_argument("audio"); p.add_argument("--title"); p.add_argument("--artist", default="")
    p.add_argument("--album", default=""); p.add_argument("--project", default=DEFAULT_PROJECT)
    for l in core.LANGS:
        p.add_argument("--title-" + l.lower()); p.add_argument("--artist-" + l.lower()); p.add_argument("--album-" + l.lower())
    p = sub.add_parser("remove"); p.add_argument("n", type=int); p.add_argument("--project", default=DEFAULT_PROJECT)
    p = sub.add_parser("queue"); p.add_argument("--project", default=DEFAULT_PROJECT); p.add_argument("--iso")
    p = sub.add_parser("replace"); p.add_argument("n", type=int); p.add_argument("audio")
    p = sub.add_parser("edit"); p.add_argument("n", type=int)
    for f in core.FIELDS:
        p.add_argument("--" + f)
        for l in core.LANGS:
            p.add_argument("--%s-%s" % (f, l.lower()))
    p = sub.add_parser("remove-song"); p.add_argument("n", type=int)
    p = sub.add_parser("move"); p.add_argument("n", type=int); p.add_argument("to")
    for name in ("replace", "edit", "remove-song", "move"):
        sub.choices[name].add_argument("--iso"); sub.choices[name].add_argument("--project", default=DEFAULT_PROJECT)
    p = sub.add_parser("reset"); p.add_argument("--project", default=DEFAULT_PROJECT)
    p = sub.add_parser("build"); p.add_argument("--iso"); p.add_argument("--out"); p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--no-normalize", action="store_true")
    p.add_argument("--unlock-off", action="store_true")
    p = sub.add_parser("validate"); p.add_argument("source"); p.add_argument("output")
    p = sub.add_parser("export"); p.add_argument("n", type=int); p.add_argument("out"); p.add_argument("--iso")
    a = ap.parse_args(argv)

    if a.cmd in (None, "gui"):
        from . import gui
        demo = json.load(open(a.demo, encoding="utf-8")) if getattr(a, "demo", None) else None
        size = tuple(int(v) for v in a.size.lower().split("x")) if getattr(a, "size", None) else None
        return gui.main(getattr(a, "shot", None), demo, size)
    if a.cmd == "list":
        d = core.Disc(a.iso or load_project(DEFAULT_PROJECT)["iso"])
        print("%d songs (%s%s)" % (d.count, "MusicKit-patched" if d.patched else "original",
                                   ", every song can be switched OFF" if d.unlocked else ""))
        for s in d.songs:
            print("%3d  %-28s %-45s %-30s %d:%02d" % (s.index + 1, s.artist[:28], s.title[:45], s.album[:30],
                                                     int(s.duration) // 60, int(s.duration) % 60))
        return 0
    if a.cmd == "info":
        from . import audio
        i = audio.AudioInfo(a.audio)
        print(i.describe())
        for lvl, t in i.quality_notes():
            print(" %s %s" % ("!" if lvl == "warn" else "-", t))
        return 0
    if a.cmd == "add":
        j = load_project(a.project)
        from . import audio
        info = audio.AudioInfo(a.audio)
        names = _names(a)
        s = core.NewSong(os.path.abspath(a.audio), a.title or info.title or os.path.splitext(os.path.basename(a.audio))[0],
                         a.artist or info.artist, a.album or info.album, names)
        if j.get("list") is not None:
            j["list"].append(s.to_json())
            n = len(j["list"])
        else:
            j["songs"].append(s.to_json())
            n = len(j["songs"])
        save_project(a.project, j)
        print("queued #%d: %s - %s (%s)  [%s]" % (n, s.artist, s.title, s.album, info.describe()))
        return 0
    if a.cmd == "remove":
        j = load_project(a.project)
        if j.get("list") is not None:
            new = [k for k, x in enumerate(j["list"]) if "src" not in x]
            if not 1 <= a.n <= len(new):
                raise SystemExit("there are %d queued new songs" % len(new))
            s = j["list"].pop(new[a.n - 1])
        else:
            s = j["songs"].pop(a.n - 1)
        save_project(a.project, j)
        print("removed", s["title"])
        return 0
    if a.cmd == "queue":
        j = load_project(a.project)
        if j.get("list") is None and not a.iso:
            for k, s in enumerate(j["songs"]):
                print("%2d  %s - %s (%s)  %s" % (k + 1, s["artist"], s["title"], s["album"], s["path"]))
            return 0
        d = core.Disc(a.iso or j.get("list_iso") or j["iso"])
        items = pending(j, d)
        for k, it in enumerate(items):
            print(describe(d, k, it))
        w = d.save_warning(items)
        if w:
            print("! " + w)
        return 0
    if a.cmd == "reset":
        j = load_project(a.project)
        j["list"] = None
        j["songs"] = []
        save_project(a.project, j)
        print("pending changes cleared")
        return 0
    if a.cmd in ("replace", "edit", "remove-song", "move"):
        j = load_project(a.project)
        src = a.iso or j.get("list_iso") or j["iso"]
        if not src:
            raise SystemExit("give the ISO with --iso")
        d = core.Disc(src)
        items = pending(j, d)
        k = _index(items, a.n)
        it = items[k]
        if a.cmd == "replace":
            from . import audio
            info = audio.AudioInfo(a.audio)
            if isinstance(it, core.NewSong):
                it.path = os.path.abspath(a.audio)
            else:
                it.audio = os.path.abspath(a.audio)
            print("song %d gets new audio: %s" % (k + 1, info.describe()))
        elif a.cmd == "edit":
            for f in core.FIELDS:
                v = getattr(a, f)
                if v is not None:
                    setattr(it, f, v)
                    for l in list(it.names):         # a text for all languages replaces per-language texts
                        it.names[l].pop(f, None)
            for l, dd in _names(a).items():
                it.names.setdefault(l, {}).update(dd)
        elif a.cmd == "remove-song":
            if len(items) == 1:
                raise SystemExit("at least one song must stay on the disc")
            items.pop(k)
        elif a.cmd == "move":
            to = {"up": k - 1, "down": k + 1}.get(a.to)
            if to is None:
                to = int(a.to) - 1
            to = max(0, min(len(items) - 1, to))
            items.insert(to, items.pop(k))
        store(j, d, items)
        save_project(a.project, j)
        if a.cmd in ("remove-song", "move"):
            for i, x in enumerate(items):
                print(describe(d, i, x))
            w = d.save_warning(items)
            if w:
                print("! " + w)
        else:
            print(describe(d, k, it))
        return 0
    if a.cmd == "build":
        j = load_project(a.project)
        src = a.iso or j["iso"]
        out = a.out or core.default_output(src)
        d = core.Disc(src)
        items = pending(j, d)
        unlock = a.unlock_off and not d.unlocked
        if d.is_unchanged(items) and not unlock:
            raise SystemExit("nothing to do: add, replace, edit, remove or move songs first (or use --unlock-off)")
        w = d.save_warning(items)
        if w:
            print("! " + w)
        size = d.size_plan(items, unlock)
        print(("" if size["fits"] else "! ") + size["text"])
        rep = d.build_list(items, out, normalize=not a.no_normalize, progress=_progress, unlock_off=unlock)
        print()
        if rep["unlocked"]:
            print("every song can be switched OFF in the Song Manager")
        if "target_lufs" in rep:
            print("target loudness %.1f LUFS (median of the original songs)" % rep["target_lufs"])
        for s in rep["songs"]:
            print("song %d %s%s: %.1f s, in %s LUFS -> out %s LUFS, %d KB" % (
                s["index"] + 1, s["title"], " (new audio)" if s.get("replaced") else "", s["seconds"],
                "%.1f" % s["input_lufs"] if s["input_lufs"] is not None else "-",
                "%.1f" % s["output_lufs"] if s["output_lufs"] is not None else "-", s["bytes"] // 1024))
        print("wrote", out, "(%d songs)" % rep["total_songs"])
        return 0
    if a.cmd == "validate":
        from . import validate
        return 0 if validate.validate(a.source, a.output) else 1
    if a.cmd == "export":
        import soundfile as sf
        d = core.Disc(a.iso or load_project(DEFAULT_PROJECT)["iso"])
        sf.write(a.out, d.decode_song(a.n - 1), 32000)
        print("wrote", a.out)
        return 0


if __name__ == "__main__":
    sys.exit(main())
