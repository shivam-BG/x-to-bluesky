#!/usr/bin/env python3
"""
Crosspost posts from X (Twitter) to Bluesky.

Handles: plain text, images (+ alt text), videos/GIFs, self-reply threads,
long posts (split into a Bluesky thread), links and hashtags.

Quote tweets are posted the way people usually do it by hand on Bluesky: your
text, plus a screenshot-style image of the quoted tweet (avatar, name, text,
photos, date), cropped tight with no bars. The image is drawn from the tweet's
own data with a headless browser (Playwright). If that ever fails, it falls
back to a text pointer with a link, so nothing is lost.

Setup:
    pip install -U atproto requests pillow playwright
    python -m playwright install --with-deps chromium
    export X_USERNAME="..."             # your X handle, without the @
    export BSKY_HANDLE="..."            # your full Bluesky handle
    export X_BEARER_TOKEN="..."         # from the X Developer Console
    export BSKY_APP_PASSWORD="..."      # Bluesky > Settings > Privacy and security > App passwords

Usage:
    python crosspost.py                 # first run: just records your latest tweet, posts nothing
    python crosspost.py --backfill 3    # first run: also post your 3 most recent tweets
    python crosspost.py --dry-run       # show what would be posted, change nothing
    python crosspost.py --loop 30       # keep checking every 30 seconds for 55 minutes

Optional env vars: STATE_FILE, CROSSPOST_RETWEETS=1, TIMEZONE (for the date on
screenshots, default America/Chicago), CROSSPOST_GIT_PUSH=1 (GitHub Actions only),
CHROMIUM_PATH.
"""
import argparse
import base64
import html
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from atproto import Client, models
from PIL import Image, ImageOps

X_USERNAME = os.environ["X_USERNAME"]
BSKY_HANDLE = os.environ["BSKY_HANDLE"]
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
CROSSPOST_RETWEETS = os.environ.get("CROSSPOST_RETWEETS", "0") == "1"
TIMEZONE = os.environ.get("TIMEZONE", "America/Chicago")

X_API = "https://api.x.com/2"
TEXT_LIMIT = 295          # Bluesky max is 300 graphemes; keep some headroom
IMG_LIMIT = 950_000       # Bluesky image blob limit is ~976 KB
MAX_FAILURES = 3          # give up on a tweet after this many failed attempts

TWEET_FIELDS = "created_at,entities,referenced_tweets,in_reply_to_user_id,note_tweet,attachments,author_id,lang"
MEDIA_FIELDS = "type,url,alt_text,variants,width,height,preview_image_url"
USER_FIELDS = "username,name,profile_image_url,verified,verified_type"
EXPANSIONS = ("attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id,"
              "referenced_tweets.id.attachments.media_keys")

URL_RE = re.compile(r"https?://\S+")
TAG_RE = re.compile(r"(?<![\w/])#(\w+)")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"user_id": None, "last_id": None, "posted": {}, "failures": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def git_push_state():
    """On GitHub Actions: commit state.json right after each post, so a crashed
    runner can never cause the same tweet to be posted twice."""
    if os.environ.get("CROSSPOST_GIT_PUSH") != "1":
        return

    def run(*cmd):
        return subprocess.run(cmd, capture_output=True, text=True)

    run("git", "config", "user.name", "crosspost-bot")
    run("git", "config", "user.email", "crosspost-bot@users.noreply.github.com")
    for _ in range(3):
        run("git", "fetch", "-q", "origin", "main")
        run("git", "reset", "-q", "origin/main")   # newest remote commit, our files stay on disk
        run("git", "add", str(STATE_FILE))
        if run("git", "diff", "--cached", "--quiet").returncode == 0:
            return
        run("git", "commit", "-q", "-m", "update state")
        if run("git", "push", "origin", "HEAD:main").returncode == 0:
            return
        time.sleep(3)
    print("warning: could not push state.json", file=sys.stderr)


# --------------------------------------------------------------------------
# X API
# --------------------------------------------------------------------------
class Ctx:
    """Everything the X API returned in `includes`, indexed by id."""

    def __init__(self):
        self.media, self.tweets, self.users = {}, {}, {}

    def username(self, user_id):
        return self.users.get(user_id, {}).get("username", "i")


def x_get(path, params):
    token = os.environ["X_BEARER_TOKEN"]
    r = requests.get(
        X_API + path,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"X API {r.status_code}: {r.text[:300]}")
    return r.json()


