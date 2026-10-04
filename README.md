# music-tools

A small toolkit for cleaning up a local music library, built for collections that come from
YouTube downloads (`yt-dlp`), where cover art, artist pictures, lyrics and tags are usually
missing or wrong. Every tool previews before it changes anything, runs on several threads, and
reads one shared `config.toml`, so you set your music folder once.

```bash
./setup.sh                  # once: installs the packages and creates config.toml
mt dir "/path/to/Music"     # point it at your library
mt auto --dry-run           # preview everything; then run  mt auto
```

## Quick commands

Set up the `mt` alias once (last section of "The `mt` command" below), then type these from anywhere.

| Type this | It does |
|---|---|
| `mt` | opens the menu |
| `mt auto` | everything: tidy folders, then art, artist pictures and lyrics (asks once; `--dry-run` previews) |
| `mt tidy` | merges duplicate folders and deletes junk (shows the list, asks first; `--dry-run` previews) |
| `mt undo` | puts back what the last tidy moved |
| `mt lyrics` / `mt covers` / `mt artists` | find lyrics / add album art / find artist pictures |
| `mt check` | one-screen health check |
| `mt help COMMAND` | every option of one command |

The older names (`all`, `art`, `layout`, `stats`, ...) still work, and small slips in flags are forgiven
(`--dryrun`, `--dry_run` and `-n` all mean `--dry-run`; `-y` means `--yes`). A mistyped command gets a
"Did you mean ...?".

## What's in the box

| Script | Command | What it does |
| --- | --- | --- |
| `fix_album_art.py` | `mt covers` | Finds songs with no embedded cover art and adds the matching YouTube thumbnail |
| `find_artist_art.py` | `mt artists` | Finds a picture for every artist (Deezer, with a YouTube fallback) and saves it as `<Artist>.jpg`, or `Artist/artist.jpg` |
| `find_lyrics.py` | `mt lyrics` | Finds lyrics (lrclib.net) and saves original + romanization + English next to each song |
| `fix_misidentified_tags.py` | `mt fix` | Fixes songs that MusicBrainz Picard tagged as the wrong album track, rebuilding tags from the filename |
| `find_duplicates.py` | `mt dupes` | Reports songs that are probably the same recording saved twice (report only; nothing is deleted) |
| `library_layout.py` | `mt tidy` | Finds duplicate album folders, junk and odd names; merges and cleans them with an undo file |
| `organize_music.py` | `mt organize` | Sorts songs and companion lyrics/images into `Artist/Album/` folders for Jellyfin / Plex |
| `run_all.py` | `mt auto` | Runs the tools above in the smart order, asking once |
| `library_stats.py` | `mt check` | A one-screen, read-only health check |

## Requirements

- Python 3.11 or newer (`common.py` exits with a clear message on anything older).
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`brew install yt-dlp`) for the YouTube searches.
- Python packages from `requirements.txt`: `mutagen` (tags), `Pillow` (cover art), `tqdm` (progress bars).
  `./setup.sh` installs them.
- Optional, for lyrics romanization: `cutlet` + `unidic-lite` (Japanese, about 250 MB), `pypinyin`
  (Chinese) and `korean-romanizer` (Korean). Without them that column is left out (and the tool says so);
  Korean falls back to a smaller built-in table. Russian needs no package.

## Setup

```bash
./setup.sh
```

This installs the packages into a private `.venv` folder (so it works even where `pip install` is
blocked, e.g. Homebrew Python) and creates `config.toml` from `config.example.toml`. The
`music-tools` / `mt` command uses that `.venv` automatically; the scripts run on their own with
`.venv/bin/python`, e.g. `.venv/bin/python fix_album_art.py --list-missing`. If the packages are already
installed, plain `python3` works too.

## The `mt` command

`mt` on its own opens an interactive menu: arrow keys to move, Enter to choose, Space to switch options
on/off, `q` to go back. From there you can do everything below, switch folders (type a path with Tab
completion or drag a folder in from Finder), change every setting, and read the logs of previous runs.

