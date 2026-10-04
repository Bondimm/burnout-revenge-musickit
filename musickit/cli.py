"""musickit command line.

  musickit gui                                   open the GUI
  musickit list [--iso X]                        list the EA Trax songs on a disc
  musickit info <audio>                          show input format + quality notes
  musickit add <audio> --title T --artist A [--album B] [--project P]
  musickit remove <n> [--project P]              remove the n-th queued song (1-based, see `queue`)
  musickit queue [--project P]                   show queued songs
  musickit build [--iso X] [--out Y] [--project P] [--no-normalize]
  musickit export <n> <out.wav> [--iso X]        decode an existing song to WAV
  musickit validate <source.iso> <output.iso>    check an output image against its source
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


def _progress(frac, msg):
    sys.stdout.write("\r[%3d%%] %-70s" % (int(frac * 100), msg[:70]))
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="musickit", description="Add songs to Burnout Revenge (PAL) EA Trax")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("gui"); p.add_argument("--shot", help="render, save a screenshot and exit")
    p.add_argument("--demo", help="settings JSON to start with (iso, out, queue, form)")
    p = sub.add_parser("list"); p.add_argument("--iso")
    p = sub.add_parser("info"); p.add_argument("audio")
    p = sub.add_parser("add"); p.add_argument("audio"); p.add_argument("--title"); p.add_argument("--artist", default="")
    p.add_argument("--album", default=""); p.add_argument("--project", default=DEFAULT_PROJECT)
    for l in core.LANGS:
        p.add_argument("--title-" + l.lower()); p.add_argument("--artist-" + l.lower()); p.add_argument("--album-" + l.lower())
    p = sub.add_parser("remove"); p.add_argument("n", type=int); p.add_argument("--project", default=DEFAULT_PROJECT)
    p = sub.add_parser("queue"); p.add_argument("--project", default=DEFAULT_PROJECT)
    p = sub.add_parser("build"); p.add_argument("--iso"); p.add_argument("--out"); p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--no-normalize", action="store_true")
    p = sub.add_parser("validate"); p.add_argument("source"); p.add_argument("output")
    p = sub.add_parser("export"); p.add_argument("n", type=int); p.add_argument("out"); p.add_argument("--iso")
    a = ap.parse_args(argv)

    if a.cmd in (None, "gui"):
        from . import gui
        demo = json.load(open(a.demo, encoding="utf-8")) if getattr(a, "demo", None) else None
        return gui.main(getattr(a, "shot", None), demo)
    if a.cmd == "list":
        d = core.Disc(a.iso or load_project(DEFAULT_PROJECT)["iso"])
        print("%d songs (%s)" % (d.count, "MusicKit-patched" if d.patched else "original"))
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
        names = {}
        for l in core.LANGS:
            d = {f: getattr(a, "%s_%s" % (f, l.lower())) for f in ("title", "artist", "album")}
            d = {k: v for k, v in d.items() if v}
            if d:
                names[l] = d
        s = core.NewSong(os.path.abspath(a.audio), a.title or info.title or os.path.splitext(os.path.basename(a.audio))[0],
                         a.artist or info.artist, a.album or info.album, names)
        j["songs"].append(s.to_json())
        save_project(a.project, j)
        print("queued #%d: %s - %s (%s)  [%s]" % (len(j["songs"]), s.artist, s.title, s.album, info.describe()))
        return 0
    if a.cmd == "remove":
        j = load_project(a.project)
        s = j["songs"].pop(a.n - 1)
        save_project(a.project, j)
        print("removed", s["title"])
        return 0
    if a.cmd == "queue":
        j = load_project(a.project)
        for k, s in enumerate(j["songs"]):
            print("%2d  %s - %s (%s)  %s" % (k + 1, s["artist"], s["title"], s["album"], s["path"]))
        return 0
    if a.cmd == "build":
        j = load_project(a.project)
        src = a.iso or j["iso"]
        out = a.out or core.default_output(src)
        d = core.Disc(src)
        songs = [core.NewSong.from_json(s) for s in j["songs"]]
        rep = d.build(songs, out, normalize=not a.no_normalize, progress=_progress)
        print()
        if "target_lufs" in rep:
            print("target loudness %.1f LUFS (median of the original songs)" % rep["target_lufs"])
        for s in rep["songs"]:
            print("song %d %s: %.1f s, in %s LUFS -> out %s LUFS, %d KB" % (
                s["index"] + 1, s["title"], s["seconds"],
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
