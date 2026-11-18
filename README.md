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

**0.7.0** *(in testing, not released yet)*
- Faster local recognition when you replay a record: weak matches now add up over consecutive
  lookups.
- The database stays small: at most about two plays are kept per track (see
  [Disk space](#disk-space)).
- An update can never stop the provider because of an old or damaged database; it starts a new
  one and logs a warning.
- Removing the provider also removes its database.

**0.6.0**: first public release.

All changes: [CHANGELOG.md](CHANGELOG.md) and the
[GitHub releases](https://github.com/ChrisMisker/music-assistant-audd-local-recognizer/releases).

## How it works

1. You play a record through a live source in Music Assistant (for example **Sendspin Source**).
2. The provider listens to that live audio. Silence between tracks marks a track change.
3. For each new track it first checks its **local fingerprint database**. A track it has heard
   before is recognized locally: no internet, no cost.
4. An unknown track is identified by **[AudD](https://audd.io)**, an online music recognition
   service, using a short sample.
5. While the track plays, the provider learns local fingerprints of it, so next time it is
   recognized locally.
6. The recognized title, artist, album and artwork appear on the Music Assistant player.

The more you play, the fewer AudD requests are needed.

## Requirements

- **Music Assistant Nightly** (2.11 development builds from November 2026 or later). **For now
  this is required:** analysing live audio sources is not in a stable or beta release yet.
- A **live audio source that allows audio analysis**, for example *Sendspin Source* or
  *VBAN Receiver*.
- An **AudD API token**, needed to identify music that hasn't been learned yet (see below).

### About AudD: a paid service, with a free trial

AudD is a commercial API. **To identify new music you need an AudD account and API token.**

- **Free trial:** 300 requests within 14 days, no credit card required.
- After that a **paid plan** is needed. At the time of writing the smallest plan
  (*Indie*) is **$5/month for 1,000 requests**, plus $5 per extra 1,000. Check
  [audd.io/pricing](https://audd.io/pricing/) for current prices.

How many requests to expect:
- **An unknown track** usually costs **1 to 3 requests**. AudD sometimes needs a second try,
  and the provider waits ~24 seconds into a track before the first try.
- **A track you've played before** costs **0 requests**: it is recognized locally.
- Example: learning 10 LPs (~100 tracks) takes roughly 100–300 requests. After that, replaying
  them is free.

Without a token the provider still recognizes everything it has already learned. It just can't
identify or learn new music.

Get a token at [dashboard.audd.io](https://dashboard.audd.io) and enter it in the provider
settings in Music Assistant.

## Privacy

- **Only audio samples of music not recognized locally** are sent to AudD: about 12 seconds of
  mono audio per request, together with your API token. Nothing else is sent.
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
- from version 0.7 each track keeps at most about **two plays (~1 MB)**; older plays are removed
  automatically. (In 0.6.0 there is no limit yet: every play adds to the database.)

Rough upper limits from 0.7:

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
| **Recognize all live audio sources** | On by default. Turn off to pick specific sources. |
| **Audio sources to recognize** | The sources to listen to, when the option above is off. |
| *Advanced:* **Silence threshold (dBFS)** | Audio at or below this level counts towards a track gap (default −45). |
| *Advanced:* **Resume threshold (dBFS)** | Audio at or above this level ends a gap (default −35). |
| *Advanced:* **Minimum track gap (seconds)** | How long the silence must last to count as a track change (default 2). |

The defaults were tuned on a real turntable. Only change them if track changes are missed
(raise the silence threshold) or detected inside quiet passages (lower it, or raise the
minimum gap).

## Limitations

These are known and documented on purpose. Some are being worked on (see [ROADMAP.md](ROADMAP.md)).

- **Records without silence between tracks are not supported yet** (DJ mixes, live albums,
  continuous sides, crossfades). Without a gap the provider doesn't notice that the track
  changed: the previous title stays on screen, and **it may learn the next track under the
  previous track's name**, so that track can later be recognized wrongly. Support is planned
  for a later version (using the track length to detect where a track should end). Until then,
  we recommend turning the provider off for such records, or limiting it to sources where you
  play records with gaps. There is no way yet to remove wrongly learned tracks (see below).
- **One long track** (for example a 20-minute classical piece) works: it is recognized once,
  and learning stops after 10 minutes.
- **The album shown is AudD's**, often a digital compilation rather than the LP you are
  playing. Audio recognition identifies the recording, not your pressing. Using your own
  record collection's metadata is on the roadmap.
- **Recognition takes a little time.** Locally, about 12–40 seconds into a track. Via AudD,
  usually 25–60 seconds, because AudD often doesn't recognize the first seconds (intro) of a
  track.
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

## Troubleshooting

The provider logs what it does and why at INFO level, in the Music Assistant log:

| Log line | Meaning |
|---|---|
| `Passage … started` | Music started after silence; a new track is being listened to. |
| `Track boundary: … dBFS for 2.0s` | A silence gap was detected: track change, or the record ended. |
| `LOCAL HIT: … votes=8 ratio=0.889` | Recognized locally. More votes means more certain (5+ needed). |
| `LOCAL CANDIDATE: … votes=1 -> rejected` | A coincidental match, correctly ignored. |
| `AudD attempt n/8` / `AudD HIT` / `AudD: no match` | An AudD request and its outcome. |
| `Identified … via local after 31s` | The track was recognized (locally or via AudD), and when. |
| `LOCAL CANDIDATE: … votes=2 acc=4 -> pending` | A weak match; votes of consecutive lookups add up (6 needed). |
| `Learned … references` | Fingerprints stored for future local recognition. |
| `No AudD API token configured` | Only local recognition is possible. |

If nothing is recognized, check that the source allows audio analysis, that the provider
is enabled, and that music is above the silence threshold (the `audio resumed at … dBFS`
log line shows the level).

## Licence

[Apache License 2.0](LICENSE), the same licence as Music Assistant.

*Developed by Chris Misker, with the help of [Claude Code](https://claude.com/claude-code).*

## Development

Release history: [CHANGELOG.md](CHANGELOG.md).

See [CLAUDE.md](CLAUDE.md) for architecture, design decisions and the development
workflow, and [ROADMAP.md](ROADMAP.md) for plans and known issues. Tests run without Music
Assistant installed: `uv sync && uv run pytest`.