```
  ╔╦╗╦ ╦╔═╗╦╔═╗  ╔╦╗╔═╗╔═╗╦  ╔═╗     /\_/\   ♪
  ║║║║ ║╚═╗║║     ║ ║ ║║ ║║  ╚═╗    ( o.o )
  ╩ ╩╚═╝╚═╝╩╚═╝   ╩ ╚═╝╚═╝╩═╝╚═╝     > ^ <  ~
  ────────────────────────────
  ♫ cover art + artist pictures + tag fixer

  Music folder  /path/to/Music  ✓
  Art folders   YouTube  ✓

 ❯  1 Find songs without art    just lists them, changes nothing
    2 Add missing art           search YouTube and add covers
    3 Retry failed songs        songs that didn't get art last time
    4 Find artist pictures      a photo for every artist, from Deezer
    5 Find lyrics               translate foreign songs: original + romaji + English
    6 Fix wrong tags            rebuild tags from filenames
    7 Find duplicate songs      report only; nothing is ever deleted
    8 Organize library          sort songs into Artist/Album for Jellyfin
    9 All-in-one run            art + artist pictures + lyrics in one go
   10 Library stats             one-screen health check, read-only
   11 Check and tidy folders    duplicate albums, junk files; merge them all in one go
   12 Folders                   change which folders are used
   13 Settings                  matching, cropping, tag options…
   14 Logs                      see what previous runs did
   15 Help                      all commands
      Quit
```

The main menu has a colour-faded title and a cat that listens along. In a narrow window the cat steps
aside. To turn the animation off set `MUSIC_TOOLS_NO_ANIMATION=1` (and `NO_COLOR=1` for no colours).
Every run is saved to `logs/` (the last 200 are kept; turn off with `save_logs = false`).

Everything is also available as direct commands; `mt help` lists them all.

```bash
mt where                          # show current folders and config
mt dir "/path/to/Music"           # set the music library folder
mt folders use "YouTube" "Mixes"  # switch the folders checked for art
mt folders add ~/Downloads/Music  # add one
mt folders remove "Mixes"         # remove one
mt set album_art.min_match 0.9    # change any setting
mt missing                        # list songs without art
mt covers                         # add missing art
mt artists                        # find a picture for every artist
mt lyrics                         # find and translate lyrics
mt fix --only-severe --apply      # fix wrong tags
mt dupes                          # find likely duplicate songs
mt tidy                           # merge duplicate folders, delete junk (asks first)
mt undo                           # reverse the last tidy
mt organize                       # sort songs into Artist/Album for Jellyfin
mt auto                           # everything, in the smart order
mt check                          # one-screen library health check
```

To run it from anywhere as `mt`, add an alias to your shell (from the repo folder):

```bash
echo "alias mt='python3 $PWD/music-tools'" >> ~/.zshrc && source ~/.zshrc
```

After that every example in this README works as `mt <command>`. Without the alias use `./music-tools`.

## Choosing folders and settings

Use the `music-tools` commands above, or edit `config.toml` directly
(`./music-tools edit` opens it):

```toml
music_dir = "/path/to/your/Music"         # library root

[album_art]
folders = ["YouTube", "Downloads/Mixes"]  # relative to music_dir, or absolute paths
```

Every option is explained in `config.example.toml`. You can also skip the config
and pass folders directly: `python3 fix_album_art.py "/some/folder" "/other/folder"`.
Command-line flags always override the config. The config is looked up in this
order: `--config PATH`, `$MUSIC_TOOLS_CONFIG`, `config.toml` next to the scripts,
`~/.config/music-tools/config.toml`. `$MUSIC_DIR` overrides `music_dir`.

## Safety

- Nothing is written unless needed: `--list-missing`, `--dry-run` and the tag
  fixer's default preview mode never modify files.
- Pressing Ctrl-C lets files that are being written finish, then stops. Songs
  that weren't done are saved for `--retry`.
- One unreadable, corrupt or read-only file is reported and skipped; it never
  stops the run. Bad config values are reported with the setting name.

## Speed

The slow parts (waiting on the network or the drive) run on several threads at
once, so a big library isn't handled one song at a time. `workers` is how many
(default 8, 1–64): change it for one run with `--workers N`, or for good with
`mt set workers 16`.

| Command | What runs in parallel |
| --- | --- |
| `mt art` | checking songs for art, YouTube searches and thumbnail downloads (`workers`); writing covers into files and converting `.webm` (up to 4) |
| `mt artists` | reading artists from tags (`workers`); then two pools working at the same time, one searching Deezer / YouTube and one downloading pictures (`workers` each), so a slow download never holds up the next search; the parts of an `A & B` name are looked up side by side (up to 4); saving pictures (up to 4) |
| `mt lyrics` | looking up and translating songs (`workers`); writing the lyrics files (up to 4) |
| `mt tags` | reading and checking files (`workers`) |

Writes are capped at 4 at a time so a slow drive isn't thrashed. Ctrl-C stops
cleanly: work in progress finishes, nothing new starts.

