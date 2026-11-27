"""
Speak narration lines in the channel owner's own voice, cloned from one sample clip they provided.

Uses Chatterbox (Resemble AI, MIT licence), which runs on the free GitHub runner's CPU: no paid
service, and the sample never leaves the runner. Only ever use a sample of your OWN voice.

  python scripts/clone_voice.py SAMPLE.wav LINES.json OUT_DIR [language]

LINES.json is a list; line N is written to OUT_DIR/line_NN.wav (empty lines are skipped). An item is
the text to speak in the owner's voice, or {"text", "file", "voice", "ref"} for a CHARACTER line: any
"voice" other than "narrator" is spoken in the model's own built-in voice (not a clone of anyone), or
in the voice of the clip "ref" when the owner provided one for that character. The pitch of every
take is written to OUT_DIR/pitch.json, so the pipeline can tune character voices.
Called by pipeline.py; needs requirements-voice.txt.
"""
from __future__ import annotations

import json
import os
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
    # the workflow passes HF_TOKEN even when that secret is not set: an empty token makes the model
    # download send an invalid "Bearer " header and fail. The model is public; no token is needed.
    for var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        if not os.environ.get(var, "").strip():
            os.environ.pop(var, None)
    sample, lines_file, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    language = (sys.argv[4] if len(sys.argv) > 4 else "English").strip().lower()
    code = LANGS.get(language, language if language in LANGS.values() else "")
    if not code:
        sys.exit(f"Voice cloning does not support the language {language!r}. Supported: {', '.join(sorted(LANGS))}.")
    lines = json.loads(lines_file.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)

    # how the voice is spoken (repository variables, so they can be tuned by ear without a code change):
    #   VOICE_CFG          0-1, higher keeps closer to the sample's own way of speaking
    #   VOICE_EXAGGERATION 0.25-1, higher is more expressive but less steady
    #   VOICE_TEMPERATURE  lower is steadier and more even, higher more lively
    # The defaults are the "4-more-expressive" setting the owner picked by ear in the voice test
    # (the model's own defaults are 0.5 / 0.5 / 0.8).
    def setting(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name) or default)
        except ValueError:
            return default
    cfg, exaggeration, temperature = (setting("VOICE_CFG", 0.3), setting("VOICE_EXAGGERATION", 0.75),
                                      setting("VOICE_TEMPERATURE", 0.9))
    print(f"  voice settings: cfg {cfg}, exaggeration {exaggeration}, temperature {temperature}")

    import torch
    torch.manual_seed(7)  # the same voice colour from video to video
    t0 = time.time()
    if code == "en":
        from chatterbox.tts import ChatterboxTTS
        model = ChatterboxTTS.from_pretrained(device="cpu")
        speak = lambda text: model.generate(text, exaggeration=exaggeration, cfg_weight=cfg,  # noqa: E731
                                            temperature=temperature)
    else:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        model = ChatterboxMultilingualTTS.from_pretrained(device="cpu")  # the string: the package compares it to "cpu"
        speak = lambda text: model.generate(text, language_id=code, exaggeration=exaggeration,  # noqa: E731
                                            cfg_weight=cfg, temperature=temperature)
    builtin = model.conds  # the model's own stock voice: what characters speak in
    model.prepare_conditionals(str(sample), exaggeration=exaggeration)  # the owner's voice, read from the sample once
    voices = {"narrator": model.conds}
    print(f"  voice model ready in {time.time() - t0:.0f} s")

    def pitch_of(wav) -> float | None:
        """Median pitch (Hz) of the voiced parts of a take."""
        try:
            import librosa
            import numpy as np
            y = wav.numpy().astype("float32")
            f0 = librosa.yin(y, fmin=65, fmax=500, sr=model.sr, frame_length=2048)
            rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
            n = min(len(f0), len(rms))
            voiced = f0[:n][rms[:n] > 0.3 * rms[:n].max()]
            return round(float(np.median(voiced)), 1) if len(voiced) else None
        except Exception as e:  # noqa: BLE001 - pitch is a nicety
            print(f"  pitch not measured ({str(e)[:80]})")
            return None

    pitch = {}
    for n, item in enumerate(lines):
        item = item if isinstance(item, dict) else {"text": item}
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        name, voice, ref = item.get("file") or f"line_{n:02d}.wav", item.get("voice") or "narrator", item.get("ref") or ""
        character = voice != "narrator"
        try:
            key = ref or voice
            if character and key not in voices:
                if ref:  # a clip the owner provided for this kind of character
                    model.prepare_conditionals(ref, exaggeration=0.8)
                    voices[key] = model.conds
                else:
                    voices[key] = builtin or voices["narrator"]
            model.conds = voices[key if character else "narrator"]
            expected = len(text.split()) / WORDS_PER_SECOND
            for attempt in range(3):  # a take that is far too long or short is a slip of the model: take it again
                t1 = time.time()
                if character:  # characters act: livelier than the narrator
                    kw = {"exaggeration": 0.8, "cfg_weight": 0.4, "temperature": 0.9}
                    wav = (model.generate(text, **kw) if code == "en" else model.generate(text, language_id=code, **kw))
                else:
                    wav = speak(text)
                wav = wav.squeeze().detach().cpu()
                seconds = wav.numel() / model.sr
                if 0.3 * expected <= seconds <= 3 * expected + 2:
                    break
                print(f"  {name}: take {attempt + 1} is {seconds:.1f} s for a {expected:.1f} s line, again")
            else:
                raise RuntimeError("could not be spoken properly after 3 takes")
        except Exception as e:  # noqa: BLE001
            if not character:
                sys.exit(f"Line {n + 1} failed ({str(e)[:200]}): {text!r}")
            print(f"  {name}: character line skipped ({str(e)[:120]})")  # the video works without it
            continue
        pcm = (wav.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()
        with wave.open(str(out / name), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(model.sr)
            f.writeframes(pcm)
        pitch[name] = pitch_of(wav)
        print(f"  {name} [{voice}]: {seconds:.1f} s of speech in {time.time() - t1:.0f} s, pitch {pitch[name]} Hz")
    (out / "pitch.json").write_text(json.dumps(pitch), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
