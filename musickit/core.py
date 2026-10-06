"""MusicKit core: read the EA Trax soundtrack of a Burnout Revenge (PAL) ISO and build a new ISO with added songs.

Disc facts (details in README.md / NOTES.md):
- songs 0..19 = segments of /TRACKS/_EATRAX0.RWS, songs 20.. = segments of /TRACKS/_EATRAX1.RWS (index-20);
  new songs are appended to _EATRAX1.RWS.
- song count + song table live in SLES_535.07 (patched by elfpatch).
- names: string ids EATraxArtist<n>, EATraxSongTitle<n>, EATraxAlbum<n> (n = index+1) in
  /LANGUAGE/STRINGS/MAIN{UK,FR,GE}.BIN.
"""
import io
import json
import os
import statistics
import tempfile

import numpy as np

from . import audio, elfpatch, iso, rws, strtable

LANGS = ("UK", "FR", "GE", "US")          # all string tables MusicKit knows (a disc has a subset)
LANG_NAMES = {"UK": "English", "FR": "French", "GE": "German", "US": "English (USA)"}
ELF_PATH = "/SLES_535.07"                 # PAL default; the boot ELF is read from SYSTEM.CNF
REGIONS = {"SLES_535.07": "PAL (SLES-53507)", "SLUS_212.42": "USA (SLUS-21242)"}
RWS_FILES = ("/TRACKS/_EATRAX0.RWS", "/TRACKS/_EATRAX1.RWS")
SPLIT = 20
MAX_TEXT = 60          # longest original title is 59 characters
MAX_SONGS = 100        # keeps the _EATRAX1.RWS header below 0x2000 bytes (the size MOVIES.RWS uses)
DEFAULT_ISO = ""  # no default: the user picks their own disc image
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")


def default_output(src):
    base, ext = os.path.splitext(src)
    if base.endswith(" (MusicKit)"):
        return base + ext
    return base + " (MusicKit)" + (ext or ".iso")


class Song:
    def __init__(self, index, title, artist, album, rws_index, segment, usable, original=True, names=None):
        self.index = index
        self.title = title
        self.artist = artist
        self.album = album
        self.rws_index = rws_index
        self.segment = segment
        self.usable = usable
        self.original = original
        self.names = names or {}

    @property
    def duration(self):
        return self.usable / 2 / 16 * 28 / 32000.0


class NewSong:
    """A song to add: source file + names (per language; missing languages use the main text)."""

    def __init__(self, path, title="", artist="", album="", names=None):
        self.path = path
        self.title = title
        self.artist = artist
        self.album = album
        self.names = names or {}     # lang -> {"title":..,"artist":..,"album":..}
        self.info = None

    def probe(self):
        self.info = audio.AudioInfo(self.path)
        return self.info

    def text(self, lang, field):
        v = (self.names.get(lang) or {}).get(field)
        return v if v else getattr(self, field)

    def to_json(self):
        return {"path": self.path, "title": self.title, "artist": self.artist, "album": self.album,
                "names": self.names}

    @classmethod
    def from_json(cls, j):
        return cls(j["path"], j.get("title", ""), j.get("artist", ""), j.get("album", ""), j.get("names") or {})