### Measured: `mt artists`

58 artists (all on Deezer), real network, whole run from reading the songs to
saving the pictures, in seconds (two runs each; lower is better). "Before" is
the first version, where each worker searched *and* downloaded for one artist
before starting the next; "after" is the current two-pool version.

| `workers` | Before | After |
| --- | --- | --- |
| 2 | 71.6 · 71.8 | 29.6 · 31.9 |
| 4 | 22.8 · 28.0 | 13.8 · 23.5 |
| 8 (default) | 13.3 · 12.2 | 11.7 · 8.5 |

- The gain is biggest with few workers (about 2.3× at 2), because a worker that
  is busy downloading no longer stops searches. Timings vary from run to run
  (the 4-worker "after" runs are 13.8 and 23.5), so read them as rough.
- All six "after" runs found all 58 artists; one "before" run at 2 workers
  missed one and reported it as not found.
- Deezer answers about 8 searches a second (the tool keeps under Deezer's limit
  of 50 per 5 seconds), so around 8 workers is where more threads stop helping:
  in an earlier test of the "before" version, 16 workers took 8.3 s and 8 took 9.1 s,
  and 1 worker took 52 s. 58 searches at 8 a second is a floor of about 7 s.
- Artists Deezer doesn't have fall back to YouTube, which is throttled harder
  (about 3 searches a second), so runs with many of those take longer.

## fix_album_art.py  (`mt covers`)

For each song without art it tries, in order:

1. An image next to the song with the same name (`song.jpg`, `.png`, `.webp`)
2. A YouTube video ID already in the file: `[dQw4w9WgXcQ]` in the filename, or
   a YouTube link in the comment/description tags
3. A YouTube search for the artist + title (or the filename)

A search result is used automatically only when its title has exactly the same
words as the song name (`min_match = 1.0`). Otherwise, once all searches are
done, you're shown the top results and can pick one, paste a link, or skip.
Filenames are normalized first, so yt-dlp's look-alike characters (`⧸`, `｜`,
`？`) and macOS's decomposed Korean/Japanese names still match.

```bash
python3 fix_album_art.py --list-missing      # just list songs without art
python3 fix_album_art.py --dry-run           # find art, write nothing
python3 fix_album_art.py                     # add art (folders from config)
python3 fix_album_art.py --auto              # never ask; skip unsure songs
python3 fix_album_art.py "song.flac" --url https://youtu.be/VIDEO_ID
python3 fix_album_art.py "song.flac" --force # redo a song that already has art
python3 fix_album_art.py --retry             # only the songs that failed last run
python3 fix_album_art.py --convert-webm      # turn .webm into .opus so they can hold art
```

At the end, every song that still has no art is listed with the reason, a
YouTube search link, and a ready-to-paste `--url` command to fix it. Supports
mp3, m4a, flac, ogg and opus. `.webm` files can't hold cover art; `--convert-webm`
copies their audio into an `.opus` file (no re-encoding, so no quality loss),
checks the copy, and moves the original to the Trash. wav and raw aac files are skipped.

## find_artist_art.py  (`mt artists`)

Finds a picture for every artist in your library. Artists come from each song's
Artist tag, or from the filename (`Artist - Title`) when a song has no tag;
`A feat. B` and `A; B` count as two artists, and a name like `A & B` that Deezer
doesn't know as one artist is looked up as `A` and `B`. Placeholders such as
"Various Artists" are ignored.

The pictures come from [Deezer](https://developers.deezer.com/api) (no account or
API key needed) and are saved as `<Artist>.jpg` in one folder: `Artist Art`
inside your music folder by default (`artist_art.output_dir`). Deezer's pictures are
1000×1000, YouTube's 800×800. Your songs are never changed, and existing pictures are
never replaced unless you pass `--force`.

A picture is used automatically only when Deezer has an artist with **exactly** the
same name (the most popular one, if several do). Deezer doesn't have everyone —
smaller, YouTube-only or non-musical channels usually aren't on it — so an artist
it doesn't know, or has no picture for, is also looked up on YouTube (their channel
picture, matched the same way: only an exact name match, the most subscribed
channel if several do; a video that merely mentions the artist is ignored, since its
thumbnail is a frame of the video and not a picture of them). YouTube limits how fast
it answers searches, so if it starts refusing, it's dropped for the rest of the run
and those artists are reported as not found — run the tool again later to retry them.
Otherwise, after all lookups are done, you're shown the
closest artists and can pick one, paste a Deezer artist link, a YouTube channel link
(`youtube.com/@name` or `/channel/...`, needs `yt-dlp`), an image link or an
image file, or skip. Artists that can't be found are listed at the end with a
ready-to-paste fix command. Run it again any time: artists that already have a
picture are skipped, so a second run only retries the ones that failed.

