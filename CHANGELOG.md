# Changelog

## Unreleased

Records without silence between tracks no longer teach the provider the next track under the
wrong name (as long as AudD reports the track length, which it usually does).

### New

- **Gapless guard:** the provider now stores the track length that AudD reports, and uses it with
  AudD's position in the song (or the position of a local match) to work out when the track should
  end. Learning stops 5 seconds before that moment; if no silence follows within 5 seconds after
  it, the provider starts listening for the next track anyway. New log lines: `Expected end of …`
  and `Track boundary (no gap): …`.
- **Track length and position in Music Assistant:** when the provider knows where the song
  started and how long it is, Music Assistant now shows the position in the song and the track
  length (a progress bar), instead of the time since the source started. The length follows the
  speed correction, so the bar ends when the track ends on your turntable.
- **Lengths for tracks you already have:** a track recognized locally that has no stored length
  yet gets one extra AudD request (once) to learn it, so the progress bar and the end guard work
  for your whole collection over time. If AudD names a different track, the local recognition
  stands and nothing is stored.
- **Merge duplicates** (for developers/advanced users): `scripts/merge-tracks.sh` merges two
  tracks that are one recording when AudD named it in two ways (e.g. *Barry White* and *The Love
  Unlimited Orchestra*); the other name is remembered, so the duplicate does not come back.
- **Fewer duplicate tracks:** edition suffixes such as *(Album Version)*, *(Single Version)* or
  *(2016 Remaster)* no longer make a separate track, so "You See The Trouble With Me" and "You See
  The Trouble With Me (Album Version)" are one track. Existing duplicates are merged automatically
  when the provider starts (the older track keeps its name). *(Live)*, *(Remix)* or *(Radio Edit)*
  still count as different recordings.
- **Live and gapless records:** a long stretch of unidentified music no longer stops asking AudD
  after 8 tries (now 20, still one every 90 seconds after the first three), so on a live record
  the next tracks get their chance even when one track in between is unknown to AudD. New
  advanced setting *Ask AudD more often (live and gapless records)*: every 30 seconds instead of
  90, at the cost of more requests.
- **Self-correcting track ends on live and gapless records:** AudD's track length is often that
  of another edit (on a live LP 10–30 seconds too long). When the next track is recognized, the
  provider now knows where the previous one really ended: it removes what it learned past that
  point and measures the real length; a length measured twice on your record replaces AudD's.
  A real silence after a track also measures its length (a stream dropout does not). While a
  length is not yet measured, learning stops 20 seconds before the expected end on such records.
- A local recognition now knows where the song started even when the earlier play it matched
  began in the middle of the song (after a boundary without silence), so the expected end is right.
- **Stream dropouts no longer split a track:** the audio input sometimes delivers a second or two
  of digital silence in the middle of a track (a stream hiccup, not the record). That used to end
  the track and start recognition again; now it is ignored (log: `Stream dropout: … ignored`).
  Longer digital silence (over 10 seconds, e.g. the source stopped) still ends the track.
- **No stale title:** when a new track starts and is not recognized at the first check (after
  12 seconds), Music Assistant no longer keeps showing the previous track (with a finished progress
  bar) but shows your source again until the new track is recognized. During a short silence
  between tracks the last title stays, as before; after 30 seconds of silence (the record has
  ended) your source is shown again too.
- A track that turns out to be longer than AudD's length (another edit or version) is recognized
  again and then simply learned to its end.
- When a passage started on the tail of the previous track, the fingerprints from before the
  recognized song started are no longer learned.
- **Recording for troubleshooting** (advanced): new setting *Maximum recording (minutes)* (default
  60, up to 600), and every recording now gets the recognizer's own log of that session next to
  it, so it can be analysed later even after Music Assistant was restarted. A silence of 30
  seconds or more (turning the record over) starts a new recording, so a finished side can be
  copied while the next one plays; playback itself is not affected.

### Changed

- *Recognize all live audio sources* is now an advanced setting, listed after *Audio sources
  to recognize*: choosing your turntable is the normal way. If you had it turned on, it stays on.
- **Fewer false local matches when AudD can't help** (no token, AudD unavailable, or a track AudD
  doesn't know): the longer an unknown track plays, the more evidence a local match now needs. In
  a test without AudD, an unknown Barry White track was taken for a Phil Collins track after three
  minutes, and the wrong name was then learned. Normal recognition is unaffected: known tracks are
  still recognized in about 18 seconds.

