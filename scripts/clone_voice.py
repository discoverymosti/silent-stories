"""
Speak narration lines in the channel owner's own voice, cloned from one sample clip they provided.

Uses Chatterbox (Resemble AI, MIT licence), which runs on the free GitHub runner's CPU: no paid
service, and the sample never leaves the runner. Only ever use a sample of your OWN voice.

  python scripts/clone_voice.py SAMPLE.wav LINES.json OUT_DIR [language]

LINES.json is a list of strings; line N is written to OUT_DIR/line_NN.wav (empty lines are skipped).
Called by pipeline.py; needs requirements-voice.txt.
"""
from __future__ import annotations

import json
import sys
import time
import wave
from pathlib import Path

LANGS = {"arabic": "ar", "danish": "da", "german": "de", "greek": "el", "english": "en", "spanish": "es",
         "finnish": "fi", "french": "fr", "hebrew": "he", "hindi": "hi", "italian": "it", "japanese": "ja",
         "korean": "ko", "malay": "ms", "dutch": "nl", "norwegian": "no", "polish": "pl", "portuguese": "pt",
         "russian": "ru", "swedish": "sv", "swahili": "sw", "turkish": "tr", "chinese": "zh"}
WORDS_PER_SECOND = 2.4


def main() -> int:
    sample, lines_file, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    language = (sys.argv[4] if len(sys.argv) > 4 else "English").strip().lower()
    code = LANGS.get(language, language if language in LANGS.values() else "")
    if not code:
        sys.exit(f"Voice cloning does not support the language {language!r}. Supported: {', '.join(sorted(LANGS))}.")
    lines = json.loads(lines_file.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)

    import torch
    torch.manual_seed(7)  # the same voice colour from video to video
    t0 = time.time()
    if code == "en":
        from chatterbox.tts import ChatterboxTTS
        model = ChatterboxTTS.from_pretrained(device="cpu")
        speak = lambda text: model.generate(text, exaggeration=0.55, cfg_weight=0.5)  # noqa: E731
    else:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        model = ChatterboxMultilingualTTS.from_pretrained(device=torch.device("cpu"))
        speak = lambda text: model.generate(text, language_id=code, exaggeration=0.55, cfg_weight=0.5)  # noqa: E731
    model.prepare_conditionals(str(sample), exaggeration=0.55)  # the voice is read from the sample once
    print(f"  voice model ready in {time.time() - t0:.0f} s")

    for n, text in enumerate(lines):
        text = " ".join(str(text).split())
        if not text:
            continue
        expected = len(text.split()) / WORDS_PER_SECOND
        for attempt in range(3):  # a take that is far too long or short is a slip of the model: take it again
            t1 = time.time()
            wav = speak(text).squeeze().detach().cpu()
            seconds = wav.numel() / model.sr
            if 0.3 * expected <= seconds <= 3 * expected + 2:
                break
            print(f"  line {n + 1}: take {attempt + 1} is {seconds:.1f} s for a {expected:.1f} s line, again")
        else:
            sys.exit(f"Line {n + 1} could not be spoken properly after 3 takes: {text!r}")
        pcm = (wav.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()
        with wave.open(str(out / f"line_{n:02d}.wav"), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(model.sr)
            f.writeframes(pcm)
        print(f"  line {n + 1}: {seconds:.1f} s of speech in {time.time() - t1:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