```bash
mt artists                                  # artist folders from the config
mt artists --list-missing                   # list artists without a picture (no downloading)
mt artists --dry-run                        # look them up, save nothing
mt artists "/path/to/Some Folder" --ask     # only this folder, pick from the closest artists when no exact match
mt artists --artist "Radiohead" --artist "Fred again.."   # look up specific artists
mt artists --artist "Some DJ" --image https://example.com/photo.jpg
mt artists --artist "Some DJ" --image ~/Pictures/dj.png   # or an image file
mt set artist_art.output_dir "Artist Art"   # where the pictures go
```

Media servers like Plex, Jellyfin and Navidrome look for `artist.jpg` inside each
artist's own folder instead. By default this tool uses one shared folder (YouTube
downloads are usually kept flat). If your library is `Artist/Album/Track`, switch with
`mt set artist_art.placement artist_folder` (or `mt artists --placement artist_folder`
for one run): each picture is saved as `Artist/artist.jpg`, an artist with no folder of
its own still goes to the shared folder, and pictures already in either place count as done.

## find_lyrics.py  (`mt lyrics`)

Translates songs whose lyrics aren't in English, and shows every line three
ways side by side: the **original** writing (kanji / hangul / Cyrillic), its
**romanization** (romaji / pinyin / Korean / Russian), and **English**. Time-synced
lyrics keep their timestamps. Songs that are already English are skipped
(`--include-english` to save those too).

