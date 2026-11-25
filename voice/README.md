# Your voice

Every video is narrated in **your own voice**. You provide one clip, once; AI then speaks each video's
narration in that voice (open-source Chatterbox model, run on the GitHub runner: no paid service).

## Add your voice sample (once)
1. Record **15-30 seconds** of yourself talking naturally, the way you would tell a story.
   Quiet room, no music, phone a hand's width from your mouth. Any phone voice recorder works.
2. Upload it **into this folder** named `sample` + its ending, e.g. `voice/sample.mp3`, `sample.m4a` or
   `sample.wav` (github.com: open this folder → *Add file* → *Upload files*).
3. Run the **Test my cloned voice** workflow (Actions tab) and download the result to hear it.
   Not happy? Replace the sample with a cleaner or livelier one and test again.

Until a sample is here, no video is published (never a silent one). Only ever use your own voice.

Privacy: if this repository is public, anyone can download the sample. Your narrated videos are
public too, but a clean sample is easier to misuse; make the repository private if that worries you.

## Settings (repository variables)
- `NARRATION`: `clone` (default, as above) · `record` (you record every video yourself: the pipeline
  writes `voice/<story>/script.md`, you upload your recording into that folder, and the video is built
  around it) · `optional` (like record, silent when there is no recording) · `off` (silent videos).
- Language: the main (entertainment) channel is narrated in **Hindi**, the TECH channel in **English**.
  Change them with `NARRATION_LANGUAGE` (main) and `TECH_NARRATION_LANGUAGE` (TECH); 23 languages work.
  One sample serves both: the model keeps your voice across languages.
- The TECH channel uses the same sample; a different one can go in `channels/tech/voice/sample.*`.
