#!/usr/bin/env python3
"""
Crosspost tweets from X (Twitter) to Bluesky.

Handles: plain text, images (+ alt text), videos/GIFs, self-reply threads,
quote tweets, long tweets (split into a Bluesky thread), links and hashtags.

Setup:
    pip install -U atproto requests pillow
    export X_BEARER_TOKEN="..."         # from the X Developer Console
    export BSKY_APP_PASSWORD="..."      # Bluesky > Settings > Privacy and security > App passwords

Usage:
    python crosspost.py                 # first run: just records your latest tweet, posts nothing
    python crosspost.py --backfill 3    # first run: also post your 3 most recent tweets
    python crosspost.py --dry-run       # show what would be posted, change nothing
    (then run it on a schedule, e.g. cron every 10 minutes)

Optional env vars: X_USERNAME, BSKY_HANDLE, STATE_FILE, CROSSPOST_RETWEETS=1
"""
import argparse
import html
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from atproto import Client, models
from PIL import Image, ImageOps

X_USERNAME = os.environ.get("X_USERNAME", "beshearstan")
BSKY_HANDLE = os.environ.get("BSKY_HANDLE", "beshearstan.bsky.social")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
CROSSPOST_RETWEETS = os.environ.get("CROSSPOST_RETWEETS", "0") == "1"

X_API = "https://api.x.com/2"
TEXT_LIMIT = 295          # Bluesky max is 300 graphemes; keep some headroom
IMG_LIMIT = 950_000       # Bluesky image blob limit is ~976 KB
MAX_FAILURES = 3          # give up on a tweet after this many failed attempts

TWEET_FIELDS = "created_at,entities,referenced_tweets,in_reply_to_user_id,note_tweet,attachments,author_id,lang"
MEDIA_FIELDS = "type,url,alt_text,variants,width,height,preview_image_url"

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
        "expansions": "attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id",
        "media.fields": MEDIA_FIELDS,
        "user.fields": "username",
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
    """Split long text into <=limit chunks on newlines/spaces."""
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
# Deciding what to post
# --------------------------------------------------------------------------
def plan(t, ctx, state):
    """Return (plan_dict, None) or (None, reason_to_skip)."""
    tid = t["id"]
    refs = {r["type"]: r["id"] for r in t.get("referenced_tweets", [])}
    lang = t.get("lang")
    lang = lang if lang and re.fullmatch(r"[a-z]{2}", lang) else None

    # Retweets
    if "retweeted" in refs:
        if not CROSSPOST_RETWEETS:
            return None, "retweet"
        orig = ctx.tweets.get(refs["retweeted"])
        if not orig:
            return None, "retweeted tweet unavailable"
        author = ctx.username(orig.get("author_id"))
        text = f"🔁 @{author}:\n\n{clean_text(orig)}\n\nhttps://x.com/{author}/status/{orig['id']}"
        return dict(text=text, media=[], quote_id=None, parent=None, lang=None, url=tweet_url(tid)), None

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
    has_video = any(m["type"] != "photo" for m in media)

    # Quote tweets: real Bluesky quote if we crossposted the original,
    # otherwise a text pointer + link to the original tweet.
    quote_id = None
    if quoted_id:
        if quoted_id in state["posted"] and not has_video:
            quote_id = quoted_id
        else:
            q = ctx.tweets.get(quoted_id)
            if q:
                qa = ctx.username(q.get("author_id"))
                snippet = re.sub(r"https?://\S+", "", clean_text(q))
                snippet = re.sub(r"\s+", " ", snippet).strip()
                if len(snippet) > 120:
                    snippet = snippet[:119] + "…"
                text += f"\n\n↪ Quoting @{qa}: “{snippet}”\nhttps://x.com/{qa}/status/{quoted_id}"
            else:
                text += f"\n\n↪ Quoting: https://x.com/i/status/{quoted_id}"
        text = text.strip()

    if not text and not media and not quote_id:
        return None, "nothing to post"
    return dict(text=text, media=media, quote_id=quote_id, parent=parent, lang=lang, url=tweet_url(tid)), None


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


def build_embed(client, p):
    """Returns (embed, video_media_or_None)."""
    images = [m for m in p["media"] if m["type"] == "photo"]
    video = next((m for m in p["media"] if m["type"] != "photo"), None)

    images_embed = None
    if images:
        items = []
        for m in images[:4]:
            raw = requests.get(m["url"] + "?name=large", timeout=30)
            raw.raise_for_status()
            data, w, h = shrink_image(raw.content)
            blob = client.upload_blob(data).blob
            items.append(models.AppBskyEmbedImages.Image(
                alt=(m.get("alt_text") or "")[:1900],
                image=blob,
                aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(width=w, height=h),
            ))
        images_embed = models.AppBskyEmbedImages.Main(images=items)

    quote_embed = None
    if p["quote_id"]:
        q = p["quote_state"]
        quote_embed = models.AppBskyEmbedRecord.Main(record=strong_ref(q["first_uri"], q["first_cid"]))

    if quote_embed and images_embed:
        return models.AppBskyEmbedRecordWithMedia.Main(record=quote_embed, media=images_embed), video
    return quote_embed or images_embed, video


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
    if p["quote_id"]:
        p["quote_state"] = state["posted"][p["quote_id"]]
    embed, video = build_embed(client, p)
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
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be posted; change nothing")
    ap.add_argument("--backfill", type=int, default=0,
                    help="on the very first run, also post this many recent tweets")
    args = ap.parse_args()

    state = load_state()

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
            return
        tweets = tweets[-args.backfill:]
    else:
        tweets, ctx = fetch(state["user_id"], state["last_id"])

    if not tweets:
        print("No new tweets.")
        return

    client = None
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
            kind = ("quote " if p["quote_id"] else "") + ("reply " if p["parent"] else "") + "post"
            print(f"\n[dry run] {tid} -> {kind}, {len(p['media'])} media\n{p['text']}")
            continue

        client = client or login()
        try:
            state["posted"][tid] = publish(client, p, state)
            print(f"posted {tid}")
        except Exception as e:
            failed = True
            n = state["failures"].get(tid, 0) + 1
            state["failures"][tid] = n
            print(f"FAILED {tid} (attempt {n}/{MAX_FAILURES}): {e}", file=sys.stderr)
            if n < MAX_FAILURES:
                save_state(state)
                break  # retry next run, keeping order
            print(f"giving up on {tid}", file=sys.stderr)

        state["last_id"] = tid
        commit()
        time.sleep(1)

    if failed:
        sys.exit(1)  # makes GitHub Actions email you about the failure


if __name__ == "__main__":
    main()
