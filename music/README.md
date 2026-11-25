# Music (BGM)

Drop royalty-free tracks here to replace the built-in ambient pad.
Formats: mp3, m4a, aac, wav, ogg, opus, flac.

- `music/<mood>/`: used for that mood (`warm`, `sad`, `tense`, `curious`, `uplifting`)
- `music/`: used for any mood that has no folder of its own

What happens to a track: one is picked at random, silence at its start is skipped, it is cut to the
video's length (looped if shorter, started at a random point if much longer), levelled to the same
loudness as every other upload and faded in and out. Cover art inside the file is ignored. A file
that can't be read is skipped; the run's log names the track used (`'music': ...`).

Safe sources: YouTube Studio → Audio Library (filter "Attribution not required"), Pixabay Music.
Do not use copyrighted songs. Shorts with claimed music can be blocked or demonetised.