### ⚠️ Worth knowing

- **Music Assistant moved the Sendspin input** from the separate *Sendspin Source* plugin into the
  Sendspin provider (Nightly of 2026-11-19 and later). A turntable you selected under the old
  plugin keeps being recognized; re-select it once under *Audio sources to recognize* to make that
  permanent. *Allow analysis of the audio input* (on by default) is a setting of the Sendspin
  player that has the input.
- Tracks learned before this version have no stored length yet. They get one through the extra
  AudD request above, the first time you play them again: expect about one AudD request per
  known track, once.

## 0.8.0 (2026-11-18)

Records you have played before are now recognized locally much more often and faster, so fewer
AudD requests are needed. Tested on a real turntable (two LP sides, 11 tracks played before):
**8 of 11 recognized locally, on average after ~16 seconds**, no wrong titles. On the same Barry
White side, 0.7 recognized 1 of 6 tracks locally; 0.8 recognized 5 of 6.

### ⚠️ Worth knowing

- **Disk space:** the new fast start (on by default) stores the first 30 seconds of every track
  with more fingerprints: about **35 % more disk space** for the fingerprint database (roughly
  0.7 MB per play of a 3-minute track instead of 0.5 MB). Turn off "Faster recognition at the
  start of known tracks" in the provider settings if you prefer a smaller database.
- Nothing else to do: your existing database is kept, and tracks you learned before also benefit
  (fully from the next time you play them).

### New

- **Faster recognition at the start of known tracks** (setting, on by default). The first 30
  seconds of every track are stored with more fingerprints, so a record you play again from the
  start is usually recognized locally within **12–18 seconds** instead of ~25 s, and without an
  AudD request. Measured offline on a real recording (Barry White, played twice): on the second
  play 5 of 6 tracks were recognized locally (before: 1 of 6), AudD requests 5 → 1, no wrong
  titles. Costs about **35 % more disk space**; turn it off for a very large collection. Tracks
  you learned before benefit from the next time you play them.

### Improved

