#!/usr/bin/env python3
"""안온 제작 공장 — GitHub Actions에서 무료로 카드·릴스를 렌더하고 목소리를 만든다.

사무실 제작 담당(Claude)이 jobs/<id>/job.json(+ HTML 파일)을 올리면 이 스크립트가
  - kind "render": 카드 HTML → PNG(+720×900 축소본), 릴스 HTML(GSAP 타임라인) → 프레임 렌더 → MP4(+목소리·배경음)
  - kind "voice" : 일레븐랩스 API로 목소리 MP3 + 글자별 시간표(alignment)
  - kind "voices": 일레븐랩스 계정의 목소리 이름·ID 목록
  - kind "bank"  : 생성 이미지(7일 뒤 사라지는 주소)를 bank/ 에 영구 보관
  - kind "cardreel": 카드 이미지 주소들 → 9:16 카드 슬라이드 릴스(배경음). render 작업에 "cardReel":{...}을 주면 카드와 함께 만든다
를 만들어 out/<id>/r<rev>/ 와 results/<id>.json 에 쓴다(media 브랜치). 공개 주소는 GitHub Pages.

같은 job은 rev가 바뀔 때만 다시 만든다. 토큰·키는 환경 변수로만 받고 파일·기록에 쓰지 않는다.
"""
import base64
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
JOBS = os.path.join(ROOT, "jobs")
WORK = os.environ.get("FACTORY_OUT", os.path.join(ROOT, "_media"))  # media 브랜치 작업 폴더
REPO = os.environ.get("GITHUB_REPOSITORY", "pypl4325/anon-media")
BRANCH = os.environ.get("FACTORY_MEDIA_BRANCH", "media")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
_owner, _name = REPO.split("/")
PAGES = os.environ.get("FACTORY_PAGES_BASE", f"https://{_owner.lower()}.github.io/{_name}")  # 링크팜에 주는 주소(올바른 파일 형식)
KST = dt.timezone(dt.timedelta(hours=9))
FPS = 30
UA = "Mozilla/5.0 (anon-media factory)"
SECRETS = [v for v in [os.environ.get("ELEVENLABS_API_KEY", "")] if v]


def now():
    return dt.datetime.now(KST).isoformat(timespec="seconds")


def scrub(text):
    text = str(text)
    for s in SECRETS:
        text = text.replace(s, "***")
    return text[-1500:]


def log(*a):
    print(*a, flush=True)


def download(url, path, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
            return path
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"download failed: {url} ({last})")


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-800:]}")
    return p.stdout


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(out.strip() or 0)


def read_html(job_dir, spec, job=None):
    """spec: {"html": "..."} 또는 {"file": "reel.html"}, 선택 "vars": {"scene1": "https://..."} → {{scene1}} 바꿈"""
    if isinstance(spec, str):
        spec = {"file": spec}
    html = spec["html"] if spec.get("html") else open(os.path.join(job_dir, spec["file"]), encoding="utf-8").read()
    for k, v in {**((job or {}).get("vars") or {}), **(spec.get("vars") or {})}.items():
        html = html.replace("{{" + k + "}}", str(v))
    left = sorted(set(re.findall(r"\{\{\w+\}\}", html)))
    if left:
        raise RuntimeError(f"채우지 않은 자리표시 {left} — job vars 확인")
    return html


# ---------------- 브라우저 ----------------
class Browser:
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.b = self._pw.chromium.launch(args=["--disable-web-security", "--allow-file-access-from-files",
                                                 "--autoplay-policy=user-gesture-required", "--hide-scrollbars"])

    def page(self, w, h):
        ctx = self.b.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
        # 오디오·비디오는 브라우저에서 재생하지 않는다(소리는 ffmpeg로 따로 섞음)
        ctx.add_init_script("HTMLMediaElement.prototype.play=function(){return Promise.resolve()};")
        return ctx.new_page()

    def close(self):
        self.b.close()
        self._pw.stop()


