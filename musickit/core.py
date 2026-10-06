"""MusicKit core: read the EA Trax soundtrack of a Burnout Revenge ISO and build a new ISO with a changed song list
(songs added, removed, reordered, renamed or with new audio).

Disc facts (details in README.md / NOTES.md):
- songs are identified by their position only: song i streams from /TRACKS/_EATRAX0.RWS segment i (i < 20) or
  /TRACKS/_EATRAX1.RWS segment i-20; its names are the string ids EATraxArtist<n>, EATraxSongTitle<n>,
  EATraxAlbum<n> (n = i+1) in /LANGUAGE/STRINGS/MAIN{UK,FR,GE,US}.BIN.
- song count + song table live in the boot executable (patched by elfpatch).
- the memory card profile keeps one settings byte per position 0..40, so removing / reordering songs among the
  first 41 positions shifts those saved settings to other songs.
- segments MusicKit writes carry a UUID starting with MARK, so a MusicKit image can be read back (which songs
  are original).
"""
import io
import json
import os
import statistics
import struct
import tempfile

import numpy as np

from . import audio, elfpatch, iso, rws, strtable

LANGS = ("UK", "FR", "GE", "US")          # all string tables MusicKit knows (a disc has a subset)
LANG_NAMES = {"UK": "English", "FR": "French", "GE": "German", "US": "English (USA)"}
ELF_PATH = "/SLES_535.07"                 # PAL default; the boot ELF is read from SYSTEM.CNF
REGIONS = {"SLES_535.07": "PAL (SLES-53507)", "SLUS_212.42": "USA (SLUS-21242)"}
RWS_FILES = ("/TRACKS/_EATRAX0.RWS", "/TRACKS/_EATRAX1.RWS")
SPLIT = 20
MARK = b"MusicKit"     # first 8 bytes of the RWS segment UUID of songs MusicKit encoded
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
    def __init__(self, index, title, artist, album, rws_index, segment, usable, original=True, names=None, flags=7):
        self.index = index
        self.title = title
        self.artist = artist
        self.album = album
        self.rws_index = rws_index
        self.segment = segment
        self.usable = usable
        self.original = original
        self.names = names or {}
        self.flags = flags

    @property
    def duration(self):
        return self.usable / 2 / 16 * 28 / 32000.0


FIELDS = ("title", "artist", "album")


class SongRef:
    """A song of the opened disc in the new song list, optionally with new audio (`audio` = file path) and new
    names (`title`/`artist`/`album` for every language, `names` = {lang: {field: text}} per language)."""

    def __init__(self, src, audio=None, title=None, artist=None, album=None, names=None):
        self.src = src
        self.audio = audio
        self.title = title
        self.artist = artist
        self.album = album
        self.names = names or {}

    def text(self, disc, lang, field):
        v = (self.names.get(lang) or {}).get(field)
        if v:
            return v
        v = getattr(self, field)
        if v:
            return v
        return disc.songs[self.src].names.get(lang, {}).get(field, "")

    def edited(self, disc):
        return any(self.text(disc, l, f) != disc.songs[self.src].names.get(l, {}).get(f, "")
                   for l in disc.langs for f in FIELDS)

    def to_json(self):
        return {"src": self.src, "audio": self.audio, "title": self.title, "artist": self.artist,
                "album": self.album, "names": self.names}

    @classmethod
    def from_json(cls, j):
        return cls(j["src"], j.get("audio"), j.get("title"), j.get("artist"), j.get("album"), j.get("names") or {})


