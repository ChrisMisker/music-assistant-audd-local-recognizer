# Audio Recognizer: AudD + Local Fingerprints

A Music Assistant audio-analysis provider that recognizes the music playing on a **live audio
source**, such as a turntable on a line-in, and shows the title, artist, album and artwork on
your player. It learns your music as you play it, so records you have played before are
recognized **locally**, without any external service.

> **AudD discovers. Local fingerprints remember. Music Assistant displays.**

> ⚠️ **Status: experimental.** It works on real vinyl and is tested end to end, but it has
> known limitations (see below). Feedback is welcome.
>
> **For now it requires Music Assistant Nightly.** The live-source analysis it depends on is
> not in a stable or beta release yet. It will work on stable once Music Assistant ships that
> feature there.

## What's new

**0.8.0** (2026-11-18)
- **Records you played before are recognized locally more often and faster:** on a real turntable
  8 of 11 known tracks locally, on average after ~16 seconds, without AudD (0.7: 1 of 6 on the
  same side). Two changes: a denser *fast start* for the first 30 s of a track, and evidence that
  lines up in time with an earlier play adds up over the whole track.
- ⚠️ Fast start (on by default) uses **~35 % more disk space**; you can turn it off in the settings.
- Fixes: more cases of missing album art; no more dependency warnings in the log.

**0.7.0** (2026-11-18)
- ⚠️ **Choose your audio source:** "Recognize all live audio sources" is now off by default. After
  updating, open the provider's settings and select your source (e.g. your turntable), or nothing
  is recognized.
