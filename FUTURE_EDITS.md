# Future edits: lyrics tool (`mt lyrics`)

Planned or worth-considering changes, most useful first. Nothing here is done yet unless marked;
see `PROGRESS_LOG.md` for what is.
Last updated: 2026-10-01 (session 3, end).

## Do next

1. ~~Commit~~ — **done**: session 2's work was committed as `90b83ad`. Session 3's bug fixes and features
   are a separate commit on top of that. `./setup.sh` has been run and all 128 tests pass.
2. ~~Staged real run, always read-only first~~ / ~~drive it for real~~ — **done, session 2, the hard way**:
   dry-runs first (`--limit 20`, one artist folder), then a hand-driven trip through the interactive menu
   that turned into a real, bare, whole-library run (832 songs saved, 1,029 not found/busy, 0 write errors).
   You chose to keep it. Nothing left to stage here.
3. ~~Decide the `.bak` policy~~ — **done (session 2)**: added `lyrics.backup_dir` (collect backups in one
   mirrored folder instead of scattering them) and `--restore-lrc` (put them back, undoing the change;
   `--dry-run` previews it). Default behaviour (backup next to the file) is what tonight's real run used —
   1,410 `.bak` files are sitting next to their songs in the library right now. Not yet done: a full per-run
   undo manifest — see "Should do" below, this was the simpler of the two restore options from session 1's list.
4. **Try a Claude key** for romanised Japanese/Korean (`ANTHROPIC_API_KEY`, `mt set lyrics.translators
   claude,google`) on a few songs and compare with the kana route. Still not done — no key was available in
   any of the three sessions' environments. Worth trying now: session 2 found and fixed a bug where
   `lyrics.claude_model` defaulted to `"claude-sonnet-5-5"`, which isn't a real model id, so every earlier
   attempt (if any) would have failed regardless of the key. It's now `"claude-sonnet-5"`. Session 3 also
   raised the Claude backend's `max_tokens` from 8192 to 16384 (untested live, same reason) so an unusually
   long batch of lines can't get silently truncated into an unparseable reply.
5. ~~Look at the 1,029 songs that didn't get lyrics tonight~~ — **partly done (session 3)**: sampled ~300 of
   them at random and re-queried lrclib live, read-only, to check whether they're genuine gaps or
   parsing misses. Verdict: overwhelmingly genuine (instrumental OST tracks - several literally marked
   `instrumental: true` on lrclib -, niche/SoundCloud artists, freestyles, commercials not on any lyrics
   database). Found and fixed exactly one real parsing bug this way (see `PROGRESS_LOG.md` session 3: the
   whole-title-in-brackets case in `_tidy()`). Not a full audit of all 912-ish remaining entries, just a
   sample large enough to trust the overall picture — a bigger sample would need pacing (see the 503 note
   in `PROGRESS_LOG.md`) to avoid getting rate-limited by lrclib's free API again.

## Should do

- ~~Drive the lyrics menu by hand once~~ — **done (session 2)**, more thoroughly than planned: exercised
  `--list-missing --dry-run`, then an actual full real run through the "Preview only" checkbox defaulting to
  off. Confirmed: nothing stops a real run if you don't explicitly check Preview, and the only other guard is
  the one y/N prompt for files you already own. Worth remembering next time you're in that menu.
- **The `--restore-lrc` and `--relayer` checkboxes specifically** are still only `ast.parse`-checked, not
  actually driven through the terminal UI (only the plain fetch+translate path was exercised tonight).
- **Live-test DeepL** (`DEEPL_API_KEY`), and add a test that records a real response shape for each backend.
- **Menu**: edit `lyrics.layers` and `lyrics.translators` in Settings (the generic settings screen can't edit
  lists). Related gap **fixed (session 3)**: the folder lists specifically (`artist_art.folders`,
  `lyrics.folders`) had no menu path at all before — the "Folders" screen only ever touched
  `album_art.folders`. It now edits all three (add/remove/replace), via its own screen rather than teaching
  the generic Settings list-editor, which is still the open part of this item.
- **A full `--restore` / `--undo` for a run**, beyond the per-file `--restore-lrc` built this session: write a
  small manifest of everything a run changed (`lyrics_changes.json`, covering new `.lrc`/`.txt`/`.html` files
  too, not just `.lrc` translations) so a whole run can be rolled back in one step.
- **Progress for large runs**: an ETA and a periodic "N of M songs" line when not on a terminal; a
  `--resume`-style summary of what the last run left unfinished (partial songs, declined `.lrc` updates).
- **Report grouping**: collapse identical failure reasons by default even for small runs (now runs of 6 or
  fewer failures always list each one). ~~add a `--report FILE`~~ — **done (session 3)**: saves the
  not-found/failed list (path + reason) as plain text.
