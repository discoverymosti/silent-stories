"""
Check which free AI providers work with the configured keys (one tiny request each).
Run from Actions -> "Check free AI providers". Text checks cost nothing; each image check
uses one image from that provider's free allowance.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as p  # noqa: E402

PROMPT = 'Return JSON {"ok": true, "word": "<one happy English word>"}'
rows = []

if os.getenv("GEMINI_API_KEY"):
    try:
        p.ask_gemini(PROMPT)
        rows.append(("text", "gemini", "OK"))
    except SystemExit as e:
        rows.append(("text", "gemini", f"FAIL {str(e)[:80]}"))
else:
    rows.append(("text", "gemini", "no key"))
for name, url, env, models in p.FREE_TEXT_PROVIDERS:
    if not os.getenv(env):
        rows.append(("text", name, f"no key ({env})"))
        continue
    ok = p.ask_openai_compatible(name, url, os.environ[env], models, PROMPT)
    rows.append(("text", name, "OK" if ok else "FAIL (see log above)"))

check_images = os.getenv("CHECK_IMAGES", "true").lower() == "true"
tmp = Path(tempfile.mkdtemp())
prompt = p.full_prompt({"style": "cinematic film still", "grade": "warm"}, "a red kite over green hills")
for name, fn, env in [("cloudflare", lambda d: p.gen_image_cloudflare(prompt, d, 1), "CF_API_TOKEN"),
                      ("huggingface", lambda d: p.gen_image_hf(prompt, d), "HF_TOKEN"),
                      ("together", lambda d: p.gen_image_together(prompt, d), "TOGETHER_API_KEY"),
                      ("pollinations", lambda d: p.gen_image_pollinations(prompt, d, 1), "POLLINATIONS_TOKEN")]:
    if name != "pollinations" and not os.getenv(env):
        rows.append(("image", name, f"no key ({env})"))
    elif not check_images:
        rows.append(("image", name, "skipped"))
    else:
        rows.append(("image", name, "OK" if fn(tmp / f"{name}.png") else "FAIL (see log above)"))

print("\nFree AI providers:")
for kind, name, status in rows:
    print(f"  {kind:5}  {name:13}  {status}")
summary = os.getenv("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as f:
        f.write("| type | provider | status |\n|---|---|---|\n")
        f.writelines(f"| {k} | {n} | {s} |\n" for k, n, s in rows)
