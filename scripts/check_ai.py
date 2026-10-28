"""
Check which free AI providers work with the configured keys (one tiny request each).
Run from Actions -> "Check free AI providers". Text checks cost nothing; each image check
uses one image from that provider's free allowance.
"""
import base64
import os
import re
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
    if name not in ("groq", "openrouter"):
        ok = p.ask_openai_compatible(name, url, key, models, PROMPT)
        rows.append(("text", name, "OK" if ok else "FAIL (see log above)"))
    else:  # these retire models often: test each listed model, then show what the key offers now
        for m in models:
            ok = p.ask_openai_compatible(name, url, key, [m], PROMPT)
            rows.append(("text", f"{name}/{m}", "OK" if ok else "FAIL (see log above)"))
        try:
            r = requests.get(url.replace("/chat/completions", "/models"),
                             headers={"Authorization": f"Bearer {key}"}, timeout=60)
            ids = sorted(m.get("id", "") for m in r.json().get("data") or [])
            ids = [i for i in ids if name != "openrouter" or i.endswith(":free")]
            print(f"\n{name} models ({r.status_code}): {', '.join(ids)}\n")
        except Exception as e:  # noqa: BLE001 - a probe must never fail the check
            print(f"{name} model listing failed: {e}")

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
                print(f"  {m} multipart -> {r.status_code}: {r.text[:200]}")
                r = requests.post(f"{base}/run/{m}", headers=auth, timeout=180,
                                  json={"prompt": prompt, "width": w, "height": h})
            img = None
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                img = (r.json().get("result") or {}).get("image")
            elif r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                img = base64.b64encode(r.content).decode()
            rows.append(("image", m, f"OK ({len(base64.b64decode(img)) // 1024} KB, {w}x{h} asked)" if img
                         else f"FAIL {r.status_code}: {r.text[:120]}"))
        # models listed at $0: do they work even when the daily allowance is used up?
        for m in ("@cf/bytedance/stable-diffusion-xl-lightning", "@cf/lykon/dreamshaper-8-lcm"):
            if not check_images:
                continue
            w, h = p._gen_size()
            r = requests.post(f"{base}/run/{m}", headers=auth, timeout=180,
                              json={"prompt": prompt, "width": w, "height": h, "num_steps": 8})
            ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image")
            rows.append(("image", m, f"OK ({len(r.content) // 1024} KB)" if ok else f"FAIL {r.status_code}: {r.text[:120]}"))
    except Exception as e:  # noqa: BLE001 - a probe must never fail the check
        print(f"Cloudflare model listing failed: {e}")

# AI Horde: free community GPUs, no signup (anonymous key). Which image models are online, and how
# long does one image take?
if check_images and os.getenv("CHECK_HORDE", "true") == "true":
    import time as _t
    H = "https://aihorde.net/api/v2"
    hdr = {"apikey": os.getenv("AI_HORDE_KEY", "0000000000"),
           "Client-Agent": "silent-stories:1.0:github.com/discoverymosti/silent-stories"}
    try:
        live = requests.get(f"{H}/status/models", params={"type": "image"}, headers=hdr, timeout=60).json()
        live = sorted(live, key=lambda m: -m.get("count", 0))
        print("\nAI Horde image models online (workers, queued):")
        for m in [m for m in live if re.search(r"flux|sdxl|albedo|juggernaut|realvis|z-image|qwen", m["name"], re.I)][:15]:
            print(f"  {m['name']:45} workers={m.get('count')} queued={m.get('queued')} eta={m.get('eta')}s")
        flux = [m["name"] for m in live if "flux" in m["name"].lower() and m.get("count", 0) > 0]
        model = flux[0] if flux else live[0]["name"]
        w, h = (1024, 576) if p.FILM else (576, 1024)
        params = ({"sampler_name": "k_euler", "cfg_scale": 1.0, "steps": 4} if "flux" in model.lower()
                  else {"sampler_name": "k_dpmpp_2m", "cfg_scale": 6.0, "steps": 24})
        t0 = _t.time()
        job = requests.post(f"{H}/generate/async", headers=hdr, timeout=60, json={
            "prompt": prompt[:1000], "params": {**params, "width": w, "height": h, "n": 1},
            "models": [model], "nsfw": False, "censor_nsfw": True, "r2": True, "slow_workers": True}).json()
        print(f"  submitted to {model}: {str(job)[:200]}")
        status = "FAIL: " + str(job.get("message", job))[:100]
        if job.get("id"):
            while _t.time() - t0 < 600:
                chk = requests.get(f"{H}/generate/check/{job['id']}", headers=hdr, timeout=60).json()
                if chk.get("done") or chk.get("faulted") or not chk.get("is_possible", True):
                    break
                _t.sleep(10)
            res = requests.get(f"{H}/generate/status/{job['id']}", headers=hdr, timeout=60).json()
            gens = res.get("generations") or []
            if gens and gens[0].get("img"):
                img = requests.get(gens[0]["img"], timeout=120)
                status = f"OK in {round(_t.time() - t0)}s via {gens[0].get('model')} ({len(img.content) // 1024} KB)"
            else:
                status = f"FAIL after {round(_t.time() - t0)}s: {str(chk)[:120]}"
        rows.append(("image", "ai-horde (no key)", status))
    except Exception as e:  # noqa: BLE001
        rows.append(("image", "ai-horde (no key)", f"FAIL {e}"))

# Who used the Cloudflare allowance, and when? (needs Account Analytics read on the token)
if acct and token:
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    query = """query ($acct: string!, $start: Date!, $end: Date!) { viewer { accounts(filter: {accountTag: $acct}) {
      aiInferenceAdaptiveGroups(limit: 200, filter: {date_geq: $start, date_leq: $end}, orderBy: [datetimeHour_ASC]) {
        count sum { totalNeurons } dimensions { datetimeHour modelId } } } } }"""
    try:
        r = requests.post("https://api.cloudflare.com/client/v4/graphql", headers={"Authorization": f"Bearer {token}"},
                          json={"query": query, "variables": {"acct": acct, "start": str(today - _dt.timedelta(days=1)),
                                                              "end": str(today)}}, timeout=60)
        d = r.json()
        if d.get("errors"):
            print(f"\nCloudflare usage analytics unavailable: {str(d['errors'])[:300]}")
        else:
            groups = d["data"]["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"]
            print("\nCloudflare Workers AI usage by hour (UTC):")
            for g in groups:
                print(f"  {g['dimensions']['datetimeHour']}  {g['dimensions']['modelId']:45} "
                      f"requests={g['count']:4}  neurons={round(g['sum']['totalNeurons'])}")
    except Exception as e:  # noqa: BLE001
        print(f"Cloudflare usage analytics failed: {e}")

print("\nFree AI providers:")
for kind, name, status in rows:
    print(f"  {kind:5}  {name:13}  {status}")
summary = os.getenv("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as f:
        f.write("| type | provider | status |\n|---|---|---|\n")
        f.writelines(f"| {k} | {n} | {s} |\n" for k, n, s in rows)
