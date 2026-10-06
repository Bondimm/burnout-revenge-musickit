"""GUI flow test without a window: drives the MusicKit window's logic (MusicKitGui) the way its buttons, file
dialogs and drag-and-drop do - select ISO, add, replace, rename, remove, move, save (the real background job),
validate, reopen the saved image, change it again, save again.

Writes two disc images of ~4 GB (one at a time) into pytest's temp folder (use --basetemp to choose it) and only
reads the source ISO. Runs only with MUSICKIT_FULL_TEST=1 and MUSICKIT_ISO set:

    set MUSICKIT_FULL_TEST=1
    set MUSICKIT_ISO=D:\\path\\to\\your.iso
    .venv\\Scripts\\python -m pytest tests\\test_gui_flow.py -v
"""
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

ISO = os.environ.get("MUSICKIT_ISO", "")
pytestmark = pytest.mark.skipif(not (os.environ.get("MUSICKIT_FULL_TEST") and ISO and os.path.exists(ISO)),
                                reason="set MUSICKIT_FULL_TEST=1 and MUSICKIT_ISO")


def _wait(g, timeout=3600):
    t = time.time()
    while g.busy():
        assert time.time() - t < timeout, "job '%s' did not finish" % g.job.title
        time.sleep(0.2)
    return g.job


def _errors(log):
    with log.lock:
        return [line for lv, _, line in log.lines if lv == "error"]


def _texts(g):
    """Every text of the song list as the game will show it: [(lang, field) -> text] per song."""
    return [{(l, f): g.disc.sanitize(g.item_text(it, f, l)).strip() for l in g.disc.langs
             for f in ("title", "artist", "album")} for it in g.items]


def _close(g):
    if g is not None and g.disc is not None:
        g.disc.img.f.close()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))   # settings + log of this test only
    import make_test_songs
    songs = tmp_path / "songs"
    make_test_songs.main(str(songs))
    return tmp_path, str(songs / "test_chords_44k_24bit.flac"), str(songs / "test_chords_22k_mono.wav")


