# Changelog

## Fixed: `mt organize` could replace a song, and it re-split the albums `mt tidy` had just merged

- **A song could be silently replaced.** All moves are planned before any file moves, so two different files with
  the same name headed for the same album folder (the same song downloaded twice, in different folders) both saw
  the destination as free, and the second move replaced the first while the run reported "Moved 2 songs" and no
  errors. Destinations are now claimed as they are planned (case-folded and Unicode-normalised, like the macOS and
  Windows file systems see them), so the second copy becomes `Song (2).mp3`. As a last safety net a move never
  replaces a file that is already there: the song stays where it was and the run says so.
- **Organize no longer undoes `mt tidy`.** It matched an existing album folder only by case and Unicode form, so
  `TIMELY!!` / `Timely`, `SUGAR RUSH` / `Sugar Rush - EP`, `WE DON’T TRUST YOU` with a curly or straight
  apostrophe, and `The Singles – …` with a dash or a hyphen counted as different albums and every odd-tagged song
  started a new folder: after a full `mt auto` the 11 duplicate album folders tidy had merged were back. Organize
  now uses the same "same album" rule as tidy (the fuller folder wins), for folders on disk and for albums planned
  in the same run.
- A lyrics or cover file whose name is already taken in the new folder stays behind with a warning instead of
  replacing the other one, and a song that moved still counts as moved when only such a file couldn't. A copy that
  stops half way (disk full, drive unplugged) is removed instead of being left looking like the song.
- A folder name made from a huge tag is cut to 200 bytes instead of failing with "File name too long".

## Added: remove duplicate songs, a goal-first menu, and an `mt auto` that asks what you want

- **`mt dupes --delete-strays`** keeps the copy of a song that sits in an `Artist/Album/` folder and removes the
  loose copies (in the music folder or directly in an artist folder), with the lyrics and cover files named
  after them. It previews unless you add `--apply`, then asks first (`--yes` skips the question). A group with no
  album copy, or with every copy in an album, is left alone; a copy outside the music folder is never touched; a
  loose lossless file is held back when every album copy is lossy. On a Mac the copies go to the Trash; elsewhere
  they are deleted for good (the question says which). What was removed is saved to
  `logs/duplicates_removed_<time>.txt`. Plain `mt dupes` is still report-only.
- **`mt auto` looks, then asks.** Typed alone in a terminal it scans the library (read-only, no network), shows what
  it found next to each step ("212 of 1,904 songs have no art"), ticks the steps with work to do and lets you
  change the list. Below the steps are **Preview only (dry run)** and **Apply the changes without asking
  again**. Removing songs, fixing tags and sorting folders are never ticked for you. With any option on the
  command line, or without a terminal, it runs exactly what you asked for with no questions, as before.
  `mt auto --delete-strays` adds the duplicate-song step to the pipeline (after the tidy, before art and lyrics).
- **A menu organised by what you want to do**: Do it all for me, Tidy my files, Add what's missing, Check my
  library, then Folders, Settings, Logs and Help. The old 15 tool-named rows are now one level down under those
  goals. The duplicates screen has Remove / Preview / Apply boxes, and hints line up in a column on every screen.
- `library_stats.py` now has `art_numbers`, `artist_numbers` and `lyrics_numbers` that return the counts the
  health check prints (its output is unchanged).

## Added: `mt clean`, and dashes in front of a command are ignored

- `mt clean` and `mt cleanup` now mean `mt tidy` (same confirmation prompt, same undo file). Before, `mt -clean`
  stopped with "Unknown command" and no suggestion, because `--clean` is only an option of `mt layout`.
- Dashes typed in front of a real command are ignored (`mt -clean`, `mt --tidy`, `mt --undo`), like the flag
  slips that were already forgiven. A word that isn't a command (`mt -banana`) is still refused.

## Added: Windows support, and fixes for other setups

