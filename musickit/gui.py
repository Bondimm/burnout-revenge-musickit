"""MusicKit desktop GUI: 1) select the Burnout Revenge ISO, 2) add songs, 3) save a new ISO. The song list on the
right replaces, renames, removes and reorders any song (changes are applied when the new ISO is saved).

Launch: MusicKit.bat in the project root (or `musickit gui`).
Same GLFW + OpenGL 3.3 + Dear ImGui stack as carkit. Long operations run on a worker thread; messages go to the
log panel and %APPDATA%\\musickit\\gui.log. The source ISO is only read; the result is always a new file.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import tempfile
import threading
import time
import traceback

import glfw
from OpenGL import GL as gl
from imgui_bundle import imgui
from imgui_bundle.python_backends.glfw_backend import GlfwRenderer

from . import audio, core

TITLE = "MusicKit - Burnout Revenge EA Trax"
RED = (1.0, 0.45, 0.45, 1.0)
YELLOW = (1.0, 0.85, 0.35, 1.0)
GREEN = (0.45, 0.9, 0.5, 1.0)
GREY = (0.62, 0.62, 0.66, 1.0)
ACCENT = (1.0, 0.62, 0.15, 1.0)
AUDIO_FILTERS = ["Audio files", "*.mp3 *.flac *.wav *.ogg *.m4a *.aac *.opus *.wma *.aiff *.aif", "All files", "*"]


def app_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "musickit")
    os.makedirs(d, exist_ok=True)
    return d


def _col(c):
    return imgui.ImVec4(*c)


def _bw(*labels):
    """Width of a row of small buttons with these labels."""
    st = imgui.get_style()
    return sum(imgui.calc_text_size(t).x + 2 * st.frame_padding.x + st.item_spacing.x for t in labels)


def _tip(text):
    if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled | imgui.HoveredFlags_.delay_short):
        imgui.begin_tooltip()
        imgui.push_text_wrap_pos(imgui.get_font_size() * 40)
        imgui.text_unformatted(text)
        imgui.pop_text_wrap_pos()
        imgui.end_tooltip()


def _text(color, text):
    """Message text wrapped to the width of the current panel / table column (re-flows when the window is resized)."""
    imgui.push_text_wrap_pos(0)
    if color is None:
        imgui.text_unformatted(text)
    else:
        imgui.push_style_color(imgui.Col_.text, _col(color))
        imgui.text_unformatted(text)
        imgui.pop_style_color()
    imgui.pop_text_wrap_pos()


class Log:
    def __init__(self, path):
        self.lines = collections.deque(maxlen=3000)
        self.lock = threading.Lock()
        try:
            self.file = open(path, "a", encoding="utf-8", buffering=1)
            self.file.write("\n===== musickit gui %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            self.file = None

    def __call__(self, text, level="info"):
        for line in str(text).splitlines() or [""]:
            with self.lock:
                self.lines.append((level, time.strftime("%H:%M:%S"), line))
            if self.file:
                try:
                    self.file.write(line + "\n")
                except Exception:
                    pass

    def error(self, text):
        self(text, "error")


class Job:
    def __init__(self, title, fn, log):
        self.title = title
        self.progress = 0.0
        self.message = title
        self.done = False
        self.ok = False
        self.result = None
        self.log = log
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def update(self, frac, msg):
        self.progress = max(0.0, min(1.0, frac))
        self.message = msg

    def _run(self, fn):
        try:
            self.result = fn(self)
            self.ok = True
        except Exception as exc:
            self.log.error("%s failed: %s" % (self.title, exc))
            self.log(traceback.format_exc(), "error")
        finally:
            self.done = True


class Pending:
    """A song queued for adding, with its probed format."""

    def __init__(self, song: core.NewSong):
        self.song = song
        self.info = None
        self.error = None
        try:
            self.info = song.probe()
        except Exception as exc:
            self.error = str(exc)


def _item(it):
    return it.song if isinstance(it, Pending) else it


class MusicKitGui:
    def __init__(self, log, settings):
        self.log = log
        self.settings = settings
        self.iso_path = settings.get("iso") or core.DEFAULT_ISO
        self.out_path = settings.get("out") or ""
        self.disc = None
        self.disc_error = None
        self.items = []        # the new song list: core.SongRef (song of the disc) or Pending (new song)
        for j in settings.get("queue", []):
            if os.path.exists(j.get("path", "")):
                self.items.append(Pending(core.NewSong.from_json(j)))
        self.editing = None    # item whose names are being edited
        self.normalize = settings.get("normalize", True)
        self.unlock_off = settings.get("unlock_off", False)
        self.job = None
        self.dialog = None
        self.form = {"path": "", "title": "", "artist": "", "album": ""}
        self.form_info = None
        self.form_error = None
        self.ref_loudness = None
        self.last_report = None
        self.playing = None
        self.saved_out = None  # last image saved in this session
        self.load_disc(self.iso_path)
        if settings.get("form"):
            self.set_form_file(settings["form"]["path"])
            for k in ("title", "artist", "album"):
                if settings["form"].get(k):
                    self.form[k] = settings["form"][k]

    # ------------------------------------------------------------------ state
    def save_settings(self):
        self.settings.update({"iso": self.iso_path, "out": self.out_path, "normalize": self.normalize,
                              "unlock_off": self.unlock_off,
                              "queue": [p.song.to_json() for p in self.new_items()],
                              "list": [_item(it).to_json() for it in self.items] if self.disc else None,
                              "list_iso": self.disc.path if self.disc else None})
        try:
            with open(os.path.join(app_dir(), "settings.json"), "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=1)
        except OSError:
            pass

    @property
    def queue(self):
        return self.new_items()

    def new_items(self):
        return [it for it in self.items if isinstance(it, Pending)]

    def core_items(self):
        return [_item(it) for it in self.items]

    def changed(self):
        return self.disc is not None and not self.disc.is_unchanged(self.core_items())

    def unlock_pending(self):
        return bool(self.unlock_off and self.disc is not None and not self.disc.unlocked)

    def can_save(self):
        """Song list changed, or the switch-off unlock alone."""
        return self.changed() or self.unlock_pending()

    def save_problem(self):
        """Why "Save new ISO" cannot be pressed now (None when it can)."""
        if self.disc is None:
            return "select the ISO first"
        if self.busy():
            return "wait until '%s' has finished" % self.job.title
        if not self.can_save():
            return "nothing to save yet: add, replace, rename, remove or move songs first"
        if not self.out_path.strip():
            return "choose where to save the new ISO"
        if os.path.abspath(self.out_path) == os.path.abspath(self.iso_path):
            return "choose a different output file - the source ISO is never overwritten"
        return None

    def size_plan(self):
        """Projected size of the new image (cached per song list; None if it cannot be computed)."""
        if self.disc is None:
            return None
        items = self.core_items()
        key = (id(self.disc), self.unlock_pending(),
               json.dumps([it.to_json() for it in items], sort_keys=True, ensure_ascii=False))
        if getattr(self, "_size_key", None) != key:
            self._size_key = key
            est = {}
            for k, it in enumerate(self.items):
                info = getattr(it, "info", None)
                if info is not None and (isinstance(it, Pending) or it.audio):
                    est[k] = info.duration
            try:
                self._size = self.disc.size_plan(items, self.unlock_pending(), est)
            except Exception:
                self._size = None
        return self._size

    def reset_list(self, keep_new=True):
        new = self.new_items() if keep_new else []
        self.items = (self.disc.current_list() if self.disc else []) + new
        self.editing = None

    def busy(self):
        return self.job is not None and not self.job.done

    def start_job(self, title, fn):
        if self.busy():
            self.log.error("'%s' is still running" % self.job.title)
            return
        self.log("--- " + title)
        self.job = Job(title, fn, self.log)

    def load_disc(self, path):
        if self.busy():
            self.log.error("'%s' is still running - open the ISO again when it has finished" % self.job.title)
            return
        self.iso_path = path
        self.disc = None
        self.disc_error = None
        if not path:
            self.disc_error = "Choose your Burnout Revenge ISO with Browse... (PAL SLES-53507 or USA SLUS-21242)"
            return
        if not os.path.exists(path):
            self.disc_error = "ISO not found"
            return

        def run(job):
            job.update(0.3, "reading " + os.path.basename(path))
            d = core.Disc(path)
            items = None
            if self.settings.get("list") and self.settings.get("list_iso") == path:
                try:
                    items = [core.item_from_json(j) for j in self.settings["list"]]
                    items = [Pending(it) if isinstance(it, core.NewSong) else it for it in items
                             if isinstance(it, core.NewSong) and os.path.exists(it.path)
                             or isinstance(it, core.SongRef) and it.src < d.count]
                    for it in items:
                        if isinstance(it, core.SongRef) and it.audio:
                            try:
                                it.info = audio.AudioInfo(it.audio)
                            except Exception:
                                it.audio = None
                except Exception:
                    items = None
            self.disc = d
            if items:
                self.items = items
            else:
                self.reset_list(keep_new=os.path.abspath(path) != self.saved_out)
            if not self.out_path or os.path.abspath(self.out_path) == os.path.abspath(path):
                self.out_path = core.default_output(path)
            self.log("disc: %s - %d songs (%s)" % (os.path.basename(path), d.count,
                                                   "already has MusicKit songs" if d.patched else "original"))
            return d

        def wrapped(job):
            try:
                return run(job)
            except Exception as exc:
                self.disc_error = str(exc)
                raise

        self.start_job("open ISO", wrapped)

    # ------------------------------------------------------------------ dialogs
    def open_dialog(self, dlg, cb):
        if self.dialog is None:
            self.dialog = (dlg, cb)

    def poll_dialog(self):
        if self.dialog is None:
            return
        dlg, cb = self.dialog
        try:
            if dlg.ready(0):
                self.dialog = None
                cb(dlg.result())
        except Exception as exc:
            self.dialog = None
            self.log.error("file dialog failed: %s" % exc)

    def pick_iso(self):
        from imgui_bundle import portable_file_dialogs as pfd
        start = os.path.dirname(self.iso_path) if self.iso_path else ""
        self.open_dialog(pfd.open_file("Select the Burnout Revenge ISO", start, ["PS2 DVD image", "*.iso", "All files", "*"]),
                         lambda r: r and self.load_disc(r[0]))

    def pick_audio(self):
        from imgui_bundle import portable_file_dialogs as pfd
        start = os.path.dirname(self.form["path"]) if self.form["path"] else ""
        self.open_dialog(pfd.open_file("Choose a song", start, AUDIO_FILTERS), lambda r: r and self.set_form_file(r[0]))

    def pick_replace(self, it):
        from imgui_bundle import portable_file_dialogs as pfd
        self.open_dialog(pfd.open_file("New audio for this song", "", AUDIO_FILTERS),
                         lambda r: r and self.set_replacement(it, r[0]))

    def set_replacement(self, it, path):
        try:
            info = audio.AudioInfo(path)
        except Exception as exc:
            self.log.error("cannot read %s: %s" % (path, exc))
            return
        if isinstance(it, Pending):
            it.song.path = path
            it.info = info
            it.error = None
        else:
            it.audio = path
            it.info = info
        self.log("new audio for '%s': %s" % (self.item_text(it, "title"), info.describe()))
        self.save_settings()

    def item_text(self, it, field, lang=None):
        if isinstance(it, Pending):
            return it.song.text(lang, field) if lang else getattr(it.song, field)
        return it.text(self.disc, lang or self.disc.langs[0], field)

    def rename(self, it, key, value, lang=None):
        """Song editor: a text for all languages (lang None) replaces the per-language texts of that field."""
        target = it.song if isinstance(it, Pending) else it
        if lang is None:
            setattr(target, key, value)
            for d in target.names.values():
                d.pop(key, None)
        else:
            target.names.setdefault(lang, {})[key] = value

    def restore_names(self, it):
        it.title = it.artist = it.album = None
        it.names = {}

    def move_item(self, i, to):
        if 0 <= to < len(self.items):
            self.items.insert(to, self.items.pop(i))
            self.save_settings()

    def remove_item(self, i):
        if len(self.items) <= 1:
            self.log.error("at least one song must stay on the disc")
            return
        it = self.items.pop(i)
        if self.editing is it:
            self.editing = None
        self.log("removed: %s - %s" % (self.item_text(it, "artist"), self.item_text(it, "title")))
        self.save_settings()

    def pick_out(self):
        from imgui_bundle import portable_file_dialogs as pfd
        self.open_dialog(pfd.save_file("Save the new ISO as", self.out_path, ["PS2 DVD image", "*.iso"]),
                         lambda r: r and self._set_out(r))

    def _set_out(self, path):
        if not path.lower().endswith(".iso"):
            path += ".iso"
        self.out_path = path

    def drop(self, paths):
        for p in paths:
            if p.lower().endswith(".iso"):
                self.load_disc(p)
            else:
                self.set_form_file(p)

    def set_form_file(self, path):
        self.form = {"path": path, "title": "", "artist": "", "album": ""}
        self.form_info = None
        self.form_error = None
        try:
            info = audio.AudioInfo(path)
            self.form_info = info
            self.form["title"] = info.title or os.path.splitext(os.path.basename(path))[0]
            self.form["artist"] = info.artist
            self.form["album"] = info.album
        except Exception as exc:
            self.form_error = "cannot read this file: %s" % exc

    def add_form_song(self):
        s = core.NewSong(self.form["path"], self.form["title"].strip(), self.form["artist"].strip(),
                         self.form["album"].strip())
        p = Pending(s)
        if p.error:
            self.log.error(p.error)
            return
        self.items.append(p)
        self.log("added: %s - %s (%s)" % (s.artist, s.title, p.info.describe()))
        self.form = {"path": "", "title": "", "artist": "", "album": ""}
        self.form_info = None
        self.save_settings()

    # ------------------------------------------------------------------ playback
    def play_wav(self, path, label):
        import winsound
        winsound.PlaySound(None, 0)
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        self.playing = label

    def stop(self):
        import winsound
        winsound.PlaySound(None, 0)
        self.playing = None

    def preview_original(self, idx):
        def run(job):
            job.update(0.5, "decoding song %d" % (idx + 1))
            self.play_wav(self.disc.preview_wav(idx, seconds=60), "orig%d" % idx)
        self.start_job("preview", run)

    def preview_new(self, p, path=None):
        """Preview exactly as the game will play it: 32 kHz, loudness matched, PS-ADPCM encoded and decoded."""
        path_in = path or p.song.path

        def run(job):
            import soundfile as sf
            from . import rws
            target = None
            if self.normalize and self.disc:
                job.update(0.1, "measuring the original soundtrack loudness")
                target = self.disc.reference_loudness(job.update)["median_lufs"]
            job.update(0.6, "encoding preview")
            pcm, _ = audio.prepare(path_in, target, self.normalize)
            pcm = pcm[: 32000 * 45]
            payload, usable = rws.encode_segment(pcm)
            back = rws.decode_segment(payload, usable)
            path = os.path.join(tempfile.gettempdir(), "musickit_new_preview.wav")
            sf.write(path, back, 32000)
            self.play_wav(path, id(p))
        self.start_job("preview (as in game)", run)

    # ------------------------------------------------------------------ build
    def build(self):
        problem = self.save_problem()
        if problem:
            self.log.error(problem)
            return
        items = self.core_items()
        out = self.out_path
        disc = self.disc
        unlock = self.unlock_pending()
        w = disc.save_warning(items)
        if w:
            self.log(w)

        def run(job):
            rep = disc.build_list(items, out, self.normalize, job.update, unlock_off=unlock)
            if rep["unlocked"]:
                self.log("every song can be switched OFF in the Song Manager")
            if "target_lufs" in rep:
                self.log("loudness target %.1f LUFS (median of the original songs)" % rep["target_lufs"])
            for s in rep["songs"]:
                self.log("  song %d '%s'%s: %.0f s, %s -> %s LUFS" % (
                    s["index"] + 1, s["title"], " (new audio)" if s.get("replaced") else "", s["seconds"],
                    "%.1f" % s["input_lufs"] if s["input_lufs"] is not None else "-",
                    "%.1f" % s["output_lufs"] if s["output_lufs"] is not None else "-"))
            self.log("saved %s (%d songs). Untouched files keep their place on the disc." % (out, rep["total_songs"]))
            self.last_report = rep
            self.saved_out = os.path.abspath(out)
            return rep

        self.start_job("save new ISO", run)

    # ------------------------------------------------------------------ UI
    def ui(self):
        self.poll_dialog()
        vp = imgui.get_main_viewport()
        imgui.set_next_window_pos(vp.work_pos)
        imgui.set_next_window_size(vp.work_size)
        flags = (imgui.WindowFlags_.no_decoration | imgui.WindowFlags_.no_move | imgui.WindowFlags_.no_saved_settings
                 | imgui.WindowFlags_.no_bring_to_front_on_focus)
        imgui.begin("main", None, flags)
        imgui.push_style_color(imgui.Col_.text, _col(ACCENT))
        imgui.text("MusicKit")
        imgui.pop_style_color()
        imgui.same_line()
        _text(GREY, "add, replace, rename, remove and reorder the EA Trax songs of Burnout Revenge "
              "(PS2, PAL or USA)")
        imgui.separator()
        avail = imgui.get_content_region_avail()
        left_w = avail.x * 0.45
        imgui.begin_child("left", imgui.ImVec2(left_w, avail.y - 4))
        self.ui_step1()
        self.ui_step2()
        self.ui_step3()
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("right", imgui.ImVec2(0, avail.y - 4))
        self.ui_right()
        imgui.end_child()
        imgui.end()

    def step_header(self, n, text, done):
        imgui.spacing()
        _text(GREEN if done else ACCENT, "%d" % n)
        imgui.same_line()
        _text(None, text)
        imgui.separator()

    def ui_step1(self):
        self.step_header(1, "Select ISO", self.disc is not None)
        imgui.set_next_item_width(-110)
        changed, v = imgui.input_text("##iso", self.iso_path, imgui.InputTextFlags_.enter_returns_true)
        if changed:
            self.load_disc(v)
        imgui.same_line()
        if imgui.button("Browse...##iso", imgui.ImVec2(100, 0)):
            self.pick_iso()
        if self.disc:
            d = self.disc
            n_orig = sum(1 for x in d.songs if x.original)
            _text(GREEN, "Burnout Revenge %s - %d songs on the disc%s" %
                  (d.region, d.count, " (%d added or changed earlier with MusicKit)" % (d.count - n_orig)
                  if d.count != n_orig else ""))
            if d.count > core.MAX_SONGS:
                _text(YELLOW, "! More than %d songs: remove some before saving (shuffle mode "
                      "supports %d at most)." % (core.MAX_SONGS, core.MAX_SONGS))
            _text(GREY, "The ISO is only read; it is never modified.")
            _text(GREY, "PCSX2 game CRC %08X is kept, so PCSX2 patches (widescreen ...) still apply."
                  % d.crc)
            _text(GREY, "A modified disc never matches the redump checksum (normal for any mod).")
        elif self.busy() and self.job.title == "open ISO":
            _text(YELLOW, "reading the ISO...")
        elif self.disc_error:
            _text(RED, self.disc_error)

    def ui_step2(self):
        self.step_header(2, "Add songs", bool(self.queue))
        if imgui.button("Choose audio file...", imgui.ImVec2(200, 0)):
            self.pick_audio()
        _tip("MP3, FLAC, WAV, OGG, M4A ... (anything ffmpeg reads). You can also drop files on the window.")
        imgui.same_line()
        _text(GREY, os.path.basename(self.form["path"]) or "no file chosen (or drop one here)")
        if self.form_error:
            _text(RED, self.form_error)
        enabled = bool(self.form["path"]) and self.form_info is not None
        imgui.begin_disabled(not enabled)
        for key, label in (("title", "Title"), ("artist", "Artist"), ("album", "Album")):
            imgui.set_next_item_width(-80)
            _, self.form[key] = imgui.input_text(label + "##form", self.form[key])
        if self.form_info:
            self.ui_quality(self.form_info, self.form)
        can_add = enabled and self.form["title"].strip() and self.disc is not None and len(self.items) < core.MAX_SONGS
        imgui.begin_disabled(not can_add)
        if imgui.button("Add song", imgui.ImVec2(200, 0)):
            self.add_form_song()
        imgui.end_disabled()
        imgui.end_disabled()
        imgui.spacing()
        self.ui_queue()

    def ui_quality(self, info, form=None):
        _text(GREY, "Input:  " + info.describe())
        _text(GREY, "Game:   PS-ADPCM stereo 32 kHz%s" %
              (", loudness matched to the original songs" if self.normalize else ""))
        for lvl, t in info.quality_notes():
            if t.startswith("Encoded as"):
                continue
            _text(YELLOW if lvl == "warn" else GREY, ("! " if lvl == "warn" else "- ") + t)
        if form and self.disc:
            for key in ("title", "artist", "album"):
                clean = self.disc.sanitize(form[key])
                if form[key].strip() and clean != form[key].strip():
                    _text(YELLOW, "! %s shown in game as: %s" % (key, clean))

    def ui_queue(self):
        new = self.new_items()
        if not new:
            _text(GREY, "No songs added yet.")
            return
        _text(None, "Songs to add (%d):" % len(new))
        tflags = imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.row_bg | imgui.TableFlags_.resizable
        if imgui.begin_table("queue", 5, tflags):
            imgui.table_setup_column("#", imgui.TableColumnFlags_.width_fixed, 30)
            imgui.table_setup_column("Song")
            imgui.table_setup_column("Album")
            imgui.table_setup_column("Source")
            imgui.table_setup_column("", imgui.TableColumnFlags_.width_fixed, _bw("Play", "Remove"))
            imgui.table_headers_row()
            remove = None
            for p in new:
                i = self.items.index(p)
                imgui.table_next_row()
                imgui.table_next_column()
                imgui.text("%d" % (i + 1))
                imgui.table_next_column()
                if imgui.selectable("%s - %s##q%d" % (p.song.artist, p.song.title, i), self.editing is p)[0]:
                    self.editing = p
                imgui.table_next_column()
                _text(None, p.song.album)
                imgui.table_next_column()
                _text(GREY, p.info.describe() if p.info else (p.error or "?"))
                if p.info:
                    _tip("\n".join(t for _, t in p.info.quality_notes()))
                imgui.table_next_column()
                playing = self.playing == id(p)
                if imgui.small_button(("Stop##p%d" if playing else "Play##p%d") % i):
                    self.stop() if playing else self.preview_new(p)
                _tip("Preview the first 45 s exactly as the game will play it")
                imgui.same_line()
                if imgui.small_button("Remove##r%d" % i):
                    remove = i
            imgui.end_table()
            if remove is not None:
                self.remove_item(remove)
        _text(GREY, "Move, rename or replace any song in the song list on the right.")

    def ui_editor(self):
        it = self.editing
        if it is None or it not in self.items or not self.disc:
            return
        i = self.items.index(it)
        imgui.separator()
        _text(ACCENT, "Edit song %d" % (i + 1))
        imgui.same_line()
        if imgui.small_button("Close##edit"):
            self.editing = None
            return
        for key in core.FIELDS:
            imgui.set_next_item_width(300)
            cur = self.item_text(it, key)
            changed, v = imgui.input_text("%s (all languages)##e" % key.capitalize(), cur)
            if changed:
                self.rename(it, key, v)
        for l in self.disc.langs:
            if imgui.tree_node("%s text##%s" % (core.LANG_NAMES[l], l)):
                for key in core.FIELDS:
                    imgui.set_next_item_width(300)
                    changed, v = imgui.input_text("%s##%s%s" % (key.capitalize(), l, key), self.item_text(it, key, l))
                    if changed:
                        self.rename(it, key, v, l)
                imgui.tree_pop()
        if isinstance(it, core.SongRef) and imgui.small_button("Restore original names##edit"):
            self.restore_names(it)
        if self.disc:
            for key in core.FIELDS:
                v = self.item_text(it, key)
                clean = self.disc.sanitize(v)
                if v.strip() and clean != v.strip():
                    _text(YELLOW, "! %s shown in game as: %s" % (key, clean))

    def ui_step3(self):
        self.step_header(3, "Save", self.last_report is not None)
        imgui.set_next_item_width(-110)
        _, self.out_path = imgui.input_text("##out", self.out_path)
        imgui.same_line()
        if imgui.button("Browse...##out", imgui.ImVec2(100, 0)):
            self.pick_out()
        same = self.out_path and os.path.abspath(self.out_path) == os.path.abspath(self.iso_path)
        if same:
            _text(RED, "The output must be a new file (the source ISO is never overwritten).")
        elif self.out_path and os.path.exists(self.out_path):
            _text(YELLOW, "This file exists and will be replaced.")
        problem = self.save_problem()
        imgui.begin_disabled(problem is not None)
        imgui.push_style_color(imgui.Col_.button, _col((0.75, 0.42, 0.08, 1.0)))
        if imgui.button("Save new ISO", imgui.ImVec2(200, 36)):
            self.build()
        imgui.pop_style_color()
        imgui.end_disabled()
        if problem:
            _tip(problem[0].upper() + problem[1:])
        size = self.size_plan()
        if self.changed():
            enc = [it for it in self.items if isinstance(it, Pending) or it.audio]
            removed = self.disc.count - len({it.src for it in self.items if isinstance(it, core.SongRef)})
            _text(GREY, "%d songs: %d new, %d new audio, %d removed%s" % (
                len(self.items), len(self.new_items()), len(enc) - len(self.new_items()), removed,
                "; %.2f GB" % (size["bytes"] / 1e9) if size else ""))
            w = self.disc.save_warning(self.core_items())
            if w:
                _text(YELLOW, "! " + w)
        elif self.unlock_pending():
            _text(GREY, "Song list unchanged: only \"Allow switching any song OFF\" will be applied.")
        if size:
            _text(GREY if size["fits"] else YELLOW, ("" if size["fits"] else "! ") + size["text"])
        if self.job and (not self.job.done or self.job.title == "save new ISO"):
            imgui.progress_bar(self.job.progress if not self.job.done else 1.0, imgui.ImVec2(-1, 0),
                               self.job.message if not self.job.done else ("done" if self.job.ok else "failed"))
        if self.last_report:
            _text(GREEN, "Saved %s - %d songs. Burn it or load it in an emulator; "
                  "the new songs are in Driver Details > EA Trax." %
                  (os.path.basename(self.out_path), self.last_report["total_songs"]))

    def ui_song_list(self):
        d = self.disc
        if self.changed():
            if imgui.small_button("Undo all changes"):
                self.reset_list(keep_new=False)
                self.save_settings()
            imgui.same_line()
            _text(YELLOW, "Changes are applied when you save the new ISO.")
        h = imgui.get_content_region_avail().y * (0.42 if self.editing is not None else 0.62)
        tflags = imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.row_bg | imgui.TableFlags_.scroll_y
        action = None
        if imgui.begin_table("songs", 4, tflags, imgui.ImVec2(0, h)):
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_setup_column("#", imgui.TableColumnFlags_.width_fixed, 28)
            imgui.table_setup_column("Artist / Title / Album")
            imgui.table_setup_column("Length", imgui.TableColumnFlags_.width_fixed, imgui.calc_text_size("Length").x)
            imgui.table_setup_column("", imgui.TableColumnFlags_.width_fixed,
                                     _bw("Play", "Replace", "Edit", "X") + 2 * (imgui.get_frame_height() + 8))
            imgui.table_headers_row()
            for i, it in enumerate(self.items):
                imgui.table_next_row()
                imgui.table_next_column()
                new = isinstance(it, Pending)
                if new:
                    tags, length, color = ["new"], it.info.duration if it.info else 0, ACCENT
                else:
                    s = d.songs[it.src]
                    tags = [] if s.original else ["added with MusicKit"]
                    if it.src != i:
                        tags.append("was #%d" % (it.src + 1))
                    if it.audio:
                        tags.append("new audio")
                    if it.edited(d):
                        tags.append("renamed")
                    length = it.info.duration if it.audio and getattr(it, "info", None) else s.duration
                    color = GREY if s.original else ACCENT
                _text(color, "%d" % (i + 1))
                imgui.table_next_column()
                _text(None, "%s - %s" % (self.item_text(it, "artist"), self.item_text(it, "title")))
                album = self.item_text(it, "album").strip()
                if album:
                    _text(GREY, album)
                if tags:
                    tag = "[%s]" % ", ".join(tags)
                    if album and imgui.calc_text_size(album + "  " + tag).x < imgui.get_content_region_avail().x:
                        imgui.same_line()
                    _text(YELLOW, tag)
                imgui.table_next_column()
                imgui.text("%d:%02d" % (int(length) // 60, int(length) % 60))
                imgui.table_next_column()
                key = id(it) if new or it.audio else "orig%d" % it.src
                if imgui.small_button(("Stop##s%d" if self.playing == key else "Play##s%d") % i):
                    if self.playing == key:
                        self.stop()
                    elif new:
                        self.preview_new(it)
                    elif it.audio:
                        self.preview_new(it, it.audio)
                    else:
                        self.preview_original(it.src)
                imgui.same_line()
                if imgui.arrow_button("##up%d" % i, imgui.Dir.up):
                    action = ("move", i, i - 1)
                _tip("Move up")
                imgui.same_line()
                if imgui.arrow_button("##dn%d" % i, imgui.Dir.down):
                    action = ("move", i, i + 1)
                _tip("Move down")
                imgui.same_line()
                if imgui.small_button("Replace##s%d" % i):
                    action = ("replace", i)
                _tip("Use another audio file for this song (keeps its place and its saved settings)")
                imgui.same_line()
                if imgui.small_button("Edit##s%d" % i):
                    self.editing = it
                _tip("Change title / artist / album (per language)")
                imgui.same_line()
                if imgui.small_button("X##s%d" % i):
                    action = ("remove", i)
                _tip("Remove this song from the disc")
            imgui.end_table()
        if action:
            if action[0] == "move":
                self.move_item(action[1], action[2])
            elif action[0] == "replace":
                self.pick_replace(self.items[action[1]])
            else:
                self.remove_item(action[1])
        self.ui_editor()

    def ui_right(self):
        if imgui.collapsing_header("Song list (as it will be on the new disc)", imgui.TreeNodeFlags_.default_open):
            if not self.disc:
                _text(GREY, "(select an ISO)")
            else:
                self.ui_song_list()
        if imgui.collapsing_header("Options"):
            _, self.normalize = imgui.checkbox("Match loudness to the original songs", self.normalize)
            _tip("EBU R128 loudness of every new song is set to the median of the original soundtrack\n"
                 "(constant gain, -1 dBTP ceiling).")
            if self.disc and self.disc.unlocked:
                _text(GREEN, "This disc already lets you switch every song OFF.")
            else:
                _, self.unlock_off = imgui.checkbox("Allow switching any song OFF", self.unlock_off)
                _tip("In the original game the Song Manager only lets you switch a song OFF after it has played\n"
                     "to the end once. With this option every song can be switched OFF right away.\n"
                     "Can also be saved on its own, without changing songs.")
            if self.disc and imgui.button("Measure original loudness"):
                def run(job):
                    r = self.disc.reference_loudness(job.update)
                    self.ref_loudness = r
                    self.log("original songs: median %.1f LUFS (%.1f .. %.1f)" %
                             (r["median_lufs"], r["min_lufs"], r["max_lufs"]))
                self.start_job("loudness", run)
            if self.ref_loudness:
                _text(GREY, "median %.1f LUFS" % self.ref_loudness["median_lufs"])
            _text(GREY, "Text: up to %d characters, letters the game font has (accents ok)."
                  % core.MAX_TEXT)
        if imgui.collapsing_header("Log", imgui.TreeNodeFlags_.default_open):
            imgui.begin_child("log", imgui.ImVec2(0, 0))
            with self.log.lock:
                lines = list(self.log.lines)[-400:]
            for lv, t, line in lines:
                _text(RED if lv == "error" else GREY, "%s  %s" % (t, line))
            if imgui.get_scroll_y() >= imgui.get_scroll_max_y() - 4:
                imgui.set_scroll_here_y(1.0)
            imgui.end_child()


def _fatal(msg):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg[-3000:], "MusicKit error", 0x10)
    except Exception:
        pass


def main(shot=None, demo=None, size=None):
    try:
        settings = json.load(open(os.path.join(app_dir(), "settings.json"), encoding="utf-8"))
    except Exception:
        settings = {}
    if demo:
        settings = dict(demo)
    log = Log(os.path.join(app_dir(), "gui.log"))
    try:
        return _run(settings, log, shot, size)
    except Exception:
        tb = traceback.format_exc()
        log(tb, "error")
        if not shot:
            _fatal("MusicKit crashed:\n\n%s\n\nLog: %s" % (tb, os.path.join(app_dir(), "gui.log")))
        else:
            print(tb)
        return 1


def _run(settings, log, shot, size=None):
    if not glfw.init():
        raise RuntimeError("could not initialise GLFW")
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, gl.GL_TRUE)
    window = glfw.create_window(*(size or (1400, 820)), TITLE, None, None)
    if not window:
        raise RuntimeError("could not create an OpenGL 3.3 window")
    glfw.make_context_current(window)
    glfw.swap_interval(1)
    imgui.create_context()
    io = imgui.get_io()
    io.config_flags |= imgui.ConfigFlags_.nav_enable_keyboard
    io.set_ini_filename("")
    sx, _ = glfw.get_window_content_scale(window)
    if sx and sx > 1.01:
        imgui.get_style().scale_all_sizes(sx)
        imgui.get_style().font_scale_dpi = sx
    impl = GlfwRenderer(window)
    gui = MusicKitGui(log, settings)
    if shot:
        gui.save_settings = lambda: None
    dropped = []
    glfw.set_drop_callback(window, lambda w, paths: dropped.extend(paths))
    frames = 0
    while not glfw.window_should_close(window):
        glfw.poll_events()
        if not shot and not gui.busy() and not glfw.get_window_attrib(window, glfw.FOCUSED):
            glfw.wait_events_timeout(0.1)
        impl.process_inputs()
        if dropped:
            gui.drop(list(dropped))
            dropped.clear()
        imgui.new_frame()
        fb_w, fb_h = glfw.get_framebuffer_size(window)
        if fb_w == 0 or fb_h == 0:
            imgui.end_frame()
            glfw.wait_events_timeout(0.2)
            continue
        idle = not gui.busy()  # the frame drawn now shows the finished state
        gui.ui()
        gl.glViewport(0, 0, fb_w, fb_h)
        gl.glClearColor(0.1, 0.1, 0.12, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        imgui.render()
        impl.render(imgui.get_draw_data())
        frames += 1
        if shot and frames > 30 and idle:
            import numpy as np
            from PIL import Image
            px = gl.glReadPixels(0, 0, fb_w, fb_h, gl.GL_RGB, gl.GL_UNSIGNED_BYTE)
            img = np.frombuffer(px, dtype=np.uint8).reshape(fb_h, fb_w, 3)[::-1]
            Image.fromarray(img).save(shot)
            glfw.set_window_should_close(window, True)
        glfw.swap_buffers(window)
    gui.save_settings()
    try:
        gui.stop()
    except Exception:
        pass
    impl.shutdown()
    glfw.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