def test_gui_flow(setup):
    from musickit import core, gui, validate
    tmp, flac, wav = setup
    quiet = lambda *a: None   # noqa: E731
    log = gui.Log(os.path.join(gui.app_dir(), "gui.log"))
    g = gui.MusicKitGui(log, {})
    g2 = None
    out1, out2 = str(tmp / "flow step1.iso"), str(tmp / "flow step2.iso")
    try:
        # ---- nothing selected yet: Save disabled, pressing it only logs why
        assert g.disc is None and g.disc_error and g.job is None
        assert g.save_problem() and not g.can_save()
        g.build()
        assert g.job is None and _errors(log)

        # ---- errors end up in the log / the status line, never as an exception
        g.load_disc(str(tmp / "missing.iso"))
        assert g.disc is None and g.disc_error == "ISO not found"
        bogus = tmp / "bogus.iso"
        bogus.write_bytes(b"\0" * (64 << 10))
        n_err = len(_errors(log))
        g.drop([str(bogus)])
        assert not _wait(g).ok and g.disc is None and g.disc_error
        assert len(_errors(log)) > n_err

        # ---- 1) select the ISO (drag and drop does the same as Browse...)
        g.drop([ISO])
        assert _wait(g).ok and g.disc is not None and g.iso_path == ISO
        d0 = g.disc
        n0 = d0.count
        langs = d0.langs
        assert len(g.items) == n0 and not g.new_items()
        assert g.out_path == core.default_output(ISO)
        assert not g.can_save() and "nothing" in g.save_problem()

        # ---- 2) add a song: drop / choose the file, fill title, artist, album, press "Add song"
        notaudio = tmp / "not audio.mp3"
        notaudio.write_text("this is not audio")
        g.drop([str(notaudio)])
        assert g.form_error and g.form_info is None
        n_err = len(_errors(log))
        g.add_form_song()                                   # the button is disabled here; pressing anyway only logs
        assert len(g.items) == n0 and len(_errors(log)) > n_err
        g.drop([flac])
        assert g.form["path"] == flac and g.form_info is not None and not g.form_error
        g.form.update(title="GUI Flow Song", artist="MusicKit", album="Flow Album")
        g.add_form_song()
        assert len(g.items) == n0 + 1 and g.items[-1].song.title == "GUI Flow Song"
        assert g.form["path"] == "" and g.can_save() and g.save_problem() is None

        # ---- replace an original (song 3), a broken file is refused with a log line
        n_err = len(_errors(log))
        g.set_replacement(g.items[2], str(notaudio))
        assert g.items[2].audio is None and len(_errors(log)) > n_err
        g.set_replacement(g.items[2], wav)
        assert g.items[2].audio == wav

        # ---- rename: all languages (song 5), one language (song 6), the new song in one language
        g.rename(g.items[4], "title", "Renamed All")
        assert all(g.item_text(g.items[4], "title", l) == "Renamed All" for l in langs)
        g.rename(g.items[5], "artist", "One Language", langs[-1])
        assert g.item_text(g.items[5], "artist", langs[-1]) == "One Language"
        if len(langs) > 1:
            assert g.item_text(g.items[5], "artist", langs[0]) != "One Language"
        g.rename(g.items[-1], "album", "Album Other Language", langs[-1])
        g.rename(g.items[7], "title", "Temporary")             # restore original names
        g.restore_names(g.items[7])
        assert not g.items[7].edited(d0)

        # ---- remove song 2, move the new song up twice, the first song down, out of range moves do nothing
        g.remove_item(1)
        assert len(g.items) == n0
        new = g.items[-1]
        g.move_item(len(g.items) - 1, len(g.items) - 2)
        g.move_item(len(g.items) - 2, len(g.items) - 3)
        assert g.items[-3] is new
        first = g.items[0]
        g.move_item(0, 1)
        assert g.items[1] is first
        order = list(g.items)
        g.move_item(0, -1)
        g.move_item(len(g.items) - 1, len(g.items))
        assert g.items == order

        unlock = hasattr(g, "unlock_off")                       # Revenge / Burnout 3: "Allow switching any song OFF"
        if unlock:
            g.unlock_off = True
            assert g.unlock_pending()

        # ---- settings round trip (what the next start of the window restores)
        g.save_settings()
        saved = json.load(open(os.path.join(gui.app_dir(), "settings.json"), encoding="utf-8"))
        g2 = gui.MusicKitGui(log, saved)
        assert _wait(g2).ok
        assert [it.to_json() for it in g2.core_items()] == [it.to_json() for it in g.core_items()]
        assert g2.out_path == g.out_path and g2.normalize == g.normalize
        if unlock:
            assert g2.unlock_off
        _close(g2)
        g2 = None

        # ---- 3) Save: output must differ from the source, may not be empty
        g.out_path = ISO
        assert "different" in g.save_problem()
        job = g.job
        g.build()
        assert g.job is job
        g.out_path = ""
        assert g.save_problem()
        g.out_path = out1
        assert g.save_problem() is None
        expect = _texts(g)
        n1 = len(g.items)
        g.build()
        assert g.busy() and g.save_problem()                    # Save disabled while saving
        n_err = len(_errors(log))
        g.load_disc(out2)                                       # opening another ISO meanwhile is refused
        assert g.disc is d0 and len(_errors(log)) > n_err
        assert _wait(g).ok, _errors(log)[-5:]
        assert g.last_report and g.last_report["total_songs"] == n1
        if unlock:
            assert g.last_report["unlocked"]
        assert validate.validate(ISO, out1, log=quiet)

        # ---- reopen the saved image in the window: same list, nothing queued twice, nothing to save
        g.drop([out1])
        assert _wait(g).ok and g.disc.path == out1
        assert g.disc.count == n1 and not g.new_items() and _texts(g) == expect
        assert not g.can_save()
        if unlock:
            assert g.disc.unlocked and not g.unlock_pending()
        new_pos = [k for k, s in enumerate(g.disc.songs) if not s.original]
        assert len(new_pos) == 2                                # the added song + the replaced audio

        # ---- change again: rename, move, remove, add; save; validate against the first output
        g.rename(g.items[0], "title", "Renamed Twice")
        g.move_item(new_pos[-1], 0)
        g.remove_item(len(g.items) - 1)
        g.set_form_file(wav)
        g.form.update(title="Added Later", artist="MusicKit", album="")
        g.add_form_song()
        g.out_path = out2
        assert g.save_problem() is None
        expect2 = _texts(g)
        n2 = len(g.items)
        g.build()
        assert _wait(g).ok, _errors(log)[-5:]
        assert validate.validate(out1, out2, log=quiet)
        g.drop([out2])
        assert _wait(g).ok and g.disc.path == out2
        assert g.disc.count == n2 and _texts(g) == expect2 and not g.can_save()
        assert sum(not s.original for s in g.disc.songs) == 3
        if unlock:
            assert g.disc.unlocked                              # the unlock stays on later edits
    finally:
        _close(g2)
        _close(g)
        if log.file:
            log.file.close()
        for p in (out1, out2):
            if os.path.exists(p):
                os.remove(p)