def get_user_id(username):
    return x_get(f"/users/by/username/{username}", {})["data"]["id"]


def fetch(user_id, since_id=None, max_results=100, pages=5):
    """Return (tweets oldest-first, Ctx)."""
    params = {
        "max_results": max(5, min(100, max_results)),
        "tweet.fields": TWEET_FIELDS,
        "expansions": EXPANSIONS,
        "media.fields": MEDIA_FIELDS,
        "user.fields": USER_FIELDS,
    }
    if since_id:
        params["since_id"] = since_id

    tweets, ctx, token = [], Ctx(), None
    for _ in range(pages):
        p = dict(params)
        if token:
            p["pagination_token"] = token
        data = x_get(f"/users/{user_id}/tweets", p)
        tweets += data.get("data", [])
        inc = data.get("includes", {})
        for m in inc.get("media", []):
            ctx.media[m["media_key"]] = m
        for x in inc.get("tweets", []):
            ctx.tweets[x["id"]] = x
        for u in inc.get("users", []):
            ctx.users[u["id"]] = u
        token = data.get("meta", {}).get("next_token")
        if not token:
            break

    tweets.sort(key=lambda t: int(t["id"]))
    return tweets, ctx


# --------------------------------------------------------------------------
# Text handling
# --------------------------------------------------------------------------
def tweet_url(tid):
    return f"https://x.com/{X_USERNAME}/status/{tid}"