- **CI**: confirm `pip install -r requirements.txt` builds `mojimoji` (needed by `cutlet`) on the GitHub
  runners for Python 3.11 to 3.13. **Checked without pushing (session 3)**: `pip download --platform
  manylinux2014_x86_64 --python-version 311 --only-binary=:all:` finds prebuilt wheels for both `mojimoji`
  and `fugashi` (no compiler needed), and `unidic-lite` is a pure-Python sdist (no C extension to build) —
  so this is very likely fine, but the actual CI run for this commit hasn't happened yet (the "Harden the
  lyrics tool" commit that added these requirements was still unpushed as of session 3's start; see
  `git log origin/main` vs `git log`).

## Could do

- **Korean / Chinese romanised lyrics without Claude**: a reverse romanizer (romanization -> hangul) is
  ambiguous because the spelling follows pronunciation. Could try a small dictionary + frequency approach,
  or skip.
- **Better Japanese kana route**: keep word boundaries as spaces, or send romaji and kana together, and
  measure whether Google's output improves on real lines ("Hanataba", "chi" = blood).
- **Second lyrics source** when lrclib has nothing (plain text only, so higher wrong-match risk; would need
  the same title/artist/length guard, and a clear "unsynced, from X" label).
- **Word-level (karaoke) timing** if a record has enhanced LRC (`<mm:ss.xx>` tags): highlight words in the page.
- **Embed into tags**: optionally write the combined lyrics into the song's lyrics tag (USLT / `©lyr` /
  `LYRICS`) for players that ignore `.lrc`. Changes the song file, so opt-in only.
- **Per-artist overrides**: a `lyrics.overrides` table (folder -> layers / skip) for artists you never want
  translated.
- **Configurable cache expiry** for "not found" (now fixed at 30 days), and a `--recheck-missing` flag.
- **Terminal reader**: a full-screen, scrolling side-by-side view that follows the song (`afplay` clock) with
  the cat dancing next to it; the HTML page already does the synced version in a browser.
- **Share the cat UI**: the banner and animated progress bar exist only in the lyrics tool; the other tools
  could use `lyrics_ui.py` (rename it to something general first).
- **More scripts**: Cyrillic, Arabic, Thai, Hindi romanization (`unidecode`-style) so those songs get the
  middle column.

## Known limitations to keep in mind

- Translation from romaji cannot recover kanji; homophones can come out wrong. Claude is better (once
  actually tried with a key — see "Do next" above).
- LRCLIB sometimes only has a romanised or bilingual record; the tool prefers the original-script one and
  folds bilingual lines, but a bad record can still slip through. `--lyrics FILE` is the escape hatch.
- Google's free endpoint is unofficial; MyMemory's free quota is a few songs a day. With both unavailable,
  songs with a romanization are saved without English and finished later; songs without one wait entirely.
- Each song's `.html` embeds its data so layers can be switched later; deleting the `.html` makes `--relayer`
  skip it. Same idea applies to `--restore-lrc`: it looks for the `.lrc.bak` wherever `backup_dir` would have
  put it, so moving/deleting that folder (or changing `backup_dir` between the translate run and the restore)
  means it won't find what it's looking for.
- `lyrics_checked.json` remembers "English" and "not found" answers per path; moving the library invalidates
  it (harmless, just slower). `--force` ignores it.
- Session 1 noted the Bash tool used in these sessions couldn't read `/Volumes/Transcend` directly (macOS
  privacy) and needed the Terminal panel instead. Session 2's Bash tool could list and dry-run against it
  directly — permissions must have changed on your end — but this hasn't been deliberately re-tested for
  every operation, so don't be surprised if it's inconsistent.

## Ideas considered and left out (and why)

- **Passing the song's Album tag into lrclib's exact "get" lookup** (session 3): `fetch()` already had an
  `album` parameter, wired to lrclib's API, that nothing ever populated from real tags. Tested live against
  20 real songs that already have lyrics: it never turned a miss into a hit, and it turned one working match
  into a miss (a compilation whose Album tag isn't spelled the same way on lrclib). For a library this messy
  (the whole reason `fix_misidentified_tags.py` exists), trusting the Album tag as a hard filter is a net
  loss — left out. Don't re-add this without similar live evidence that it helps more than it hurts.
- A second free translator tier beyond Google + MyMemory: none is reliable without a key.
- Translating into languages other than English: not asked for; the English-only detection heuristics would
  need reworking.
- Overwriting the original `.lrc` with no backup: too risky for a 4,650-file library.
- Making the cat animate in `--no-progress` / piped runs: it only moves on a real terminal by design
  (`MUSIC_TOOLS_NO_ANIMATION=1` keeps it still).
- A backup-location scheme fancier than "next to the file" or "one mirrored folder" (e.g. per-run
  timestamped folders): not asked for, and would make `--restore-lrc` need to pick which run to undo instead
  of just "the latest backup of this file", which is the case that matters.