- **Known tracks are recognized more reliably, also on a worn or slightly different play.** On
  vinyl only a few fingerprints repeat exactly on every play, so the provider now adds up the
  evidence of a whole track, but only fingerprints that line up *in time* with an earlier play
  (a real replay keeps the same timing; coincidental matches don't). Measured offline on three
  real recordings: replays recognized locally after **18–24 seconds**, including tracks that
  previously needed AudD or were not recognized at all, and **no wrong titles** in ten unknown
  tracks of 3–5 minutes each. This replaces the 0.7 rule that summed votes over the last four
  lookups without checking the timing.
- When AudD is due but local recognition is already close to a known track, the provider waits
  one more moment for local confirmation instead of spending an AudD request.


### Fixed

- **More missing album art.** Some ordinary-looking Deezer cover links turn out to redirect to
  Deezer's empty "no cover" image. The provider now checks a Deezer cover once when AudD provides
  it (a single small request to Deezer's image server) and ignores it when it is empty, so a real
  cover can fill it in later. Covers stored earlier are checked once at startup.
- No more harmless "invalid escape sequence" warnings from a dependency (pydub) in the Music
  Assistant log on the first start after installing or updating.

## 0.7.0 (2026-11-18)

Much better recognition on real vinyl, a database that stays small, and tools to find out why a
track isn't recognized. Tested on a real turntable: with the new speed correction **10 of 10
tracks were identified** (before: about 6 in 10), with **0.9 AudD requests per track** (before:
about 3) and in **~27 seconds** (before: ~56 s); known tracks were recognized locally in ~20–25 s.

### ⚠️ Action needed: choose your audio source

- **"Recognize all live audio sources" is now off by default**, following the Music Assistant
  maintainers' advice: you choose which live sources (e.g. your turntable) are analysed.
  **If you never changed this setting, recognition stops after the update until you open the
  provider's settings and select your source** under "Audio sources to recognize" (or turn
  "Recognize all live audio sources" back on). The log tells you when a source is skipped, and
  warns at startup when no source is selected.

### New

- **Speed correction for AudD (%)** (advanced setting, default 0). AudD is sensitive to a
  turntable's speed: a turntable running slightly fast made AudD miss about 4 in 10 tracks,
  including well-known ones. Samples sent to AudD can now be played a little slower or faster;
  local recognition is not affected. **For a turntable that runs fast, start with -2.** The README
  explains how to measure your turntable with a phone app.
- **Debugging aids** (advanced settings, off by default): *Save AudD samples for debugging* keeps
  the last 40 samples sent to AudD with their outcome and measurements; *Record the input stream
  for debugging* records the live input (up to 60 minutes). A no-match now logs AudD's reason.

### Improved

- **Faster local recognition of replays.** Weak but genuine matches now add up over consecutive
  lookups of the same track, so a replay is often recognized without asking AudD.
- **The database stays small.** Each track keeps the fingerprints of its **two most recent plays**;
  older plays are removed automatically (suggested by the Music Assistant maintainers).
- **Updates can never stop the provider because of the database.** An incompatible or damaged
  database file is moved aside (`fingerprints.sqlite3.incompatible`) and a new one is started.
- **Removing the provider also removes its fingerprint database.**
- Clearer logs: every identification says how and when (`Identified … via local after 22s`).

### Fixed

- **Repetitive audio could look like a strong local match.** In music with a repeating pattern
  (a drum machine, a held note) one fingerprint repeated in a sample counted as several votes, so
  one accidental match could look strong enough to show a wrong title. Each distinct fingerprint
  now counts once. (Found in a test with *In the Air Tonight*; no wrong title was shown.)
- **The album no longer jumps between plays.** AudD often names a different compilation album on
  every identification; the first stored metadata is now kept and later results only fill in what
  is missing. The ISRC (recording code) is stored when available.
- **Missing album art on some tracks.** Deezer's empty "no cover" image was stored as artwork. It is
  now ignored, a real cover can replace it, and stored placeholders are cleaned up at startup.
- Quiet passages no longer log a harmless "divide by zero" warning.

### Hardening

- A learned window with an abnormal number of fingerprints is not stored, so a noisy or broken
  source can't blow up the database.

## 0.6.0 (2026-11-17): first public release

First release of **Audio Recognizer: AudD + Local Fingerprints** as a Music Assistant community
provider. It recognizes the music playing on a live audio source, such as a turntable, and
shows title, artist, album and artwork on the player. It learns your music as you play it, so
records you've played before are recognized locally, without an external service.

### Requirements

- **Music Assistant Nightly** (2.11 development builds from November 2026 or later). For now this
  is required: live audio source analysis is not in a stable or beta release yet.
- A live audio source that allows audio analysis, such as *Sendspin Source* or *VBAN Receiver*.
- An **AudD API token** to identify music that hasn't been learned yet. AudD is a paid service with
  a free trial (300 requests in 14 days); see [audd.io/pricing](https://audd.io/pricing/). Without
  a token, only already-learned tracks are recognized.

### Features

- Recognizes music on live audio sources through Music Assistant's official audio-analysis API.
  No changes to Music Assistant are needed.
- Local recognition first: Panako fingerprints (via audalign), stored at a deterministic 6.25%
  density in a local SQLite database that survives restarts.
- AudD as discovery service for unknown tracks. Requests are throttled per track and per
  stream, and back off only on real errors.
- Learns every track it identifies, including its start, for future local recognition.
- Track changes are detected from the silence between tracks. A track never "recognizes
  itself" from fingerprints learned seconds earlier.
- Works whether Music Assistant plays the source at 44.1 or 48 kHz (fingerprints are
  normalized to one rate).
- Artwork from Apple Music, Spotify or Deezer when AudD provides it.
- Settings: AudD token, which sources to recognize, and (advanced) silence detection
  thresholds. English and Dutch texts.

### Known limitations

- **Records without silence between tracks** (DJ mixes, live albums, continuous sides,
  crossfades) **are not supported yet.** The previous title stays on screen, and the next track
  may be learned under the previous track's name. Turn the provider off for such records.
  Support is planned.
- The album shown is AudD's, often a digital compilation rather than the LP you are playing.
- Recognition can take 30–60 seconds into a track. A replayed track is recognized locally,
  sometimes only after ~45 seconds; faster local recognition is planned for the next version.
- Learned tracks can't be viewed or removed individually yet.
- The last track stays visible during silence until the next one is recognized. The time shown
  on the player is the time since the source started, not the position in the song.

### Privacy

Only ~12-second audio samples of music that isn't recognized locally are sent to AudD,
together with your API token. Local fingerprints are computed by this provider, stay on your
Music Assistant server, and are never uploaded.