class Disc:
    def __init__(self, path):
        self.path = path
        self.img = iso.IsoImage(path)
        self.elf_path = self._boot_elf()
        for p in (self.elf_path,) + RWS_FILES:
            if p not in self.img.entries:
                raise ValueError("not a Burnout Revenge disc (missing %s)" % p)
        name = self.elf_path.lstrip("/")
        if name not in REGIONS:
            raise ValueError("unsupported Burnout Revenge version (%s); supported: PAL SLES-53507, USA SLUS-21242" % name)
        self.region = REGIONS[name]
        self.elf = self.img.read_file(self.elf_path)
        self.crc = elfpatch.crc(self.elf)
        try:
            self.count = elfpatch.read_song_count(self.elf)
        except Exception as exc:
            raise ValueError("unsupported game executable %s: %s" % (name, exc))
        self.patched = elfpatch.is_patched(self.elf)
        self.unlocked = elfpatch.is_unlocked(self.elf)
        self.langs = tuple(l for l in LANGS if "/LANGUAGE/STRINGS/MAIN%s.BIN" % l in self.img.entries)
        if not self.langs:
            raise ValueError("no string tables found")
        self.changed_paths = {self.elf_path, RWS_FILES[1]} | {"/LANGUAGE/STRINGS/MAIN%s.BIN" % l for l in self.langs}
        self.tables = {l: strtable.StringTable(self.img.read_file("/LANGUAGE/STRINGS/MAIN%s.BIN" % l))
                       for l in self.langs}
        self.allowed = strtable.charset(self.tables.values())
        self.headers = []
        for p in RWS_FILES:
            self.headers.append(rws.RwsHeader(self._rws_header_blob(RWS_FILES.index(p))))
        self.songs = []
        for i in range(self.count):
            ri = 0 if i < SPLIT else 1
            seg_i = i if i < SPLIT else i - SPLIT
            hdr = self.headers[ri]
            seg = hdr.segments[seg_i] if seg_i < len(hdr.segments) else None
            names = {}
            for l in self.langs:
                t = self.tables[l]
                names[l] = {"title": t.get("EATraxSongTitle%d" % (i + 1)) or "",
                            "artist": t.get("EATraxArtist%d" % (i + 1)) or "",
                            "album": t.get("EATraxAlbum%d" % (i + 1)) or ""}
            uk = names[self.langs[0]]
            self.songs.append(Song(i, uk["title"], uk["artist"], uk["album"].strip(), ri, seg_i,
                                   seg.usable if seg else 0, original=i < elfpatch.ORIGINAL_SONGS, names=names))

    def sanitize(self, text):
        return strtable.sanitize(text, self.allowed, MAX_TEXT)

    def decode_song(self, index):
        s = self.songs[index]
        hdr = self.headers[s.rws_index]
        seg = hdr.segments[s.segment]
        f, e = self.img.open_file(RWS_FILES[s.rws_index])
        with f:
            f.seek(e.lsn * iso.SECTOR + hdr.segment_file_offset(seg))
            payload = f.read(seg.size)
        return rws.decode_segment(payload, seg.usable, hdr.block_size, hdr.channels)

    def preview_wav(self, index, seconds=None):
        import soundfile as sf
        pcm = self.decode_song(index)
        if seconds:
            pcm = pcm[: int(seconds * 32000)]
        path = os.path.join(tempfile.gettempdir(), "musickit_preview_%d.wav" % index)
        sf.write(path, pcm, 32000)
        return path

    def reference_loudness(self, progress=None):
        """Median integrated loudness of the original songs (cached per image)."""
        st = os.stat(self.path)
        key = "%s|%d|%d" % (os.path.basename(self.path), st.st_size, int(st.st_mtime))
        cache = os.path.join(CACHE_DIR, "loudness.json")
        try:
            data = json.load(open(cache))
        except Exception:
            data = {}
        if key in data:
            return data[key]
        vals = []
        n = min(self.count, elfpatch.ORIGINAL_SONGS)
        for i in range(n):
            if progress:
                progress(i / n, "measuring loudness of original songs (%d/%d)" % (i + 1, n))
            pcm = self.decode_song(i)
            vals.append(audio.measure_loudness(pcm.astype(np.float64) / 32768.0, 32000))
        res = {"median_lufs": statistics.median(vals), "min_lufs": min(vals), "max_lufs": max(vals),
               "songs": vals}
        os.makedirs(CACHE_DIR, exist_ok=True)
        data[key] = res
        json.dump(data, open(cache, "w"), indent=1)
        return res

    # ------------------------------------------------------------------ build
    def build(self, new_songs, out_path, normalize=True, progress=None, unlock_off=False):
        """Encode `new_songs` and write a new ISO. Returns a report dict.
        unlock_off: let the Song Manager switch every song OFF (the game normally refuses OFF for songs not
        heard to the end yet). Can be the only change."""
        prog = progress or (lambda f, m: None)
        unlock_off = unlock_off and not self.unlocked
        if not new_songs and not unlock_off:
            raise ValueError("nothing to do: add songs or choose the switch-off unlock")
        if os.path.abspath(out_path) == os.path.abspath(self.path):
            raise ValueError("the output must be a new file (the source ISO is never modified)")
        total = self.count + len(new_songs)
        if total > MAX_SONGS:
            raise ValueError("too many songs (max %d in total, %d new)" % (MAX_SONGS, MAX_SONGS - self.count))
        report = {"songs": [], "unlocked": unlock_off or self.unlocked}
        reps = {}
        if new_songs:
            self._build_songs(new_songs, normalize, prog, report, reps)
        # 4. executable
        elf = self.elf
        if new_songs:
            elf = elfpatch.extend(elf, total) if self.patched else elfpatch.patch(elf, total)
        if unlock_off:
            elf = elfpatch.unlock(elf)
        reps[self.elf_path] = elf
        assert elfpatch.crc(reps[self.elf_path]) == self.crc   # PCSX2 game CRC unchanged
        # 5. image
        placed = self.img.build(out_path, reps, lambda f, m: prog(0.55 + 0.45 * f, m))
        report["placed"] = {k: v for k, v in placed.items()}
        report["total_songs"] = total
        return report

    def _build_songs(self, new_songs, normalize, prog, report, reps):
        """Steps 1-3 of build(): encode the new songs, new _EATRAX1.RWS, string tables (into `reps`)."""
        target = None
        if normalize:
            ref = self.reference_loudness(lambda f, m: prog(0.15 * f, m))
            target = ref["median_lufs"]
            report["target_lufs"] = target
        # 1. encode songs
        hdr_rd = rws.RwsHeader(self._rws_header_blob(1))
        payloads = []
        for k, s in enumerate(new_songs):
            prog(0.15 + 0.35 * k / len(new_songs), "encoding %s" % (s.title or os.path.basename(s.path)))
            pcm, in_lufs = audio.prepare(s.path, target, normalize)
            payload, usable = rws.encode_segment(pcm, hdr_rd.block_size)
            payloads.append((payload, usable))
            out_lufs = audio.measure_loudness(pcm.astype(np.float64) / 32768.0, 32000) if len(pcm) > 32000 else None
            report["songs"].append({"index": self.count + k, "title": s.title, "seconds": len(pcm) / 32000.0,
                                    "input_lufs": in_lufs, "output_lufs": out_lufs, "bytes": len(payload)})
        # 2. new _EATRAX1.RWS (existing header + appended segments)
        prog(0.5, "building _EATRAX1.RWS")
        for k, (payload, usable) in enumerate(payloads):
            idx = self.count + k
            hdr_rd.add_segment("%02d" % idx, len(payload), usable)
        new_head = hdr_rd.build()
        old_hdr = self.headers[1]
        f, e = self.img.open_file(RWS_FILES[1])
        with f:
            f.seek(e.lsn * iso.SECTOR + old_hdr.data_offset + 12)
            old_data = f.read(old_hdr.data_size())
        rws_blob = new_head + old_data + b"".join(p for p, _ in payloads)
        # 3. strings
        reps[RWS_FILES[1]] = rws_blob
        for l in self.langs:
            t = strtable.StringTable(self.img.read_file("/LANGUAGE/STRINGS/MAIN%s.BIN" % l))
            for k, s in enumerate(new_songs):
                n = self.count + k + 1
                t.set("EATraxSongTitle%d" % n, self.sanitize(s.text(l, "title")) or "Untitled")
                t.set("EATraxArtist%d" % n, self.sanitize(s.text(l, "artist")) or " ")
                t.set("EATraxAlbum%d" % n, self.sanitize(s.text(l, "album")) or " ")
            reps["/LANGUAGE/STRINGS/MAIN%s.BIN" % l] = t.build()

    def _boot_elf(self):
        try:
            cnf = self.img.read_file("/SYSTEM.CNF").decode("latin-1")
            for line in cnf.splitlines():
                if line.replace(" ", "").upper().startswith("BOOT2="):
                    return "/" + line.replace("/", "\\").split("\\")[-1].split(";")[0].strip().upper()
        except KeyError:
            pass
        return ELF_PATH

    def _rws_header_blob(self, i):
        f, e = self.img.open_file(RWS_FILES[i])
        with f:
            head = f.read(0x20)
            hs = int.from_bytes(head[0x10:0x14], "little")
            f.seek(e.lsn * iso.SECTOR)
            return f.read(0x18 + hs + 12)