def load(page, html, tmpdir, name):
    path = os.path.join(tmpdir, name)
    open(path, "w", encoding="utf-8").write(html)
    page.goto("file://" + path, wait_until="load", timeout=180000)
    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:  # noqa: BLE001
        pass
    page.evaluate("""async () => {
      if (document.fonts && document.fonts.ready) { await document.fonts.ready; }
      const imgs = Array.from(document.images);
      await Promise.all(imgs.map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; })));
    }""")
    bad = page.evaluate("Array.from(document.images).filter(i=>!i.naturalWidth).map(i=>i.src)")
    return bad


# ---------------- 카드 ----------------
def render_cards(br, job, job_dir, out_dir, tmpdir):
    cards = job.get("cards") or []
    w, h = (job.get("cardSize") or [1080, 1350])
    urls, thumbs, warn = [], [], []
    for i, c in enumerate(cards):
        name = str(c.get("name") or f"{i + 1:02d}")
        page = br.page(w, h)
        bad = load(page, read_html(job_dir, c, job), tmpdir, f"card_{name}.html")
        if bad:
            warn.append(f"card {name}: 이미지 못 불러옴 {bad[:2]}")
        png = os.path.join(out_dir, f"card_{name}.png")
        page.screenshot(path=png, type="png")
        page.context.close()
        urls.append(png)
        if job.get("thumbs", True):
            jpg = os.path.join(out_dir, f"card_{name}_720.jpg")
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", png, "-vf", "scale=720:900", "-q:v", "3", jpg])
            thumbs.append(jpg)
    sheet = None
    if urls:
        sheet = os.path.join(out_dir, "cards_sheet.jpg")
        cols = min(len(urls), 5)
        inputs = sum([["-i", u] for u in urls], [])
        layout = "|".join(f"{(k % cols) * 216}_{(k // cols) * 270}" for k in range(len(urls)))
        fc = "".join(f"[{k}:v]scale=216:270[s{k}];" for k in range(len(urls)))
        fc += "".join(f"[s{k}]" for k in range(len(urls))) + f"xstack=inputs={len(urls)}:layout={layout}:fill=black[o]" if len(urls) > 1 else "[s0]copy[o]"
        run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc, "-map", "[o]", "-q:v", "4", sheet])
    return urls, thumbs, sheet, warn


# ---------------- 릴스 ----------------
SEEK_JS = """(t) => {
  const tls = Object.values(window.__timelines || {});
  if (window.gsap) { try { gsap.globalTimeline.pause(); gsap.globalTimeline.seek(t, false); } catch (e) {} }
  tls.forEach(tl => { try { tl.pause(); tl.seek(t, false); } catch (e) {} });
  if (document.getAnimations) document.getAnimations().forEach(a => { try { a.pause(); a.currentTime = t * 1000; } catch (e) {} });
  return true;
}"""


def reel_duration(page, job):
    if job.get("reel", {}).get("duration"):
        return float(job["reel"]["duration"])
    d = page.evaluate("""() => {
      const tls = Object.values(window.__timelines || {});
      let m = 0; tls.forEach(t => { try { m = Math.max(m, t.duration()); } catch (e) {} });
      document.querySelectorAll('audio[data-duration]').forEach(a => { m = Math.max(m, (+a.dataset.start||0) + (+a.dataset.duration||0)); });
      return m;
    }""")
    if not d:
        raise RuntimeError("릴스 길이를 알 수 없음 — window.__timelines 또는 reel.duration 필요")
    return float(d)


def audio_tracks(html, job):
    tracks = []
    for m in re.finditer(r"<audio\b[^>]*>", html):
        tag = m.group(0)
        src = re.search(r'\bsrc="([^"]+)"', tag)
        if not src:
            continue
        def attr(n, d):
            x = re.search(rf'\b{n}="([^"]*)"', tag)
            return float(x.group(1)) if x and x.group(1) != "" else d
        tid = re.search(r'\bid="([^"]*)"', tag)
        loop = bool(re.search(r"\bdata-loop\b", tag)) or ("bgm" in (tid.group(1).lower() if tid else ""))
        tracks.append({"src": src.group(1), "start": attr("data-start", 0.0), "loop": loop,
                       "duration": attr("data-duration", 0.0), "volume": attr("data-volume", 1.0)})
    for t in job.get("reel", {}).get("audio", []) or []:  # html 밖에서 따로 줄 수도 있음
        tracks.append({"src": t["url"], "start": float(t.get("start", 0)), "duration": float(t.get("duration", 0)),
                       "volume": float(t.get("volume", 1)), "loop": bool(t.get("loop"))})
    return tracks


