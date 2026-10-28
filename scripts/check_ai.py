"""
Check which free AI providers work with the configured keys (one tiny request each).
Run from Actions -> "Check free AI providers". Text checks cost nothing; each image check
uses one image from that provider's free allowance.
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

import requests

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
rows += [("text", name, f"no key ({env})") for name, _, env, _ in p.FREE_TEXT_PROVIDERS if not os.getenv(env)]
for name, url, key, models in p.text_providers():
    ok = p.ask_openai_compatible(name, url, key, models, PROMPT)
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

# Which image models does this Cloudflare account offer right now? (docs change often)
acct, token = os.getenv("CF_ACCOUNT_ID"), os.getenv("CF_API_TOKEN")
if acct and token:
    base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai"
    auth = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(f"{base}/models/search", headers=auth, params={"task": "Text-to-Image", "per_page": 100},
                         timeout=60)
        models = r.json().get("result") or []
        print(f"\nCloudflare text-to-image models ({r.status_code}):")
        for m in models:
            props = {x.get("property_id"): x.get("value") for x in m.get("properties") or []}
            print(f"  {m.get('name')}  | {str(props.get('price') or '')[:160]} | {(m.get('description') or '')[:90]}")
        for m in [m.get("name", "") for m in models if "flux-2" in m.get("name", "")]:
            if not check_images:
                rows.append(("image", m, "skipped"))
                continue
            w, h = p._gen_size()
            r = requests.post(f"{base}/run/{m}", headers=auth, timeout=180,
                              files={"prompt": (None, prompt), "width": (None, str(w)), "height": (None, str(h))})
            if r.status_code != 200:  # some models take JSON instead of multipart
                r = requests.post(f"{base}/run/{m}", headers=auth, timeout=180,
                                  json={"prompt": prompt, "width": w, "height": h})
            img = None
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                img = (r.json().get("result") or {}).get("image")
            elif r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                img = base64.b64encode(r.content).decode()
            rows.append(("image", m, f"OK ({len(base64.b64decode(img)) // 1024} KB, {w}x{h} asked)" if img
                         else f"FAIL {r.status_code}: {r.text[:120]}"))
    except Exception as e:  # noqa: BLE001 - a probe must never fail the check
        print(f"Cloudflare model listing failed: {e}")

print("\nFree AI providers:")
for kind, name, status in rows:
    print(f"  {kind:5}  {name:13}  {status}")
summary = os.getenv("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as f:
        f.write("| type | provider | status |\n|---|---|---|\n")
        f.writelines(f"| {k} | {n} | {s} |\n" for k, n, s in rows)