Lyrics come from a `.lrc` file you already have, the lyrics in the song's tags,
or [lrclib.net](https://lrclib.net) (free, no account or key), matched on
artist, title **and length**, so a song it doesn't have is reported as not
found instead of getting another song's lyrics. Saved next to each song:

| File | What it is |
| --- | --- |
| `<song>.lrc` | For music players: each line followed by its romanization and English at the same timestamp (only when the lyrics have timing) |
| `<song>.html` | A page with all three in columns and one-click buttons to switch between *All*, *Original + English*, *Romanization + English* and *Original + Romanization*. If synced and opened next to a browser-playable file, the current line highlights as it plays and you can click any line to jump there |
| `<song>.txt` | Plain text, stacked (`lyrics.formats = ["lrc", "txt", "html"]` to write it) |

**Choosing what shows.** `--layers original,english` (or `romanization,english`, or
`original,romanization`) picks what each line shows in the `.lrc`/`.txt`; `mt set
lyrics.layers original,english` keeps it. Names like `kanji` and `romaji` work too. The
page always contains every layer, and `--relayer` re-applies a new choice to songs that are
already done, instantly, with no lookups (the lyrics are stored inside the page).

**Lyrics that are already romanized** (a lot of `.lrc` files are romaji only) get an English
translation too: the romaji stays as the text, English is added. Japanese romaji is written
back into kana first so Google/DeepL can read it; Korean, Chinese and Russian romanization can only
be translated by Claude (`ANTHROPIC_API_KEY`, add `claude` to `lyrics.translators`), otherwise
the song is reported clearly. `--no-romanized` leaves those songs alone. Translating from
romaji is less exact than from the original writing (kanji that sound alike can't be told
apart); Claude does noticeably better.

**Your own `.lrc` files** are never changed silently. When a song has one, it's used as the
lyrics, and before adding the translation into it the tool asks (`--yes` skips the question);
your original is kept next to it as `<song>.lrc.bak` (`lyrics.backup_dir` collects them all in
one folder, mirroring the library, instead of scattering them next to every song; `mt lyrics
--restore-lrc` puts them back, wherever they are, undoing the change - add `--dry-run` to
preview which files it would touch). Declined songs are offered again next time. Files the tool
writes are never replaced later except by `--force` or `--relayer`. Bilingual `.lrc` files (a
translation on a second line at each timestamp) are understood: an English or romanized second
line is used as that layer, other languages are dropped.

**Safe by design.** Songs are never changed. Everything is worked out before anything is
written, files are written atomically, and one bad song never stops the run. If no translation
service is answering, the original and romanization are saved anyway and the English is added
by the next run. Songs with nothing to be found, and English songs, are remembered
(`lyrics_checked.json`) so re-runs are fast; `--force` looks again. If your music drive is
mounted but macOS blocks it (Privacy & Security > Files & Folders > Removable Volumes), the
tool says exactly that instead of "not found".

Translation tries, in order, Google Translate (free), MyMemory (free, small daily quota),
DeepL (`$DEEPL_API_KEY`) and Claude (`$ANTHROPIC_API_KEY`), moving to the next if one is down,
out of quota or handing the text back untranslated (`lyrics.translators`). A service that
keeps failing is set aside for a few minutes instead of being hammered. If the tags or a
YouTube-style title find nothing, it retries with a cleaned title, the filename, and the
folder name as the artist.

```bash
mt lyrics --dry-run --limit 10               # trial run on 10 songs: nothing is written
mt lyrics                                    # lyrics folders from the config
mt lyrics "song.flac" --show                 # one song, and print it side by side here
mt lyrics --layers original,english          # what each line shows
mt lyrics --relayer --layers romanization,english   # switch finished songs to another pair
mt lyrics --yes                              # add translations to your own .lrc files without asking
mt lyrics "song.flac" --lyrics lyrics.txt    # use the lyrics in this file
mt lyrics --offline                          # only .lrc files and tags you already have
mt lyrics --list-missing                     # list songs without lyrics yet (no fetching)
mt lyrics --artist "IU" --title "Through the Night"   # look up a song not in your library; prints, doesn't save
mt lyrics --no-translate                     # original + romanization only
mt lyrics --no-romanized                     # leave already-romanized lyrics alone
mt lyrics --report missing.txt               # save the not-found/failed list (path + reason) as a text file
mt set lyrics.translators "google,mymemory"  # which services to try, in order
```

## fix_misidentified_tags.py  (`mt fix`)

Live sets, mixes and unreleased tracks often have good filenames but wrong tags
from Picard (e.g. a Boiler Room set tagged as a studio album track). This sets
Title/Artist/Album from the filename, keeps a track number found in it
(`03 - Title`), and removes the MusicBrainz/AcoustID IDs that point at the wrong
release. It's a dry run unless you pass `--apply`.

```bash
python3 fix_misidentified_tags.py                     # preview the whole library
python3 fix_misidentified_tags.py --only-severe       # only clear mismatches
python3 fix_misidentified_tags.py --only-severe --apply
python3 fix_misidentified_tags.py "/path/to/Music/Some Artist/Live" --apply
python3 fix_misidentified_tags.py --filter "boiler room" --apply
```

`--only-severe` skips files where the tag is only a transliteration, a typo, or
a subtitle away from the filename. Applying to the whole library without a
filter asks for confirmation.

## find_duplicates.py  (`mt dupes`)

Finds songs that are probably the same recording saved more than once —
downloaded twice into different folders, or in different formats/bitrates.
Report only: nothing is ever deleted, moved or changed. You decide what (if
anything) to remove yourself.

Songs are grouped by artist and a cleaned-up title (YouTube upload noise and
deliberate variant words like "sped up"/"slowed"/"nightcore" stripped, so two
re-downloads of the same edit still match even if worded slightly
differently), and only flagged when their lengths are also close — a song and
its own sped-up or slowed edit share a title but have a different length, so
they're correctly never flagged together.

```bash
python3 find_duplicates.py                  # duplicates.folders from config.toml
python3 find_duplicates.py "/some/folder"   # just this folder
python3 find_duplicates.py --tolerance 1.5  # how close two lengths must be (seconds)
python3 find_duplicates.py --report dupes.txt
```

## organize_music.py  (`mt organize`)

Organizes flat download folders (like YouTube downloads) into media-server-friendly
folder hierarchies ideal for **Jellyfin**, **Plex**, or **Navidrome**:

```
Music/
├── Artist Name/
│   ├── Album Name/
│   │   ├── 01 - Track Title.mp3
│   │   ├── 01 - Track Title.lrc
│   │   └── 01 - Track Title.html
│   └── folder.jpg (copied from Artist Art for Jellyfin / Plex)
```

- **Smart tag & filename reading**: Uses artist and album tags from the files,
  falling back to `Artist - Title` in the filename if tags are missing.
- **Sidecars move together**: Accompanying `.lrc`, `.html`, `.txt`, and sidecar
  images are kept with the audio file.
- **Jellyfin artist photos**: If `artist_art.output_dir` contains an image for the
  artist, it automatically copies it to `<Artist>/folder.jpg`.
- **Safe**: Resolves file name collisions cleanly, cleans up empty source directories,
  and offers a `--dry-run` preview.

```bash
mt organize --dry-run                       # preview what will move without touching disk
mt organize                                 # organize folders from config.toml
mt organize "YouTube"                       # organize a specific folder
mt organize --no-artist-art                 # don't copy artist photo to folder.jpg
```

## run_all.py  (`mt auto`)

Runs your cleanup pipeline sequentially in one automated pass:

0. **Tidy folders**: delete junk, move `.lrc.bak` files, merge duplicate albums, only if the library has some (skipped when you name folders or pass `--no-layout`)
1. *(Optional)* **Tags**: Fix misidentified tags from filenames (`--tags`)
2. **Album art**: Search YouTube and embed missing cover art (`--auto`)
3. **Artist pictures**: Download artist photos from Deezer / YouTube (`--auto`)
4. **Lyrics**: Fetch, romanize, and translate lyrics from lrclib.net
5. *(Optional)* **Organize**: Sort files and companion lyrics into `Artist/Album/` (`--organize`)

```bash
mt auto                                      # tidy folders (if needed) + art + artist pictures + lyrics; asks once
mt auto --yes                                # same, no question asked
mt auto --no-layout                          # skip the folder tidy step
mt auto --organize                           # full pass: art + artists + lyrics + folder organization
mt auto --tags --organize                    # tags + art + artists + lyrics + organize
mt auto --dry-run                            # preview all steps safely
mt auto "YouTube" --organize                 # process and organize a specific folder
```

## library_stats.py  (`mt check`)

A one-screen health check for your library: how much cover art, how many
artist pictures, how much lyrics coverage, and whether any tags look clearly
wrong — the same things `--list-missing` on each tool already tells you,
gathered into one view. Read-only and offline: nothing is looked up online
and nothing is written. Each section is scoped to that tool's own configured
folders; the tag check always covers the whole library.

```bash
python3 library_stats.py
```

## library_layout.py  (`mt tidy`)

Checks how your library is laid out on disk (`Artist/Album/Track`) using folder names only; no
tags are read. Run on its own it only **reports**:

- **duplicate album folders** under one artist: `Ado's Best` / `Ado’s Best`, `Ado - Show` / `Show`,
  `SUGAR RUSH` / `Sugar Rush - EP`, `TIMELY!!` / `Timely` (case, punctuation, quote and dash styles, a
  leading `Artist - ` and a trailing `- EP` / `- Single` are ignored when comparing);
- **artist folders spelled two ways** (`A$ap Rocky` / `A$AP Rocky`) and **collaboration-style** folders
  (`A, B`, `A & B`): listed for you to decide, never merged automatically;
- **junk**: macOS `._` files, `.DS_Store`, `.lrc.bak` backups lying next to songs, empty folders, odd
  names (`null`, `Unknown`, emoji-only), files loose in the library root.

Your artist-picture folder, `lyrics.backup_dir` and anything in `layout.ignore` are left out.

```bash
mt layout                          # report only
mt layout --report layout.txt      # the full lists as a text file
mt layout --clean                  # preview: delete junk, move .lrc.bak files, remove empty folders
mt layout --merge-albums           # preview: merge each duplicate album folder into the one with most songs
mt layout --merge-artists          # preview: merge artist folders spelled two ways
mt tidy                            # do all of it (asks first; --yes skips the question, --dry-run previews)
mt layout --undo --apply           # put back what the latest run moved
```

**Safe by design.** Without `--apply` nothing changes. Files are never overwritten: when a merge finds a
byte-identical file already in the kept folder it drops the duplicate, and when it finds a different file
of the same name it leaves both where they are. A song moves together with its `.lrc`, `.html`, `.txt`,
covers and backups, and the lyrics tool's `lyrics_checked.json` memory is updated for moved songs. Every
run that changes something saves `logs/layout_undo_<time>.json`; `--undo --apply` restores the moved and
de-duplicated files (junk files it deleted can't come back, they were junk). `--clean` moves `.lrc.bak`
files into `lyrics.backup_dir` (mirroring the library, the layout `mt lyrics --restore-lrc` already
reads), so set that first with `mt set lyrics.backup_dir "/some/folder/outside/the/library"`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

See `CHANGELOG.md` for a history of notable changes.

## Contributing

Issues and pull requests are welcome. For anything beyond a small fix, open an
issue first describing the change — these tools are built around real music
libraries, so behavior changes that affect file safety need a clear
before/after. Please add or update a test under `tests/` for any behavior
change.

## License

MIT — see [`LICENSE`](LICENSE).