def render_reel(br, job, job_dir, out_dir, tmpdir):
    spec = job["reel"]
    html = read_html(job_dir, spec, job)
    w, h = spec.get("size") or [1080, 1920]
    fps = int(spec.get("fps") or FPS)
    page = br.page(w, h)
    bad = load(page, html, tmpdir, "reel.html")
    warn = [f"reel: 이미지 못 불러옴 {bad[:3]}"] if bad else []
    dur = reel_duration(page, job)
    n = int(round(dur * fps))
    silent = os.path.join(tmpdir, "video.mp4")
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg",
                           "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", str(spec.get("crf") or 23), "-pix_fmt", "yuv420p",
                           "-r", str(fps), silent], stdin=subprocess.PIPE)
    t0 = time.time()
    for i in range(n):
        page.evaluate(SEEK_JS, i / fps)
        ff.stdin.write(page.screenshot(type="jpeg", quality=92))
        if i % 300 == 0:
            log(f"  frame {i}/{n} ({time.time() - t0:.0f}s)")
    ff.stdin.close()
    if ff.wait() != 0:
        raise RuntimeError("ffmpeg 영상 인코딩 실패")
    page.context.close()

    # 소리: <audio> 태그(목소리·배경음)를 시작·길이·볼륨대로 섞는다
    tracks = audio_tracks(html, job)
    final = os.path.join(out_dir, "reel.mp4")
    if tracks:
        ins, fl = [], []
        for k, t in enumerate(tracks):
            p = download(t["src"], os.path.join(tmpdir, f"a{k}" + os.path.splitext(t["src"].split("?")[0])[1][:5]))
            ins += ["-i", p]
            d = t["duration"] or dur
            # 배경음만 반복(목소리는 data-duration이 파일보다 길어도 반복하지 않고 뒤를 무음으로)
            loop = "aloop=loop=-1:size=2147483647," if t["loop"] and t["duration"] and t["duration"] > probe_duration(p) + 0.05 else ""
            fl.append(f"[{k + 1}:a]{loop}atrim=0:{d:.3f},asetpts=N/SR/TB,volume={t['volume']},adelay={int(t['start'] * 1000)}:all=1[a{k}]")
        mix = "".join(f"[a{k}]" for k in range(len(tracks)))
        fc = ";".join(fl) + f";{mix}amix=inputs={len(tracks)}:duration=longest:normalize=0,apad,atrim=0:{dur:.3f}[aout]"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, *ins, "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-shortest", "-movflags", "+faststart", final])
    else:
        warn.append("reel: 소리 없음(<audio> 태그 없음)")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-c", "copy", "-movflags", "+faststart", final])

    sheet = video_sheet(final, dur, os.path.join(out_dir, "reel_sheet.jpg"), tmpdir)
    return final, sheet, dur, warn, round(time.time() - t0)


def video_sheet(video, dur, sheet, tmpdir, n=6):
    """검수용 장면 시트(6장을 가로로)"""
    frames = []
    for k in range(n):
        t = dur * k / n + 0.4
        f = os.path.join(tmpdir, f"q{k}_{os.path.basename(sheet)}.jpg")
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(t, dur - 0.1):.2f}", "-i", video, "-frames:v", "1",
             "-vf", "scale=270:480", f])
        frames.append(f)
    run(["ffmpeg", "-y", "-loglevel", "error", *sum([["-i", f] for f in frames], []), "-filter_complex",
         "".join(f"[{k}:v]" for k in range(n)) + f"hstack=inputs={n}[o]", "-map", "[o]", "-q:v", "4", sheet])
    return sheet