- **Windows 10/11 now works.** `setup.bat` creates `.venv` and installs the packages, and `mt.cmd` is `mt` for
  Command Prompt and PowerShell (put the folder on `PATH`). `./setup.sh` also runs in Git Bash, and on macOS it now
  picks a Python 3.11+ itself instead of failing on the older built-in `python3`.
- **The launcher**: finds the venv's python in `.venv\Scripts` as well as `.venv/bin`, and waits for it on Windows
  (where `os.execv` doesn't replace the process, so the shell got its prompt back while the menu was running).
- **The menu**: arrow keys now work on Windows (it used numbered choices there before); running a tool from the menu no
  longer crashes on Windows (`preexec_fn`); a dragged-in `C:\Users\me\Music` keeps its backslashes (it was run
  through a POSIX shell parser); "open the settings file / logs folder" uses Notepad / Explorer on Windows and says where
  the file is, instead of crashing, on a machine without `xdg-open`; the log viewer works without `less`.
- **yt-dlp is found where setup put it.** It was only looked up on `PATH`, but setup installs it into `.venv`, whose
  `bin` / `Scripts` folder isn't on `PATH`, so YouTube searches said "yt-dlp is not installed" until you also ran
  `brew install yt-dlp`. It now falls back to running the installed package, and the install hints name the right
  package manager for the system (`brew`, `winget`, `pip`).
- **Text is UTF-8 everywhere.** The output of yt-dlp and ffmpeg, `failed_album_art.txt` and the test files were read
  and written in the system's legacy code page on Windows (and under non-UTF-8 locales), which garbled or crashed on
  non-Latin titles. Printing a title the terminal can't show now prints `?` instead of stopping the run.
- **Terminal**: colours and cursor control are switched on in Windows consoles (and skipped under `TERM=dumb`).
- **Names**: on Windows, folder and picture names such as `CON`, `NUL` and `COM1` get an underscore. The config is also
  read from `%APPDATA%\music-tools\config.toml` on Windows. Printed hints quote paths for the shell in use.
- **Tests and CI**: the suite now runs on Ubuntu, macOS and Windows (Python 3.11 to 3.13), and a second job runs the
  real setup script and launcher on each. Tests no longer rely on POSIX-only paths, permissions or the system's
  default text encoding. New `tests/test_platform.py` covers each system's branch.
- README: Windows setup, the `mt` shortcut, config paths, and a "Windows, macOS and Linux" section.

## Fixed: `mt organize` and placeholder tags

- Tags that literally say `null`, `none`, `n/a`, `Unknown`, `Unknown Artist` or `Unknown Album` are now treated as missing, so
  songs are filed by the filename's `Artist - Title` and the looked-up or fallback album instead of getting `null` /
  `Unknown` folders.

## Fixed: genre words as artists, and English songs reported as lyrics failures

- `mt artists` no longer looks up genre words from a garbled artist tag (`KPOP, House, ZARA` found an artist called "House").
- `mt lyrics`: when every translation service hands the text back unchanged (or says source and target are the same
  language) the song is English or close to it. It is now skipped and remembered as such instead of failing on every run,
  and those answers no longer count as a service outage (so a service isn't set aside for them).

## Added: simpler commands

- Easier names: `mt auto`, `mt covers`, `mt pics`, `mt check`, `mt fix`, `mt dupes` (the old names still work), plus two new
  verbs: `mt tidy` (merge duplicate folders and delete junk, preview with `--dry-run`) and `mt undo`.
- Small flag slips are forgiven (`--dryrun`, `--dry_run`, `-n`, `-y`); a mistyped command suggests the closest one and
  shows a short everyday cheat sheet. `mt help` starts with the same cheat sheet.

## Changed: `mt all` is now a smart auto mode

- Runs in the cheapest order: tidy folders (junk, `.lrc.bak`, duplicate albums) when the library has some, then tags
  (`--tags`), album art, artist pictures, lyrics, and organize (`--organize`) last. The tidy step is skipped when
  there's nothing to do or when you name folders.
- Shows the plan and asks once before changing anything; `--yes` skips the question, `--dry-run` previews. `--no-layout`
  skips the tidy step. The tidy step saves an undo file (`mt layout --undo --apply`).

## Added: `mt layout` (folder layout check and tidy) and artist pictures in each artist's folder

- **`library_layout.py` (`mt layout`)** checks the `Artist/Album/Track` layout from folder names alone and, by
  default, only reports: duplicate album folders (case, punctuation, quote and dash styles, a leading
  `Artist - ` and a trailing `- EP` / `- Single` ignored), artist folders spelled two ways, collaboration-style
  artist folders, macOS `._` junk, `.DS_Store`, `.lrc.bak` files beside songs, empty folders, odd names
  (`null`, `Unknown`, emoji-only) and files loose in the library root. Your artist-picture folder and
  `lyrics.backup_dir` are left out (`layout.ignore` adds more).
- **`--clean`** deletes the junk, moves `.lrc.bak` files into `lyrics.backup_dir` (the mirrored layout
  `--restore-lrc` already reads) and removes empty folders. **`--merge-albums`** merges each duplicate album folder
  into the one with the most songs: songs travel with their lyrics and covers, nothing is overwritten,
  byte-identical copies are dropped, different files of the same name are left alone, and `lyrics_checked.json` is
  updated. Both only preview until `--apply`; every change is saved to `logs/layout_undo_<time>.json`, and
  `--undo --apply` reverses it. `mt stats` gets a one-line layout summary and the menu a "Check folder layout" entry.
- **`artist_art.placement`** (`shared` by default, or `artist_folder`; `mt artists --placement ...`) saves
  `Artist/artist.jpg`, the name Plex, Jellyfin and Navidrome look for, when the artist has a folder of their own;
  otherwise the picture goes to the shared folder as before. Pictures in either place count as done.
- Tests: 191 (23 new).

## Changed: faster, with less duplicated code (no change in what the tools do)

- **`mt stats` walks the library once** instead of once per section (cover art, artist pictures,
  lyrics, tags), and the tag check stops after reading the title for files that aren't a severe mismatch.
- **`find_lyrics.py` / `find_artist_art.py`**: splitting "already done" from "to do" was quadratic (every
  `in` check compared whole dataclasses); it is now one pass. 3.7 s -> under 1 ms for 4,650 songs.
- **Connections are reused.** lrclib, Deezer, Google/MyMemory/DeepL/Claude and thumbnail requests keep one
  connection per worker thread (`common.fetch_url`) instead of a new TLS handshake per request. Proxy
  settings are still honoured (those requests go through urllib as before).
- **lrclib answers are remembered for 10 minutes**, so spelling variants of one song that boil down to the
  same query aren't asked twice.
- **A song's tags are read once** in the lyrics tool (embedded lyrics reuse the already-parsed file).
- **`pypinyin` loads on first use**: startup of `mt stats` / `mt duplicates` / the tag fixer drops from ~280 ms to ~120 ms.
- Shared `plural` and `atomic_write` helpers replace three and three copies; removed unused code
  (`magenta`, `sky`, `BACKEND_NAMES`, the unreachable `duration_of` fallback, the duplicate `_norm`).
- Tests: 168 (17 new, covering connection reuse against a local server, the lrclib cache, `atomic_write`
  and the single-walk library). Without `mutagen`/Pillow installed the test modules now skip with a
  message instead of failing at import. CI caches pip.

## Added: a duplicate-song finder and a one-screen library stats view

Two new report-only tools, wired into `mt` and the interactive menu the same way as the
existing ones:

- **`find_duplicates.py` (`mt duplicates`)**: groups songs by artist and a cleaned-up title
  (YouTube upload noise and deliberate variant words like "sped up"/"slowed"/"nightcore"
  stripped, so re-downloads of the same edit match even when worded slightly differently),
  then only flags a group when the lengths are also close — a song and its own sped-up or
  slowed edit share a title but not a length, so they're correctly never flagged together.
  Nothing is ever deleted, moved or changed; it only reports. New `[duplicates]` config
  section (`folders`, `tolerance_seconds`).
- **`library_stats.py` (`mt stats`)**: a one-screen, read-only, offline summary of cover art,
  artist picture, lyrics and tag-mismatch coverage, reusing each tool's own configured
  folders and existing logic rather than re-implementing any of it.

## Fixed: a "la la la" intro could get a whole English song mistranslated-and-rejected

Found from a real run: several genuinely-English songs failed with confusing errors
("MyMemory couldn't translate (PLEASE SELECT TWO DISTINCT LANGUAGES)", "google returned the text
untranslated") and, worse, these failures repeatedly benched *both* Google and MyMemory for the
rest of the run, breaking other unrelated songs too.

- **Root cause**: when the offline language guess comes back unsure ("und"), a translation service
  is asked to identify the language from a probe of "the first 6 lines with letters in them." Many
  songs open with a repeated one-syllable ad-lib ("la la la", "oh-oh-oh") that carries almost no
  language signal — confirmed live that Google's auto-detect reads three repeated "la"s as Spanish.
  That wrong guess sends a plainly-English song through the translator, which correctly refuses to
  "translate" English into English — and that refusal was counted as a service failure, eventually
  benching it.
- **Fix**: the probe now prefers lines with more distinct words, so a repeated ad-lib is only used
  once nothing more substantial is available. Confirmed live against the exact song that surfaced
  this (a Cyberpunk 2077 radio track): the old probe detected "es", the new one correctly detects "en".

## Fixed: one bad lrclib request could abort a whole song's lookup

Found from a real run against the library: about a third of that run's failures were a cryptic
"lrclib.net answered HTTP 400" instead of a real reason.

- **Root cause**: lrclib's exact-match endpoint hard-rejects any `duration` outside 1-3600 seconds
  with a validation error — and a file over an hour long (a DJ mix, a compilation, a "best tracks"
  file someone scanned as if it were one song) has exactly that duration. That error wasn't being
  caught, so it aborted the *entire* lookup for that song — not just the exact-match shortcut, but
  the fuzzy search fallback too, and every other spelling the tool would otherwise have tried.
- **Fix**: a duration outside lrclib's accepted range is no longer sent to the exact-match
  endpoint (there's no point asking for something guaranteed to be refused), and a failure from
  that endpoint for any other reason no longer aborts the lookup — it now falls through to search,
  same as if nothing exact had matched. Confirmed live: the affected real songs ("J-Shoegaze
  playlist.flac", "Persona 3 Reload Best Tracks.flac", "aerospace engineering.flac", "escape
  everything..flac" — all genuinely hour+ long, not real songs) now correctly report "no lyrics
  found on lrclib.net" instead of the opaque HTTP error.

## Russian romanization

- **Russian lyrics now get a romanization column** too, alongside Japanese, Korean and Chinese:
  Cyrillic text is transliterated letter by letter with a small built-in table (no package needed,
  unlike Japanese/Chinese/Korean's optional dependencies) — e.g. "Привет" -> "Privet". Checked
  against the library's own real Cyrillic songs before building anything on top of it.
- Language detection (`guess_language`, `line_language`), the "needs Claude" message for
  already-romanized text with no original script, DeepL's source-language hint, and the
  prefer-the-original-script-over-someone's-romanization lrclib matching rule all now cover Russian
  the same way they already covered Japanese/Korean/Chinese.
- Internal: the four places that used to separately hardcode `("ja", "ko", "zh")` now share one
  `lyrics_romanize.SUPPORTED` constant, so adding a language only means registering its romanizer
  function once instead of remembering to update every call site.
- Chinese and Spanish/French/Portuguese/etc. needed no changes — Chinese was already fully
  supported, and Latin-script languages don't need a romanization column; they were already being
  detected and translated correctly.

## Bug fixes, a faster lyrics tool, and better matching

Found by probing the real library's lrclib lookups live (read-only) and comparing before/after.

- **Fixed a real matching bug:** a title that's entirely inside one set of brackets (some Vocaloid
  songs are tagged like `"(Dadadadadaru / Amala)"`) was being stripped down to nothing by the
  "remove `(feat. X)` / `[Official Video]`" cleanup, so that spelling was never searched. The bracket
  is now only removed when something is left outside it; confirmed live that lrclib does have at
  least one such song, at a matching duration, that this was silently skipping.
- **Better matching:** when the Artist tag is really a channel/curator name and the real artist only
  shows up in the filename (e.g. `Valiant / "LESST - Wicked (feat. Elvya)"`), the filename's artist is
  now also tried together with the *cleaned* title, not just paired with the raw tag artist.
- **Investigated and deliberately did *not* add:** passing the file's Album tag into lrclib's exact
  lookup. Tested live against 20 real songs: it never turned a miss into a hit, and it broke one
  match that already worked (a compilation whose Album tag didn't match lrclib's own spelling) — a
  net loss for a "right lyrics or none" tool, so it's left out.
- **~2x fewer file reads per song**: `find_lyrics.py` was opening every song twice (once for
  tags, once more just for its length). One read now returns both; measured ~16x faster for that
  step alone on a real sample (dwarfed by network time in a normal run, but matters most for
  `--offline` and slow drives).
- **New `--report FILE`**: saves the not-found/failed list (path + reason, one per line) as plain
  text, so it can be reviewed outside the terminal.
- **Menu fix**: the "Folders" screen could only change album-art folders — artist-picture and lyrics
  folders had no menu entry at all (only `mt set artist_art.folders "..."` reached them). The screen
  now lists and edits all three.
- Claude translation requests raised from `max_tokens: 8192` to `16384`, so an unusually long batch
  of lines can't get silently truncated into an unparseable reply.

## Lyrics: layers, romanized lyrics, and a lot more safety

Found while running it against the real library (about 4,000 songs and 4,650 existing `.lrc` files).

- **Translates romanized lyrics.** Lyrics that are already in romaji (like most of the existing
  `.lrc` files) get English added, keeping the romaji. Japanese romaji is converted back to kana so
  Google/DeepL can read it; Korean/Chinese romanization needs Claude and says so. `--no-romanized`
  / `lyrics.translate_romanized` turns it off. Before, these songs just failed.
- **Choose the layers**: `--layers original,english` / `romanization,english` / `original,romanization`
  (`lyrics.layers`), aliases like `kanji` and `romaji`. The `.html` page has all three columns and
  one-click switch buttons; `--relayer` re-applies a choice to finished songs with no lookups.
- **Your own `.lrc` files** are now used as the lyrics, and the translation can be added into them
  (asks first; `--yes`; original kept as `.lrc.bak`, or all collected in one folder with
  `lyrics.backup_dir`). Never touched silently. `--restore-lrc` puts the backups back, undoing the
  change. Also reads lyrics from the song's tags, `--lyrics FILE`, `--offline`, and `.lrc` files in
  Shift-JIS / EUC-KR / GBK.
- **Bilingual `.lrc` files** (translation on a second line at each timestamp) are understood: an
  English/romanized second line becomes that layer, other languages (e.g. Vietnamese) are dropped.
- **Only foreign songs** are translated and saved by default (`lyrics.only_foreign`,
  `--include-english`); the language is worked out offline from the writing.
- **Right lyrics or none.** lrclib matches now need the same title and artist (or, for artists spelled
  another way like kanji, the same length); different-length recordings are ignored; synced beats plain;
  the original writing is preferred over somebody's romanization; credit lines ("作词 : …") are removed.
- **Fallbacks:** translation services down → original + romanization saved now, English added next run;
  service order with sets-aside for ones that keep failing; retries with cleaned titles, the filename and
  the folder name; romanization engines fall back (cutlet → pykakasi, korean-romanizer → built-in).
- **Fixed:** songs with dots in their names ("Fred again..", "Mr. Brightside") were never recognised as
  done and were redone every run; a drive macOS blocks was reported as "unplugged" (now says what to
  allow); a missing romanization package was silently ignored (now shown at the start); the tags of every
  song were read one by one before anything showed (now parallel, with progress); Japanese romaji now
  splits words properly ("wa" for the particle は) with `cutlet`; Korean follows pronunciation rules;
  `lyrics.claude_model` named a model that doesn't exist and would have failed every request.
- **Nicer terminal UI:** a LYRICS banner, a cat that listens while it works (and reacts to how it went),
  `--show` prints the result side by side, `--limit N` for trial runs.
- Menu: layer switches, "translate romanized lyrics", "switch layers of finished songs".
- `music-tools` uses `.venv` automatically when `./setup.sh` has been run.
- New requirements: `cutlet`, `unidic-lite`, `pypinyin`, `korean-romanizer` (run `./setup.sh` again).

## Lyrics

- New `find_lyrics.py`, available as `mt lyrics`: finds lyrics for every song
  from [lrclib.net](https://lrclib.net) (no account or key) and saves the
  original text, a romanization, and an English translation next to the song.
  - `<song>.lrc` (only when lrclib has timing), `<song>.html` (plays along
    with the song and highlights the current line if synced and opened next
    to a browser-playable file), `<song>.txt` — which to write is
    `lyrics.formats` (default `lrc`, `html`)
  - Japanese and Korean are romanized (romaji / revised romanization),
    Chinese as pinyin; Korean needs no extra package, Japanese/Chinese use
    `pykakasi` / `pypinyin` when installed and are skipped otherwise
  - English translation tries Google Translate, MyMemory, DeepL
    (`$DEEPL_API_KEY`) and Claude (`$ANTHROPIC_API_KEY`) in order
    (`lyrics.translators`), moving on if one is down or out of quota
  - `--artist NAME --title NAME` looks up a song that isn't in your library
    and prints it instead of saving; `--no-translate`, `--formats`,
    `--list-missing`, `--dry-run`, `--force`
  - Never replaces existing lyrics files without `--force`; re-running only
    retries songs that don't have them yet
- Menu: new "Find lyrics" entry; settings for the Claude model and
  replace-existing.

## Artist pictures

- New `find_artist_art.py`, available as `mt artists`: finds a picture for every
  artist in the library from Deezer (no account or key) and saves it as
  `<Artist>.jpg` in one folder (`artist_art.output_dir`, default `Artist Art`
  inside the music folder). Songs are never changed.
  - Artists are read from the Artist tag, or the `Artist - Title` filename for
    untagged songs; `feat.` / `;` split into several artists, and `A & B` falls
    back to looking up `A` and `B` separately
  - A name is used automatically only on an exact match (the most popular
    artist if several share it); otherwise you pick from the closest artists,
    paste a Deezer link / image link / image file, or skip
  - Never replaces an existing picture without `--force`; re-running only
    retries artists that don't have one yet
  - `--list-missing`, `--dry-run`, `--auto`, `--artist NAME`, `--image`
  - Stays under Deezer's rate limit (and waits and retries if it is hit)
- An artist Deezer doesn't know, or has no picture for, is now also looked up
  on YouTube (their channel picture) before falling back to asking you —
  Deezer alone was missing most smaller and YouTube-only artists.
- Overlapped searches with downloads (two thread pools instead of one): a
  slow download no longer blocks the next artist's search, and an artist
  split into parts (`A & B`) downloads both pictures at once.
- Much faster lookups for artists Deezer doesn't have: only the first page of
  YouTube results is read (it used to page through every result YouTube had,
  tens of seconds per artist), and the parts of `A & B` are looked up at the
  same time instead of one after another. Deezer's fallback queries are only
  sent when the plain name found nobody, so the usual artist costs one request.
- YouTube searches are now spread out, and dropped for the rest of the run once
  YouTube keeps refusing them (it rate-limits bursts, and every refusal cost
  seconds of retries). Those artists are reported as not found, with the reason,
  and a later run retries them.
- Fixed: a YouTube *video* that merely mentioned the artist could be used as a
  match, which saved a frame of the video as the artist's picture. Only channels
  count now.
- Menu: new "Find artist pictures" entry; settings for the pictures folder,
  number of artists shown and replace-existing.
- Nicer look: coloured headings, aligned tables and a summary line for the new
  tool, and a cleaner progress bar for all tools. The colour helpers now live in
  `common.py` and are shared with the menu.
- Fixed: editing a text setting (such as the pictures folder) in the menu's
  Settings screen was rejected as "not a number".

## Interactive menu and run logs

- Running `music-tools` with no arguments opens a full-screen menu: arrow keys
  to move, Enter to choose, Space to switch options on/off, `q` to go back.
  - Find songs without art, add art, retry failed songs, fix tags
  - Options are shown as on/off switches before each run (convert .webm,
    ask when unsure, crop, preview only, replace existing art)
  - Switch folders by typing a path (Tab completes) or dragging a folder in
    from Finder; missing folders (e.g. an unplugged drive) are marked
  - Every setting is listed with a plain-English description
  - Browse the logs of previous runs
- Each run's output is saved to `logs/<date>_<tool>.log` (last 200 kept,
  `save_logs = false` turns it off). Logs are git-ignored.
- Fixed: Ctrl-C while a tool was started from `music-tools` killed it
  immediately instead of letting it finish the file being written.

## `music-tools` command, .webm conversion, smarter search

- New `music-tools` command for switching folders and settings without
  editing the config: `where`, `dir`, `folders use/add/remove`, `settings`,
  `set`, `reset`, `edit`, plus `art`, `tags`, `missing`, `retry`, and `help`
  (all commands grouped by tool).
- `--convert-webm`: copies a .webm's audio into an .opus file without
  re-encoding, checks the copy, adds cover art, and moves the original to
  the Trash.
- Search now tries the song's tags, then the filename, then simplified
  versions of both, so songs with wrong tags still find their video.
- A one-word search (e.g. "Guts") is never accepted automatically.
- Fixed: `--config` / `$MUSIC_TOOLS_CONFIG` pointing at a file that didn't
  exist yet could write settings into a different config.

## Album art and tag fixing tools

- `fix_album_art.py`: finds songs without embedded cover art and adds the
  matching YouTube thumbnail (from a sidecar image, a video ID in the file,
  or a YouTube search). Only exact title matches are used automatically;
  otherwise you pick from the top results, paste a link, or skip.
  - Undoes yt-dlp's look-alike filename characters (⧸ ｜ ？) and macOS's
    decomposed Korean/Japanese filenames so searches match
  - Lists every song still without art with the reason, a search link and a
    ready-to-run fix command; `--retry` re-runs only those
  - `--url`, `--force`, `--dry-run`, `--list-missing`, `--auto`, `--no-crop`
- `fix_misidentified_tags.py`: rebuilds Title/Artist/Album from the filename
  for songs MusicBrainz Picard matched to the wrong album, keeps track numbers
  found in the filename, and removes MusicBrainz/AcoustID IDs. Preview by
  default; `--apply` writes.
- Both: settings in `config.toml`, parallel processing with progress bars,
  safe Ctrl-C (files being written are finished first), clear errors for bad
  config values, missing tools, offline use, and unreadable files.
- `setup.sh` installs into a private `.venv`; tests run on GitHub Actions.