def item_from_json(j):
    return SongRef.from_json(j) if "src" in j else NewSong.from_json(j)


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
        self.langs = tuple(l for l in LANGS if "/LANGUAGE/STRINGS/MAIN%s.BIN" % l in self.img.entries)
        if not self.langs:
            raise ValueError("no string tables found")
        self.changed_paths = {self.elf_path} | set(RWS_FILES) | {"/LANGUAGE/STRINGS/MAIN%s.BIN" % l for l in self.langs}
        self.tables = {l: strtable.StringTable(self.img.read_file("/LANGUAGE/STRINGS/MAIN%s.BIN" % l))
                       for l in self.langs}
        self.allowed = strtable.charset(self.tables.values())
        self.headers = []
        for p in RWS_FILES:
            self.headers.append(rws.RwsHeader(self._rws_header_blob(RWS_FILES.index(p))))
        table = elfpatch.read_table(self.elf)
        marked = any(seg.uuid.startswith(MARK) for h in self.headers for seg in h.segments)
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
            # original = encoded by the game makers; songs written by MusicKit 1.0 had no MARK and were appended
            original = seg is not None and not seg.uuid.startswith(MARK) and (
                marked or not self.patched or i < elfpatch.ORIGINAL_SONGS)
            self.songs.append(Song(i, uk["title"], uk["artist"], uk["album"].strip(), ri, seg_i,
                                   seg.usable if seg else 0, original=original, names=names, flags=table[i][2]))

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
        orig = [s.index for s in self.songs if s.original] or list(range(self.count))
        n = len(orig)
        for k, i in enumerate(orig):
            if progress:
                progress(k / n, "measuring loudness of original songs (%d/%d)" % (k + 1, n))
            pcm = self.decode_song(i)
            vals.append(audio.measure_loudness(pcm.astype(np.float64) / 32768.0, 32000))
        res = {"median_lufs": statistics.median(vals), "min_lufs": min(vals), "max_lufs": max(vals),
               "songs": vals}
        os.makedirs(CACHE_DIR, exist_ok=True)
        data[key] = res
        json.dump(data, open(cache, "w"), indent=1)
        return res

    # ------------------------------------------------------------------ build
    def current_list(self):
        """The song list of this disc as build items (nothing changed)."""
        return [SongRef(i) for i in range(self.count)]

    def is_unchanged(self, items):
        return len(items) == self.count and all(
            isinstance(it, SongRef) and it.src == i and not it.audio and not it.edited(self)
            for i, it in enumerate(items))

    def save_shift(self, items):
        """Positions (1-based) among the first 41 whose memory card settings will apply to a different song after
        the build (removing / moving songs; replacing audio or editing names keeps them)."""
        n = min(self.count, elfpatch.ORIGINAL_SONGS)
        return [i + 1 for i in range(n)
                if i >= len(items) or not (isinstance(items[i], SongRef) and items[i].src == i)]

    def save_warning(self, items):
        pos = self.save_shift(items)
        if not pos:
            return None
        return ("Removing or moving songs shifts the EA Trax settings saved on your memory card (on/off and "
                "menus/races per song): position(s) %s will use the setting saved for the song that was there "
                "before. Check the Song Manager after loading your save." % _ranges(pos))

    def build(self, new_songs, out_path, normalize=True, progress=None):
        """Add `new_songs` after the current songs and write a new ISO. Returns a report dict."""
        if not new_songs:
            raise ValueError("no songs to add")
        return self.build_list(self.current_list() + list(new_songs), out_path, normalize, progress)

    def build_list(self, items, out_path, normalize=True, progress=None):
        """Write a new ISO whose songs are `items` in this order: SongRef (a song of this disc, optionally with
        new audio / names) or NewSong (added). Returns a report dict."""
        prog = progress or (lambda f, m: None)
        if os.path.abspath(out_path) == os.path.abspath(self.path):
            raise ValueError("the output must be a new file (the source ISO is never modified)")
        reps, report = self.replacements(items, normalize, prog)
        placed = self.img.build(out_path, reps, lambda f, m: prog(0.55 + 0.45 * f, m))
        report["placed"] = {k: v for k, v in placed.items()}
        return report

    def replacements(self, items, normalize=True, progress=None):
        """The changed files {iso path: bytes or iso.Parts} for the song list `items`, and a report dict."""
        prog = progress or (lambda f, m: None)
        total = len(items)
        if total < 1:
            raise ValueError("at least one song must stay on the disc")
        if total > MAX_SONGS:
            raise ValueError("too many songs (max %d in total)" % MAX_SONGS)
        for it in items:
            if isinstance(it, SongRef) and not 0 <= it.src < self.count:
                raise ValueError("song %d is not on this disc" % (it.src + 1))
        report = {"songs": [], "save_shift": self.save_shift(items)}
        target = None
        enc = [k for k, it in enumerate(items) if isinstance(it, NewSong) or it.audio]
        if normalize and enc:
            ref = self.reference_loudness(lambda f, m: prog(0.15 * f, m))
            target = ref["median_lufs"]
            report["target_lufs"] = target
        # 1. segments: (payload: bytes or (path, offset, size), padded size, usable, uuid, info template)
        block = self.headers[0].block_size
        segs = []
        for k, it in enumerate(items):
            if k in enc:
                path = it.path if isinstance(it, NewSong) else it.audio
                title = it.title if isinstance(it, NewSong) else it.text(self, self.langs[0], "title")
                prog(0.15 + 0.35 * enc.index(k) / len(enc), "encoding %s" % (title or os.path.basename(path)))
                pcm, in_lufs = audio.prepare(path, target, normalize)
                payload, usable = rws.encode_segment(pcm, block)
                out_lufs = audio.measure_loudness(pcm.astype(np.float64) / 32768.0, 32000) if len(pcm) > 32000 else None
                report["songs"].append({"index": k, "title": title, "seconds": len(pcm) / 32000.0,
                                        "input_lufs": in_lufs, "output_lufs": out_lufs, "bytes": len(payload),
                                        "replaced": isinstance(it, SongRef)})
                segs.append((payload, len(payload), usable, MARK + os.urandom(8), self.headers[1].segments[-1].info))
            else:
                s = self.songs[it.src]
                hdr = self.headers[s.rws_index]
                seg = hdr.segments[s.segment]
                e = self.img.entries[RWS_FILES[s.rws_index]]
                src = (self.path, e.lsn * iso.SECTOR + hdr.segment_file_offset(seg), seg.size)
                uid = seg.uuid if s.original or seg.uuid.startswith(MARK) else MARK + seg.uuid[8:]
                segs.append((src, seg.size, seg.usable, uid, seg.info))
        # 2. stream files (a file whose songs did not change is kept as it is)
        prog(0.5, "building stream files")
        reps = {}
        for ri in range(len(RWS_FILES)):
            lo, hi = (0, SPLIT) if ri == 0 else (SPLIT, total)
            part = segs[lo:hi]
            if not part:
                continue     # never opened by the game when no song position maps to it
            old = self.headers[ri].segments
            if len(part) == len(old) and all(
                    isinstance(items[lo + j], SongRef) and items[lo + j].src == lo + j and lo + j not in enc
                    and part[j][3] == old[j].uuid for j in range(len(part))):
                continue
            hdr = rws.RwsHeader(self._rws_header_blob(ri))
            hdr.segments = []
            parts = []
            off = 0
            for j, (src, size, usable, uid, info) in enumerate(part):
                info = bytearray(info)
                struct.pack_into("<II", info, 0x18, size, off)
                name = "%02d" % (lo + j)
                hdr.segments.append(rws.Segment(info, usable, uid, name, rws._pad_string(name)))
                parts.append(src)
                off += size
            reps[RWS_FILES[ri]] = iso.Parts([hdr.build()] + parts)
        # 3. strings
        for l in self.langs:
            t = strtable.StringTable(self.img.read_file("/LANGUAGE/STRINGS/MAIN%s.BIN" % l))
            for k, it in enumerate(items):
                if isinstance(it, NewSong):
                    vals = {f: self.sanitize(it.text(l, f)) for f in FIELDS}
                else:
                    src = self.songs[it.src].names.get(l, {})
                    vals = {}
                    for f in FIELDS:
                        v = it.text(self, l, f)
                        vals[f] = v if v == src.get(f, "") else self.sanitize(v)
                t.set("EATraxSongTitle%d" % (k + 1), vals["title"] or "Untitled")
                t.set("EATraxArtist%d" % (k + 1), vals["artist"] or " ")
                t.set("EATraxAlbum%d" % (k + 1), vals["album"] or " ")
            for n in range(max(total, elfpatch.ORIGINAL_SONGS) + 1, self.count + 1):   # added ids past the end
                for sid in ("EATraxSongTitle%d", "EATraxArtist%d", "EATraxAlbum%d"):
                    t.delete(sid % n)
            reps["/LANGUAGE/STRINGS/MAIN%s.BIN" % l] = t.build()
        # 4. executable: song count + table (flags travel with their song)
        flags = [7 if isinstance(it, NewSong) else self.songs[it.src].flags for it in items]
        reps[self.elf_path] = elfpatch.set_table(self.elf, flags)
        assert elfpatch.crc(reps[self.elf_path]) == self.crc   # PCSX2 game CRC unchanged
        report["total_songs"] = total
        report["removed"] = self.count - len({it.src for it in items if isinstance(it, SongRef)})
        return reps, report

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


def _ranges(nums):
    out = []
    for n in nums:
        if out and out[-1][1] == n - 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join("%d" % a if a == b else "%d-%d" % (a, b) for a, b in out)