# ---------------- 카드 슬라이드 릴스(11/5, G3) ----------------
def card_reel(images, out_path, tmpdir, opt=None):
    """카드 이미지(1080×1350) 여러 장 → 1080×1920 30fps 슬라이드 영상 + 배경음.
    뒤는 같은 카드를 흐리게 깔고, 앞 카드는 천천히 살짝 다가온다. 장 사이는 0.25초 밀기 전환.
    opt: {"sec": 2.6, "first": 3.2, "last": 3.6, "bgm": 주소, "volume": 0.3, "transition": "slideleft", "crf": 23}"""
    opt = opt or {}
    sec, first, last = float(opt.get("sec", 2.6)), float(opt.get("first", 3.2)), float(opt.get("last", 3.6))
    tr = float(opt.get("transitionSec", 0.25))
    trans = opt.get("transition", "slideleft")
    crf = str(opt.get("crf", 23))
    segs, durs = [], []
    for k, img in enumerate(images):
        d = first if k == 0 else (last if k == len(images) - 1 else sec)
        d += tr if k < len(images) - 1 else 0
        seg = os.path.join(tmpdir, f"cr{k}.mp4")
        z = f"(1+0.035*t/{d:.3f})"
        fc = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:4,"
              "eq=brightness=-0.18:saturation=0.9,setsar=1[bg];"
              f"[0:v]scale=w='trunc(1000*{z}/2)*2':h='trunc(1250*{z}/2)*2':eval=frame[fg];"
              "[bg][fg]overlay=x='(W-w)/2':y='(H-h)/2-30':eval=frame,format=yuv420p[o]")
        run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30", "-t", f"{d:.3f}", "-i", img,
             "-filter_complex", fc, "-map", "[o]", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, seg])
        segs.append(seg)
        durs.append(d)
    silent = os.path.join(tmpdir, "cardreel_silent.mp4")
    if len(segs) == 1:
        shutil.copy(segs[0], silent)
        total = durs[0]
    else:
        ins = sum([["-i", p] for p in segs], [])
        fl, prev, off = [], "[0:v]", 0.0
        for k in range(1, len(segs)):
            off += durs[k - 1] - tr
            lab = f"[x{k}]" if k < len(segs) - 1 else "[o]"
            fl.append(f"{prev}[{k}:v]xfade=transition={trans}:duration={tr}:offset={off:.3f}{lab}")
            prev = lab
        total = off + durs[-1]
        run(["ffmpeg", "-y", "-loglevel", "error", *ins, "-filter_complex", ";".join(fl), "-map", "[o]",
             "-c:v", "libx264", "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p", "-r", "30", silent])
    bgm = opt.get("bgm")
    if bgm:
        a = download(bgm, os.path.join(tmpdir, "cr_bgm" + (os.path.splitext(bgm.split("?")[0])[1][:5] or ".mp3")))
        vol = float(opt.get("volume", 0.3))
        fc = (f"[1:a]aloop=loop=-1:size=2147483647,atrim=0:{total:.3f},asetpts=N/SR/TB,volume={vol},"
              f"afade=t=in:d=0.3,afade=t=out:st={max(total - 1.2, 0):.3f}:d=1.2[a]")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-i", a, "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-shortest", "-movflags", "+faststart", out_path])
    else:
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-c", "copy", "-movflags", "+faststart", out_path])
    return out_path, probe_duration(out_path)


# ---------------- 목소리(일레븐랩스) ----------------
def el_key():
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY 없음 — GitHub 저장소 Settings → Secrets에 등록 필요")
    return key