def clean_text(t, strip_mentions=False, quoted_id=None):
    """Expand t.co links, drop media/quote-tweet links, unescape HTML."""
    note = t.get("note_tweet")  # long-form posts keep their full text here
    text = note["text"] if note else t["text"]
    ents = (note or t).get("entities", {})

    for u in ents.get("urls", []):
        expanded = u.get("expanded_url") or u["url"]
        is_media = "media_key" in u or re.search(r"/(photo|video)/\d+$", expanded)
        is_quote = quoted_id and re.search(rf"/status/{quoted_id}\b", expanded)
        text = text.replace(u["url"], "" if (is_media or is_quote) else expanded)

    if strip_mentions:  # self-replies start with "@yourname "
        text = re.sub(r"^(@\w+\s+)+", "", text)

    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text, limit=TEXT_LIMIT):
    """Split long text into <=limit chunks on newlines/spaces.
    (Bluesky shows its own 1/2, 2/2 labels on self-threads.)"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks


def build_facets(text):
    """Clickable links and hashtags (Bluesky wants UTF-8 byte offsets)."""
    facets = []

    def span(start, end):
        return len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8"))

    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]}\"'")
        s, e = span(m.start(), m.start() + len(url))
        facets.append(models.AppBskyRichtextFacet.Main(
            index=models.AppBskyRichtextFacet.ByteSlice(byte_start=s, byte_end=e),
            features=[models.AppBskyRichtextFacet.Link(uri=url)],
        ))
    for m in TAG_RE.finditer(text):
        if m.group(1).isdigit():
            continue
        s, e = span(m.start(), m.end())
        facets.append(models.AppBskyRichtextFacet.Main(
            index=models.AppBskyRichtextFacet.ByteSlice(byte_start=s, byte_end=e),
            features=[models.AppBskyRichtextFacet.Tag(tag=m.group(1))],
        ))
    return facets or None


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------
def shrink_image(data, limit=IMG_LIMIT):
    """Return (bytes, width, height), recompressing only if over Bluesky's limit."""
    img = Image.open(io.BytesIO(data))
    if len(data) <= limit:
        return data, img.width, img.height

    img = ImageOps.exif_transpose(img).convert("RGB")
    scale = 1.0
    while True:
        cur = img if scale == 1.0 else img.resize(
            (int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        for quality in (92, 85, 75, 65):
            buf = io.BytesIO()
            cur.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= limit:
                return buf.getvalue(), cur.width, cur.height
        scale *= 0.85


def best_video_url(media):
    mp4s = [v for v in media.get("variants", []) if v.get("content_type") == "video/mp4"]
    if not mp4s:
        return None
    return max(mp4s, key=lambda v: v.get("bit_rate", 0))["url"]


# --------------------------------------------------------------------------
# Quote tweets: draw the quoted tweet as a screenshot-style card
# --------------------------------------------------------------------------
CARD_W = 600        # CSS pixels; the image is rendered at 2x (1200 px wide)
MAX_RATIO = 1.9     # widest card allowed (width/height). Bluesky letterboxes very
                    # wide images with dark bars, so short tweets get a little
                    # extra white space instead.

CARD_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#fff;width:600px}
#card{width:600px;background:#fff;color:#0f1419;padding:32px;display:flex;flex-direction:column;
  justify-content:center;-webkit-font-smoothing:antialiased;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Helvetica,Arial,
  "Liberation Sans","Noto Sans","Noto Color Emoji",sans-serif}
.head{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.av{width:56px;height:56px;border-radius:50%;object-fit:cover;background:#cfd9de;flex:none}
.who{min-width:0}
.name{display:flex;align-items:center;gap:6px;font-weight:700;font-size:22px;line-height:1.25}
.name span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name svg{flex:none}
.handle{color:#536471;font-size:19px;line-height:1.3}
.body{font-size:25px;line-height:1.38;white-space:pre-wrap;overflow-wrap:anywhere}
.l{color:#1d9bf0}
.media{display:grid;gap:4px;margin-top:18px;border-radius:18px;overflow:hidden;
  border:1px solid #eff3f4;grid-template-columns:1fr}
.m2,.m3,.m4{grid-template-columns:1fr 1fr}
.m3,.m4{grid-template-rows:1fr 1fr}
.m3 .c:first-child{grid-row:span 2}
.c{position:relative;overflow:hidden;min-height:0;background:#eff3f4}
.c img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.play{position:absolute;left:50%;top:50%;width:84px;height:84px;margin:-42px 0 0 -42px;
  border-radius:50%;background:rgba(15,20,25,.75)}
.play::after{content:"";position:absolute;left:34px;top:26px;border-left:28px solid #fff;
  border-top:16px solid transparent;border-bottom:16px solid transparent}
.date{color:#536471;font-size:19px;margin-top:18px}
"""

CHECK_SVG = ('<svg viewBox="0 0 24 24" width="22" height="22"><circle cx="12" cy="12" r="11" fill="#1d9bf0"/>'
             '<path d="M7 12.5l3.2 3.2L17 9" stroke="#fff" stroke-width="2.4" fill="none" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')

# links are marked with private-use characters so they can be coloured later
TOKEN_RE = re.compile(r"([^]*(?:|$)|(?<![\w])@\w{1,15}|(?<![\w/&])#\w*[^\W\d_]\w*)")


def make_card(q, ctx):
    media = [ctx.media[k] for k in q.get("attachments", {}).get("media_keys", []) if k in ctx.media]
    return {"tweet": q, "author": ctx.users.get(q.get("author_id")), "media": media}


def to_data_uri(url):
    """Download an image and return (data URI, width, height), or (None, 0, 0)."""
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        mime = Image.MIME.get(img.format, "image/jpeg")
        return f"data:{mime};base64," + base64.b64encode(r.content).decode(), img.width, img.height
    except Exception:
        return None, 0, 0


def card_text(t):
    """Return (text with link markers, plain text) for the quoted tweet."""
    note = t.get("note_tweet")
    text = note["text"] if note else t["text"]
    ents = (note or t).get("entities", {})
    inner = {r["id"] for r in t.get("referenced_tweets", []) if r["type"] == "quoted"}
    plain = text
    for u in ents.get("urls", []):
        expanded = u.get("expanded_url") or u["url"]
        m = re.search(r"/status/(\d+)", expanded)
        drop = ("media_key" in u or re.search(r"/(photo|video)/\d+$", expanded)
                or (m and m.group(1) in inner))
        shown = u.get("display_url") or expanded
        text = text.replace(u["url"], "" if drop else f"{shown}")
        plain = plain.replace(u["url"], "" if drop else expanded)
    text = re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()
    plain = re.sub(r"\n{3,}", "\n\n", html.unescape(plain)).strip()
    if len(text) > 700:
        text = text[:699].rstrip() + "…"
    return text, plain


def html_body(text):
    out = []
    for i, part in enumerate(TOKEN_RE.split(text)):
        if not part:
            continue
        if i % 2:  # a link, @mention or #hashtag
            out.append(f'<span class="l">{html.escape(part.strip(chr(0xe000) + chr(0xe001)))}</span>')
        else:
            out.append(html.escape(part))
    return "".join(out)


def fmt_date(iso):
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE))
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except Exception:
        return ""


def build_card_html(card):
    """Return (html, alt_text) for a quoted tweet."""
    q, a = card["tweet"], card["author"] or {}
    name = a.get("name") or a.get("username") or "X user"
    handle = f"@{a['username']}" if a.get("username") else ""
    verified = a.get("verified") or a.get("verified_type") in ("blue", "business", "government")

    avatar = ""
    if a.get("profile_image_url"):
        uri = to_data_uri(a["profile_image_url"].replace("_normal.", "_400x400."))[0]
        if uri:
            avatar = f'<img class="av" src="{uri}">'
    if not avatar:
        avatar = '<div class="av"></div>'

    tiles = []
    for m in card["media"][:4]:
        url = m["url"] + "?name=medium" if m["type"] == "photo" else m.get("preview_image_url")
        if not url:
            continue
        uri, w, h = to_data_uri(url)
        if uri:
            tiles.append((uri, m.get("width") or w, m.get("height") or h, m["type"] != "photo"))
    media_html = ""
    if tiles:
        if len(tiles) == 1:
            w, h = tiles[0][1], tiles[0][2]
            ratio = min(2.0, max(0.75, w / h)) if w and h else 1.78
            style = f"aspect-ratio:{ratio:.3f}"
        else:
            style = "aspect-ratio:16/9"
        cells = "".join(
            f'<div class="c"><img src="{u}">{"<div class=play></div>" if v else ""}</div>'
            for u, _, _, v in tiles)
        media_html = f'<div class="media m{len(tiles)}" style="{style}">{cells}</div>'

    marked, plain = card_text(q)
    body = f'<div class="body">{html_body(marked)}</div>' if marked else ""
    date = fmt_date(q.get("created_at", ""))
    page = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{CARD_CSS}</style></head><body>'
        f'<div id="card"><div class="head">{avatar}<div class="who">'
        f'<div class="name"><span>{html.escape(name)}</span>{CHECK_SVG if verified else ""}</div>'
        f'<div class="handle">{html.escape(handle)}</div></div></div>'
        f'{body}{media_html}<div class="date">{html.escape(date)}</div></div></body></html>'
    )
    alt = f"Screenshot of a post by {name} ({handle}) on X"
    alt += f", {date}" if date else ""
    alt += f": {plain}" if plain else "."
    return page, alt[:1900]


def render_card(card):
    """Draw the quoted tweet as a tight PNG. Returns (png_bytes, alt_text)."""
    from playwright.sync_api import sync_playwright  # only needed for quote tweets

    page_html, alt = build_card_html(card)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        try:
            page = browser.new_page(viewport={"width": CARD_W, "height": 900}, device_scale_factor=2)
            page.set_content(page_html, wait_until="load")
            page.evaluate("document.fonts.ready.then(() => true)")
            height = page.evaluate("document.getElementById('card').getBoundingClientRect().height")
            min_h = math.ceil(CARD_W / MAX_RATIO)
            if height < min_h:  # keep short tweets from becoming a thin strip
                page.evaluate(f"document.getElementById('card').style.minHeight = '{min_h}px'")
            png = page.locator("#card").screenshot(type="png")
        finally:
            browser.close()
    return png, alt


def quote_pointer(q, ctx):
    """Text fallback for a quoted tweet (used only if the screenshot can't be made)."""
    qa = ctx.username(q.get("author_id"))
    snippet = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", clean_text(q))).strip()
    if len(snippet) > 120:
        snippet = snippet[:119] + "…"
    return f"↪ Quoting @{qa}: “{snippet}”\nhttps://x.com/{qa}/status/{q['id']}"


# --------------------------------------------------------------------------
# Deciding what to post
# --------------------------------------------------------------------------
def plan(t, ctx, state):
    """Return (plan_dict, None) or (None, reason_to_skip)."""
    tid = t["id"]
    refs = {r["type"]: r["id"] for r in t.get("referenced_tweets", [])}
    lang = t.get("lang")
    lang = lang if lang and re.fullmatch(r"[a-z]{2}", lang) else None

    # Retweets: just the screenshot of the original
    if "retweeted" in refs:
        if not CROSSPOST_RETWEETS:
            return None, "retweet"
        orig = ctx.tweets.get(refs["retweeted"])
        if not orig:
            return None, "retweeted tweet unavailable"
        return dict(text="", media=[], card=make_card(orig, ctx), pointer=quote_pointer(orig, ctx),
                    parent=None, lang=None, url=tweet_url(tid)), None

    # Replies: only keep self-replies (threads) whose parent we crossposted
    parent = None
    if "replied_to" in refs:
        if t.get("in_reply_to_user_id") != state["user_id"]:
            return None, "reply to someone else"
        parent = state["posted"].get(refs["replied_to"])
        if not parent:
            return None, "reply to a tweet that wasn't crossposted"

    quoted_id = refs.get("quoted")
    text = clean_text(t, strip_mentions=parent is not None, quoted_id=quoted_id)
    media = [ctx.media[k] for k in t.get("attachments", {}).get("media_keys", []) if k in ctx.media]
    photos = [m for m in media if m["type"] == "photo"]
    has_video = len(photos) != len(media)

    # Quote tweets: your text + a screenshot of the quoted tweet as an image.
    # (A video can't be mixed with images, and Bluesky allows 4 images, so those
    # rare cases get a text pointer + link instead.)
    card, pointer = None, ""
    if quoted_id:
        q = ctx.tweets.get(quoted_id)
        pointer = quote_pointer(q, ctx) if q else f"↪ https://x.com/i/status/{quoted_id}"
        if q and not has_video and len(photos) < 4:
            card = make_card(q, ctx)
        else:
            text = f"{text}\n\n{pointer}".strip()
            pointer = ""

    if not text and not media and not card:
        return None, "nothing to post"
    return dict(text=text, media=media, card=card, pointer=pointer, parent=parent, lang=lang,
                url=tweet_url(tid)), None


# --------------------------------------------------------------------------
# Posting to Bluesky
# --------------------------------------------------------------------------
def login():
    client = Client()
    client.login(BSKY_HANDLE, os.environ["BSKY_APP_PASSWORD"])
    return client


def strong_ref(uri, cid):
    return models.ComAtprotoRepoStrongRef.Main(uri=uri, cid=cid)


def create_post(client, text, facets, embed, reply, langs):
    record = models.AppBskyFeedPost.Record(
        text=text, facets=facets, embed=embed, reply=reply, langs=langs,
        created_at=client.get_current_time_iso(),
    )
    resp = client.com.atproto.repo.create_record(models.ComAtprotoRepoCreateRecord.Data(
        repo=client.me.did, collection=models.ids.AppBskyFeedPost, record=record))
    return resp.uri, resp.cid


def upload_image(client, data, alt):
    data, w, h = shrink_image(data)
    blob = client.upload_blob(data).blob
    return models.AppBskyEmbedImages.Image(
        alt=(alt or "")[:1900],
        image=blob,
        aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(width=w, height=h),
    )


def build_embed(client, p, card_img):
    """Returns (embed, video_media_or_None). card_img is (png_bytes, alt) or None."""
    photos = [m for m in p["media"] if m["type"] == "photo"]
    video = next((m for m in p["media"] if m["type"] != "photo"), None)

    items = []
    for m in photos[: 3 if card_img else 4]:
        raw = requests.get(m["url"] + "?name=large", timeout=30)
        raw.raise_for_status()
        items.append(upload_image(client, raw.content, m.get("alt_text")))
    if card_img:
        items.append(upload_image(client, card_img[0], card_img[1]))
    return (models.AppBskyEmbedImages.Main(images=items) if items else None), video


def post_video(client, text, facets, video, reply, langs):
    url = best_video_url(video)
    if not url:
        raise RuntimeError("no mp4 variant found")
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    kwargs = {}
    if video.get("width") and video.get("height"):
        kwargs["video_aspect_ratio"] = models.AppBskyEmbedDefs.AspectRatio(
            width=video["width"], height=video["height"])
    resp = client.send_video(
        text=text, video=r.content, video_alt=video.get("alt_text") or "",
        reply_to=reply, langs=langs, facets=facets, **kwargs)
    return resp.uri, resp.cid


def publish(client, p, state):
    """Post one tweet (as one or more Bluesky posts). Returns the state entry."""
    card_img = None
    if p.get("card"):
        try:
            card_img = render_card(p["card"])
        except Exception as e:  # never lose the post just because the screenshot failed
            print(f"  screenshot failed ({e}); using a text pointer instead", file=sys.stderr)
            p["text"] = f"{p['text']}\n\n{p['pointer']}".strip() if p.get("pointer") else p["text"]

    embed, video = build_embed(client, p, card_img)
    langs = [p["lang"]] if p["lang"] else None
    parent = p["parent"]

    reply = models.AppBskyFeedPost.ReplyRef(
        root=strong_ref(parent["root_uri"], parent["root_cid"]),
        parent=strong_ref(parent["uri"], parent["cid"]),
    ) if parent else None
    root = (parent["root_uri"], parent["root_cid"]) if parent else None

    first = last = None
    for i, chunk in enumerate(split_text(p["text"])):
        facets = build_facets(chunk)
        if i == 0 and video:
            try:
                ref = post_video(client, chunk, facets, video, reply, langs)
            except Exception as e:  # fall back to a link so nothing is silently lost
                print(f"  video upload failed ({e}); linking to the original instead")
                chunk = (chunk + f"\n\n🎥 Video: {p['url']}").strip()
                ref = create_post(client, chunk, build_facets(chunk), None, reply, langs)
        else:
            ref = create_post(client, chunk, facets, embed if i == 0 else None, reply, langs)

        first = first or ref
        root = root or ref
        last = ref
        reply = models.AppBskyFeedPost.ReplyRef(root=strong_ref(*root), parent=strong_ref(*ref))

    return {
        "uri": last[0], "cid": last[1],
        "first_uri": first[0], "first_cid": first[1],
        "root_uri": root[0], "root_cid": root[1],
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def process(args, state, session):
    """One poll of X. Returns True if a tweet failed to post."""

    def commit():
        if not args.dry_run:
            save_state(state)

    if not state["user_id"]:
        state["user_id"] = get_user_id(X_USERNAME)
        commit()

    if not state["last_id"]:
        tweets, ctx = fetch(state["user_id"], None, max(5, args.backfill), pages=1)
        if args.backfill == 0:
            if tweets:
                state["last_id"] = str(max(int(t["id"]) for t in tweets))
            commit()
            print("Initialised. Only tweets posted from now on will be crossposted.")
            return False
        tweets = tweets[-args.backfill:]
    else:
        tweets, ctx = fetch(state["user_id"], state["last_id"])

    if not tweets:
        return False

    failed = False
    for t in tweets:
        tid = t["id"]
        if tid in state["posted"]:
            state["last_id"] = tid
            commit()
            continue

        p, reason = plan(t, ctx, state)
        if p is None:
            print(f"skip {tid}: {reason}")
            state["last_id"] = tid
            commit()
            continue

        if args.dry_run:
            kind = ("quote " if p["card"] else "") + ("reply " if p["parent"] else "") + "post"
            print(f"\n[dry run] {tid} -> {kind}, {len(p['media'])} media\n{p['text']}")
            continue

        if "client" not in session:
            session["client"] = login()
        try:
            state["posted"][tid] = publish(session["client"], p, state)
            print(f"posted {tid}")
        except Exception as e:
            failed = True
            n = state["failures"].get(tid, 0) + 1
            state["failures"][tid] = n
            print(f"FAILED {tid} (attempt {n}/{MAX_FAILURES}): {e}", file=sys.stderr)
            if n < MAX_FAILURES:
                save_state(state)
                break  # retry next poll, keeping order
            print(f"giving up on {tid}", file=sys.stderr)

        state["last_id"] = tid
        commit()
        git_push_state()
        time.sleep(1)

    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be posted; change nothing")
    ap.add_argument("--backfill", type=int, default=0,
                    help="on the very first run, also post this many recent tweets")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="keep checking every SECONDS instead of running once")
    ap.add_argument("--duration", type=int, default=55, metavar="MINUTES",
                    help="with --loop, stop after this many minutes (default 55)")
    args = ap.parse_args()

    state = load_state()
    session = {}
    deadline = time.time() + args.duration * 60
    failed, ok_polls = False, 0

    while True:
        try:
            failed = process(args, state, session)
            ok_polls += 1
        except Exception as e:
            if not args.loop:
                raise
            failed = True  # a network blip shouldn't kill a long-running loop
            print(f"poll error: {e}", file=sys.stderr)

        if not args.loop or time.time() + args.loop >= deadline:
            break
        time.sleep(max(args.loop, 120) if failed else args.loop)

    # Exit code decides whether GitHub emails you. In loop mode, a single hiccup
    # is fine; only fail if not one check worked (bad token, no credits, ...).
    if (args.loop and ok_polls == 0) or (not args.loop and failed):
        sys.exit(1)


if __name__ == "__main__":
    main()