- **Speed correction for AudD**: if your turntable runs a little fast or slow, AudD misses many
  tracks. With a -2 % correction our test went from ~6 to **10 of 10** tracks identified. See
  [Questions](#questions).
- Faster local recognition when you replay a record; the database stays small (two plays per
  track); updates can't break the database; album art fixes; debugging options.

**0.6.0**: first public release.

All changes: [CHANGELOG.md](CHANGELOG.md) and the
[GitHub releases](https://github.com/ChrisMisker/music-assistant-audd-local-recognizer/releases).

## How it works

1. You play a record through a live source in Music Assistant (for example the line-in of a **Sendspin** device).
2. The provider listens to that live audio. Silence between tracks marks a track change.
3. For each new track it first checks its **local fingerprint database**. A track it has heard
   before is recognized locally: no internet, no cost.
4. An unknown track is identified by **[AudD](https://audd.io)**, an online music recognition
   service, using a short sample.
5. While the track plays, the provider learns local fingerprints of it, so next time it is
   recognized locally.
6. The recognized title, artist, album and artwork appear on the Music Assistant player.

The more you play, the fewer AudD requests are needed.

## What to expect

Recognition is not instant: the provider needs to hear a piece of each track first. Measured on
a real turntable (November 2026, version 0.8, AudD speed correction set for that turntable):

| Situation | Title appears after | AudD requests |
|---|---|---|
| **A track you have never played** (identified by AudD) | usually **~27 seconds** | usually 1 |
| **A track you have played before** (recognized locally) | usually **13–20 seconds** (8 of 11 tracks; average ~16 s) | 0 |
| **A played-before track that isn't recognized locally** | ~27 seconds, via AudD (it is learned again) | 1 |
| **A track AudD doesn't identify** | not shown; the provider keeps trying sparingly | 3–5 for a normal track |

**Honest note:** some recordings sound different enough on every play that local recognition
misses them and AudD is asked again (3 of 11 known tracks in our test, partly because they were
learned before 0.8). Without the right speed correction AudD missed about **4 in 10 tracks** in
earlier tests (see [Questions](#questions)). A track that is never identified is also never
learned, so it won't be recognized locally either.

Why it takes this long:
- The first look happens after **12 seconds** of a track; after that every 6 seconds.
- AudD often doesn't recognize the first part of a track (the intro), so the provider waits
  until ~24 seconds before asking AudD, and tries again later if needed.
- On vinyl, every play sounds slightly different, so local recognition collects evidence over a
  few lookups before it is sure (only evidence that lines up in time with an earlier play
  counts). This avoids showing a wrong title.

So: **the first time you play a record, expect about half a minute per track before the title
shows (and usually one AudD request). The next times most tracks are recognized locally within
about 15–20 seconds, for free.** These figures will be updated as more records are tested.

## Requirements

- **Music Assistant Nightly** (2.11 development builds from November 2026 or later). **For now
  this is required:** analysing live audio sources is not in a stable or beta release yet.
- A **live audio source that allows audio analysis**, for example the line-in of a
  *Sendspin* device (the separate *Sendspin Source* plugin in older Nightlies) or *VBAN Receiver*.
- An **AudD API token**, needed to identify music that hasn't been learned yet (see below).

### About AudD: a paid service, with a free trial

AudD is a commercial API. **To identify new music you need an AudD account and API token.**

- **Free trial:** 300 requests within 14 days, no credit card required.
- After that a **paid plan** is needed. At the time of writing the smallest plan
  (*Indie*) is **$5/month for 1,000 requests**, plus $5 per extra 1,000. Check
  [audd.io/pricing](https://audd.io/pricing/) for current prices.

How many requests to expect:
- **A track you've played before** usually costs **0 requests**: it is recognized locally.
  Occasionally 1, when local recognition takes longer than ~24 seconds.
- **An unknown track** usually costs **1 to 3 requests**: AudD often doesn't recognize the
  first sample of a track and needs another try.
- **A track AudD can't identify** keeps being tried while it plays, but sparingly. The first
  request is sent after ~24 seconds of the track, the next ones 30 seconds apart (three in
  total), then every 90 seconds, with a hard maximum of **8 requests per track**. A typical
  3–5 minute track that AudD doesn't know therefore costs 4–5 requests; 8 is only reached by
  tracks of 9 minutes or longer.
- Requests stop as soon as the track is identified. There are never two requests within 30
  seconds of each other. After an AudD or network error the provider pauses (1 minute, doubling
  up to 15 minutes).
- Example: learning 10 LPs (~100 tracks) takes roughly 100–400 requests, depending on how well
  AudD knows them. After that, replaying them is (nearly) free.

Without a token the provider still recognizes everything it has already learned. It just can't
identify or learn new music.

Get a token at [dashboard.audd.io](https://dashboard.audd.io) and enter it in the provider
settings in Music Assistant.

## Privacy

- **Only audio of music that isn't recognized locally** is sent to AudD: one sample of about
  12 seconds of mono audio per request, together with your API token. A track may need several
  requests (up to 8, see [How many requests to expect](#about-audd-a-paid-service-with-a-free-trial)),
  so several samples of the same track can be sent. Nothing else is sent.
- **Known tracks are recognized locally**; no audio of them is sent anywhere.
- The **local fingerprints are computed by this provider** (Panako fingerprints via
  [audalign](https://pypi.org/project/audalign/)) and are stored only on your Music Assistant
  server, in `audd_local_recognizer/fingerprints.sqlite3` in its data folder. They are never
  uploaded or shared.
- Music Assistant shows the recognized metadata on the player and does not store it in its
  library.

## Disk space

The local fingerprint database lives on your Music Assistant server, in
`audd_local_recognizer/fingerprints.sqlite3` in its data folder (`/data` in the Home Assistant
add-on). **It grows as you play new music, and it can grow quickly**:

- each play of a ~3-minute track adds roughly **0.5 MB**;
- from version 0.7 each track keeps its **two most recent plays** (~1 MB for a 3-minute track,
  more for longer ones); older plays are removed automatically. With **faster recognition at
  the start** (on by default from 0.8) it is ~35 % more (~1.35 MB for a 3-minute track). (In 0.6.0 there is no limit yet:
  every play adds to the database.)

Rough upper limits from 0.7 (add ~35 % with fast start, on by default from 0.8):

| Your music | Database size (max) |
|---|---|
| 10 records (~100 tracks) | ~100 MB |
| 100 records (~1,000 tracks) | ~1 GB |
| 500 records (~5,000 tracks) | ~5 GB |

Things to keep in mind:
- **Home Assistant backups** include the Music Assistant add-on's data, so the database makes
  your backups larger too.
- On a small system (e.g. a Raspberry Pi or Home Assistant Green/Blue with limited storage), keep
  an eye on free disk space if you play a large collection.
- **Removing the provider** in Music Assistant deletes the database. There is no way yet to
  remove individual tracks (planned).

## Settings

| Setting | Meaning |
|---|---|
| **AudD API token** | Needed to identify new music. Optional if you only want local recognition. |
| **Audio sources to recognize** | **Choose your source here** (e.g. your turntable). Nothing is recognized until you do. |
| **Recognize all live audio sources** | Off by default. Turn on to recognize every live source that allows analysis. |
| **Faster recognition at the start of known tracks** | On by default. Stores more fingerprints of the first 30 seconds of every track, so a replay is usually recognized within 12–18 s without AudD. Uses ~35 % more disk space. |
| *Advanced:* **Silence threshold (dBFS)** | Audio at or below this level counts towards a track gap (default −45). |
| *Advanced:* **Resume threshold (dBFS)** | Audio at or above this level ends a gap (default −35). |
| *Advanced:* **Minimum track gap (seconds)** | How long the silence must last to count as a track change (default 2). |
| *Advanced:* **Speed correction for AudD (%)** | Default 0. If AudD misses many tracks, your turntable may run a little fast or slow: AudD's samples are then played this much faster or slower (e.g. **-2** for a turntable that runs ~2 % fast). Local recognition is not affected. |
| *Advanced:* **Record the input stream for debugging** | Off by default. Records the live input exactly as the recognizer receives it (WAV, ~10 MB per minute), plus the recognizer's log of each session, in the provider's data folder, up to the maximum below; then it stops. For troubleshooting only; the files are included in Home Assistant backups, so remove them afterwards. |
| *Advanced:* **Maximum recording (minutes)** | Total length of all recordings kept (default 60, up to 600). |
| *Advanced:* **Save AudD samples for debugging** | Off by default. Keeps the last 40 samples sent to AudD (WAV, plus their outcome and level/clipping/left-right measurements) in the provider's data folder, for troubleshooting. They stay on your server. |

The defaults were tuned on a real turntable. Only change them if track changes are missed
(raise the silence threshold) or detected inside quiet passages (lower it, or raise the
minimum gap).

## Limitations

These are known and documented on purpose. Some are being worked on (see [ROADMAP.md](ROADMAP.md)).

- **Records without silence between tracks are only partly supported** (live albums, continuous
  sides, crossfades, DJ mixes). Without a gap the provider can't hear the track change. Since 0.9
  it uses the track length that AudD reports: it stops learning a few seconds before the track
  should end, and a few seconds after that it starts listening for the next track, gap or not.
  The previous title stays on screen until the next track is recognized. Limits: tracks learned
  before 0.9 have no stored length until AudD identifies them again, so a local recognition of
  such a track has no end guard; if AudD's version of a track is longer than the one on your
  record, a few seconds of the next track can still be learned under the previous name; real DJ
  mixes and long crossfades are not handled. There is no way yet to remove wrongly learned tracks
  (see below).
- **One long track** (for example a 20-minute classical piece) works: it is recognized once,
  and learning stops after 10 minutes.
- **The album shown is AudD's**, often a digital compilation rather than the LP you are
  playing. Audio recognition identifies the recording, not your pressing. Using your own
  record collection's metadata is on the roadmap.
- **Recognition takes a little time**: up to about a minute per track for new music, somewhat
  less for music you've played before. See [What to expect](#what-to-expect).
- **During silence the last track stays visible** until the next one is recognized. Music
  Assistant has no way yet to withdraw it earlier.
- **The time shown on the player** is the time since the source started, not the position
  in the song.
- **Artwork is best effort:** taken from Apple Music, Spotify or Deezer when AudD provides it.
  Some recordings have none.
- **Live audio sources only.** Radio streams without song info are not supported yet.
- **The learned database can't be edited yet.** You can't view or remove individual learned
  tracks. Manual cleanup is on the roadmap. The database stays small on its own (at most about
  two plays per track are kept), and it is deleted when you remove the provider.

## Questions

**I bought a new turntable (or cartridge, phono preamp, audio interface). Do I need to start over?**
No. The local fingerprints describe the music, not your equipment, and are designed to tolerate
differences in sound, volume and small speed variations. Local recognition may be a bit slower
at first. If a track isn't recognized locally, it is identified via AudD again and re-learned
with your new equipment, and older plays are replaced over time automatically. At worst the
switch costs some extra AudD requests, once per track.

**I play a different pressing of the same album.** Same as above: the same recording is usually
recognized; otherwise it is identified via AudD once and learned.

**AudD doesn't recognize many of my tracks, even well-known ones.** Check your turntable's
speed: AudD is sensitive to it, while local recognition is not. In our tests a turntable that ran
a little fast made AudD miss about 4 in 10 tracks; samples played 2 % slower were recognized.
1. Measure the speed with a phone app, e.g. the free
   [Turntable Speed](https://apps.apple.com/app/id1604670976) for iPhone: put the phone on the
   platter while it turns. It shows the average speed, e.g. *33.61 rpm / +0.82 %*.
2. If you can, adjust the turntable's speed (many have a speed trim screw); this also makes your
   records sound right.
3. Otherwise set **Speed correction for AudD** (advanced setting). Note that the right value is
   not simply the measured deviation: in our test (turntable at +0.82 %) AudD did *not* recognize
   the sample at -0.8 % or -1 %, but did at **-2 %** and -4 %. AudD seems to need the audio
   slightly slower than the exact speed. **For a turntable that runs fast, start with -2.**

## Troubleshooting

The provider logs what it does and why at INFO level, in the Music Assistant log:

| Log line | Meaning |
|---|---|
| `Passage … started` | Music started after silence; a new track is being listened to. |
| `Track boundary: … dBFS for 2.0s` | A silence gap was detected: track change, or the record ended. |
| `Expected end of … at 245s` | The track's length is known: learning stops a few seconds before that moment. |
| `Track boundary (no gap): …` | The track should have ended but there was no silence (a gapless record): listening for the next track. |
| `LOCAL HIT: … votes=8 ratio=0.889` | Recognized locally. More votes means more certain (5+ needed). |
| `LOCAL CANDIDATE: … votes=1 -> rejected` | A coincidental match, correctly ignored. |
| `AudD attempt n/8` / `AudD HIT` / `AudD: no match` | An AudD request and its outcome. |
| `Identified … via local after 31s` | The track was recognized (locally or via AudD), and when. |
| `LOCAL CANDIDATE: … votes=2 acc=4 -> pending` | A weak match; `acc` adds up the votes of this track that line up in time with an earlier play (6 needed). |
| `LOCAL HIT: … lookups=3 shift=+0.42s … -> accepted (time-consistent)` | Recognized from evidence added up over several lookups. |
| `Learned … references` | Fingerprints stored for future local recognition. |
| `AudD: no match: …` | AudD found nothing; the reason follows (e.g. `no result`, `error 300`). |
| `No AudD API token configured` | Only local recognition is possible. |

If nothing is recognized, check that the source allows audio analysis (in recent Music Assistant
Nightlies: **"Allow analysis of the audio input"** in the settings of the Sendspin player with
the audio input, or in the VBAN Receiver settings; on by default), that your source is selected
in this provider's settings, and that music is above the silence threshold (the `audio resumed at … dBFS`
log line shows the level).

## Licence

[Apache License 2.0](LICENSE), the same licence as Music Assistant.

*Developed by Chris Misker, with the help of [Claude Code](https://claude.com/claude-code).*

## Development

Release history: [CHANGELOG.md](CHANGELOG.md).

See [CLAUDE.md](CLAUDE.md) for architecture, design decisions and the development
workflow, and [ROADMAP.md](ROADMAP.md) for plans and known issues. Tests run without Music
Assistant installed: `uv sync && uv run pytest`.