def el_voices():
    """대표 계정의 목소리 목록(이름·ID). Voice ID를 복사하지 않아도 이름으로 찾게."""
    req = urllib.request.Request("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": el_key(), "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"일레븐랩스 목소리 목록 오류 HTTP {e.code}: {scrub(e.read().decode(errors='replace')[:300])}") from None
    return [{"name": x.get("name"), "voice_id": x.get("voice_id"), "category": x.get("category")} for x in data.get("voices", [])]


def resolve_voice_id(v):
    if v.get("voice_id"):
        return v["voice_id"]
    want = (v.get("voice_name") or "").strip()
    if not want:
        raise RuntimeError("voice_id 또는 voice_name 필요")
    vs = el_voices()
    hit = [x for x in vs if (x["name"] or "").strip() == want] or [x for x in vs if want in (x["name"] or "")]
    if not hit:
        raise RuntimeError(f"'{want}' 목소리를 찾지 못함 — 있는 목소리: {[x['name'] for x in vs][:20]}")
    return hit[0]["voice_id"]


def make_voice(job, out_dir):
    key = el_key()
    v = job["voice"]
    vid = resolve_voice_id(v)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{vid}/with-timestamps?output_format=mp3_44100_128"
    models = v.get("model_ids") or [v.get("model_id") or "eleven_v4", "eleven_v3", "eleven_multilingual_v2"]
    res, errs, used = None, [], None
    for mid in models:
        body = {"text": v["text"], "model_id": mid}
        if v.get("settings"):
            body["voice_settings"] = v["settings"]
        if v.get("language_code"):
            body["language_code"] = v["language_code"]
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"xi-api-key": key, "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                res = json.loads(r.read().decode())
            used = mid
            break
        except urllib.error.HTTPError as e:
            msg = scrub(e.read().decode(errors="replace")[:300])
            errs.append(f"{mid}: HTTP {e.code} {msg}")
            if e.code not in (400, 404, 422):  # 모델 문제일 때만 다음 모델로(401·402·429는 바로 멈춤)
                break
    if res is None:
        raise RuntimeError("일레븐랩스 오류 — " + " | ".join(errs))
    mp3 = os.path.join(out_dir, "voice.mp3")
    open(mp3, "wb").write(base64.b64decode(res["audio_base64"]))
    al = res.get("alignment") or res.get("normalized_alignment") or {}
    chars = al.get("characters") or []
    starts = al.get("character_start_times_seconds") or []
    ends = al.get("character_end_times_seconds") or []
    # 어절(띄어쓰기) 단위 시작 시각 — 자막·장면 시간 잡기용
    words, cur, cs, last_e = [], "", None, 0.0
    for c, s, e in zip(chars, starts, ends):
        if c.isspace():
            if cur:
                words.append({"w": cur, "start": round(cs, 2), "end": round(last_e, 2)})
            cur, cs = "", None
            continue
        if cs is None:
            cs = s
        cur += c
        last_e = e
    if cur:
        words.append({"w": cur, "start": round(cs, 2), "end": round(last_e, 2)})
    return mp3, probe_duration(mp3), {"characters": chars, "starts": starts, "ends": ends}, words, used, vid


# ---------------- 이미지 보관함(bank) ----------------
SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def bank_images(job, tmpdir):
    """링크팜 등에서 만든 이미지(7일 뒤 사라지는 주소)를 bank/ 에 영구 보관한다. 35일 정리 대상이 아님.
    images: [{"name": "protein-kitchen-01", "url": "https://...", "tags": ["단백질", "부엌"], "prompt": "...", "replace": false}]
    → bank/<name>.jpg(원본 크기, 고화질 JPG) + bank/<name>_540.jpg(검수·카드 미리보기) + bank/index.json(전체 목록)"""
    bdir = os.path.join(WORK, "bank")
    os.makedirs(bdir, exist_ok=True)
    idx_path = os.path.join(bdir, "index.json")
    index = json.load(open(idx_path, encoding="utf-8")) if os.path.exists(idx_path) else {"items": {}}
    items = index.setdefault("items", {})
    got, warn = [], []
    for k, im in enumerate(job.get("images") or []):
        name = SAFE.sub("-", str(im.get("name") or f"{job['id']}-{k + 1:02d}")).strip("-")[:80]
        old = items.get(name) or {}
        ext = os.path.splitext(im["url"].split("?")[0])[1].lower()
        if im.get("kind") == "audio" or ext in (".mp3", ".m4a", ".wav", ".aac"):  # 배경음 등 소리는 그대로 보관
            path = os.path.join(bdir, name + (ext if ext in (".mp3", ".m4a", ".wav", ".aac") else ".mp3"))
            if os.path.exists(path) and not im.get("replace"):
                warn.append(f"{name}: 이미 있어 그대로 둠(replace:true면 덮어씀)")
            else:
                try:
                    download(im["url"], path)
                except Exception as e:  # noqa: BLE001
                    warn.append(f"{name}: 실패 {scrub(e)[:200]}")
                    continue
            rec = {**old, "name": name, "kind": "audio", "url": rel_url(path), "sec": round(probe_duration(path), 2),
                   "bytes": os.path.getsize(path), "tags": im.get("tags") or old.get("tags") or [],
                   "desc": im.get("desc") or old.get("desc") or "", "source": im.get("source") or old.get("source") or "",
                   "job": str(job["id"]), "at": old.get("at") or now()}
            items[name] = rec
            got.append(rec)
            continue
        full = os.path.join(bdir, f"{name}.jpg")
        thumb = os.path.join(bdir, f"{name}_540.jpg")
        if os.path.exists(full) and not im.get("replace"):
            warn.append(f"{name}: 이미 있어 그대로 둠(replace:true면 덮어씀)")
        else:
            try:
                src = download(im["url"], os.path.join(tmpdir, f"b{k}" + (os.path.splitext(im["url"].split("?")[0])[1][:5] or ".img")))
                run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-frames:v", "1", "-q:v", "2", full])
                run(["ffmpeg", "-y", "-loglevel", "error", "-i", full, "-vf", "scale=540:-2", "-q:v", "4", thumb])
            except Exception as e:  # noqa: BLE001
                warn.append(f"{name}: 실패 {scrub(e)[:200]}")
                continue
        w, h = (run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                     "-of", "csv=p=0", full]).strip().split(",") + ["0", "0"])[:2]
        rec = {**old, "name": name, "kind": "image", "url": rel_url(full), "thumb": rel_url(thumb),
               "w": int(w or 0), "h": int(h or 0), "bytes": os.path.getsize(full),
               "tags": im.get("tags") or old.get("tags") or [],
               "desc": im.get("desc") or old.get("desc") or "",
               "prompt": (im.get("prompt") or old.get("prompt") or "")[:500],
               "source": im.get("source") or old.get("source") or "",
               "job": str(job["id"]), "at": old.get("at") or now()}
        items[name] = rec
        got.append(rec)
    index["updatedAt"] = now()
    index["count"] = len(items)
    json.dump(index, open(idx_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return got, warn


# ---------------- 실행 ----------------
def rel_url(path):
    """링크팜·쓰레드에 주는 공개 주소(GitHub Pages). 작업 공간 확인용 raw 주소는 rawBase로 바꿔 쓴다."""
    return f"{PAGES}/{os.path.relpath(path, WORK).replace(os.sep, '/')}"


def process(job_path, br):
    job_dir = os.path.dirname(job_path)
    job = json.load(open(job_path, encoding="utf-8"))
    jid = str(job["id"])
    rev = job.get("rev", 1)
    res_path = os.path.join(WORK, "results", f"{jid}.json")
    old = json.load(open(res_path, encoding="utf-8")) if os.path.exists(res_path) else {}
    if old.get("rev") == rev and old.get("status") == "done":
        return None
    shutil.rmtree(os.path.join(WORK, "out", jid), ignore_errors=True)
    out_dir = os.path.join(WORK, "out", jid, f"r{rev}")  # rev마다 새 주소(캐시에 옛 파일이 남지 않게)
    os.makedirs(out_dir, exist_ok=True)
    res = {"id": jid, "rev": rev, "kind": job.get("kind", "render"), "status": "running", "startedAt": now(), "warn": [],
           "pagesBase": PAGES, "rawBase": RAW}
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if job.get("kind") == "voice":
                mp3, sec, al, words, used, vid = make_voice(job, out_dir)
                res["voice"] = {"url": rel_url(mp3), "sec": round(sec, 2), "model": used, "voice_id": vid,
                                "words": words, "alignment": al}
            elif job.get("kind") == "voices":
                res["voices"] = el_voices()
            elif job.get("kind") == "cardreel":  # 이미 만든 카드 이미지 주소로 슬라이드 릴스만
                imgs = [download(u, os.path.join(tmp, f"cri{k}" + (os.path.splitext(u.split("?")[0])[1][:5] or ".png")))
                        for k, u in enumerate(job.get("images") or [])]
                if not imgs:
                    raise RuntimeError("images(카드 이미지 주소) 필요")
                cr, crsec = card_reel(imgs, os.path.join(out_dir, "card_reel.mp4"), tmp, job.get("cardReel") or {})
                res["cardReel"] = {"url": rel_url(cr), "sec": round(crsec, 2), "bytes": os.path.getsize(cr),
                                   "sheet": rel_url(video_sheet(cr, crsec, os.path.join(out_dir, "card_reel_sheet.jpg"), tmp))}
            elif job.get("kind") == "bank":
                got, w = bank_images(job, tmp)
                res["bank"] = got
                res["bankIndex"] = f"{PAGES}/bank/index.json"
                res["warn"] += w
                if not got:
                    raise RuntimeError("보관된 이미지 없음 — " + " | ".join(w)[:600])
            else:
                if job.get("cards"):
                    cards, thumbs, sheet, w = render_cards(br, job, job_dir, out_dir, tmp)
                    res["cards"] = [rel_url(p) for p in cards]
                    res["thumbs"] = [rel_url(p) for p in thumbs]
                    res["cardsSheet"] = rel_url(sheet) if sheet else None
                    res["warn"] += w
                if job.get("cards") and job.get("cardReel"):
                    cr, crsec = card_reel(cards, os.path.join(out_dir, "card_reel.mp4"), tmp, job["cardReel"])
                    res["cardReel"] = {"url": rel_url(cr), "sec": round(crsec, 2), "bytes": os.path.getsize(cr),
                                       "sheet": rel_url(video_sheet(cr, crsec, os.path.join(out_dir, "card_reel_sheet.jpg"), tmp))}
                if job.get("reel"):
                    mp4, sheet, dur, w, rsec = render_reel(br, job, job_dir, out_dir, tmp)
                    res["reel"] = {"url": rel_url(mp4), "sec": round(dur, 2), "bytes": os.path.getsize(mp4),
                                   "sheet": rel_url(sheet), "renderSec": rsec}
                    res["warn"] += w
        res["status"] = "done"
    except Exception as e:  # noqa: BLE001
        res["status"] = "error"
        res["error"] = scrub(f"{e}\n{traceback.format_exc()[-1200:]}")
    res["doneAt"] = now()
    res["sec"] = round(time.time() - t0)
    os.makedirs(os.path.dirname(res_path), exist_ok=True)
    json.dump(res, open(res_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"{jid} rev{rev}: {res['status']} ({res['sec']}s) {res.get('error', '')[:300]}")
    return res


def prune(days=35):
    """35일 넘은 out/ 폴더는 지운다(media 브랜치는 매번 한 커밋으로 덮어써서 저장소가 커지지 않게).
    bank/(이미지 보관함)는 지우지 않는다."""
    cut = dt.datetime.now(KST) - dt.timedelta(days=days)
    for d in glob.glob(os.path.join(WORK, "out", "*")):
        r = os.path.join(WORK, "results", os.path.basename(d) + ".json")
        try:
            t = dt.datetime.fromisoformat(json.load(open(r, encoding="utf-8"))["doneAt"])
        except Exception:  # noqa: BLE001  (결과 기록이 없으면 지우지 않는다)
            continue
        if t < cut:
            shutil.rmtree(d, ignore_errors=True)


def main():
    only = sys.argv[1:]  # 특정 id만
    paths = sorted(glob.glob(os.path.join(JOBS, "*", "job.json")) + glob.glob(os.path.join(JOBS, "*.json")))
    br = None
    done = 0
    for p in paths:
        try:
            job = json.load(open(p, encoding="utf-8"))
            jid = str(job.get("id"))
        except Exception as e:  # noqa: BLE001
            log(f"skip {p}: {e}")
            continue
        if only and jid not in only:
            continue
        if br is None and job.get("kind", "render") == "render":
            br = Browser()
        if process(p, br):
            done += 1
    if br:
        br.close()
    prune()
    log(f"끝: 새로 만든 job {done}개")


if __name__ == "__main__":
    main()
