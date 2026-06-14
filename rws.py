import time
import random
import re
import os
import hashlib
import requests
import subprocess
import json
import pytz
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════════
# SOURCES — Reddit creators & subreddits (Twitter sources gone)
# ═══════════════════════════════════════════════════════════
def _env_list(key, default=""):
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        val = default
    if isinstance(val, str):
        return [item.strip() for item in val.split(",") if item.strip()]
    return val

TARGET_CREATORS   = _env_list("TARGET_CREATORS")   # e.g. "user1,user2"
TARGET_SUBREDDITS = _env_list("TARGET_SUBREDDITS") # e.g. "sub1,sub2"

# Fixed landing page for link suffix
LANDING_PAGE_URL = "https://redditwithsound.pages.dev/"

# Limit settings
MAX_DIRECT_LINKS         = 10
MAX_BIO_LINKS            = 5
MAX_COMMUNITY_POSTS_PER_DAY = 2

# File paths (env overridable)
SESSION_FILE            = os.environ.get("SESSION_FILE", "session.json")
REDDIT_SESSION_FILE     = os.environ.get("REDDIT_SESSION_FILE", "reddit_session.json")
LINK_COUNTER_FILE       = os.environ.get("LINK_COUNTER_FILE", "link_counter.json")
POSTED_CACHE_FILE       = os.environ.get("POSTED_CACHE_FILE", "posted_cache.json")
COMMUNITY_COUNTER_FILE  = os.environ.get("COMMUNITY_COUNTER_FILE", "community_counter.json")
CAPTCHA_LOCK_FILE       = os.environ.get("CAPTCHA_LOCK_FILE", "captcha_lock.txt")
DAILY_LIMIT_FILE        = os.environ.get("DAILY_LIMIT_FILE", "daily_post_limit.json")
MEDIA_DIR               = os.environ.get("MEDIA_DIR", "downloaded_media")
os.makedirs(MEDIA_DIR, exist_ok=True)

USE_LINK_SHORTENER = os.environ.get("USE_LINK_SHORTENER", "True").lower() == "true"

# Hardcoded X communities
X_COMMUNITIES = [
    "https://x.com/i/communities/1680111154108981250",
    "https://twitter.com/i/communities/1696464643940827249",
    "https://twitter.com/i/communities/1856975516160721170",
    "https://twitter.com/i/communities/1851494480442212618",
    "https://twitter.com/i/communities/1828213627117281516",
]

# ═══════════════════════════════════════════════════════════
# SESSION MANAGEMENT (X session)
# ═══════════════════════════════════════════════════════════
def load_session():
    session_json_str = os.environ.get("SESSION_JSON")
    if session_json_str:
        try:
            data = json.loads(session_json_str)
            if "cookies" in data:
                print(f"✅ SESSION_JSON loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ SESSION_JSON parse error: {e}")
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "cookies" in data:
                print(f"✅ session.json loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ session.json error: {e}")
    return None

def validate_session():
    session = load_session()
    if session is None:
        print("❌ No X session found. Bot stopped.")
        return False
    return True

# ═══════════════════════════════════════════════════════════
# CAPTCHA LOCK (unchanged from original)
# ═══════════════════════════════════════════════════════════
def is_captcha_locked():
    if not os.path.exists(CAPTCHA_LOCK_FILE):
        return False
    with open(CAPTCHA_LOCK_FILE, "r") as f:
        lock_time = float(f.read().strip())
    elapsed = time.time() - lock_time
    remaining = (12 * 3600) - elapsed
    if remaining > 0:
        hours = int(remaining // 3600)
        mins = int((remaining % 3600) // 60)
        print(f"🔒 Captcha lock active. {hours}h {mins}m remaining.")
        return True
    os.remove(CAPTCHA_LOCK_FILE)
    print("✅ Captcha lock ended.")
    return False

def set_captcha_lock():
    with open(CAPTCHA_LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    print("🔒 Captcha lock set for 12h.")

def check_captcha(page):
    try:
        captcha = page.query_selector(
            'iframe[src*="captcha"], '
            'div[data-testid="captcha"], '
            '#captcha, '
            'iframe[title*="captcha"]'
        )
        if captcha and captcha.is_visible():
            print("  ⚠️ CAPTCHA element detected!")
            page.screenshot(path=f"captcha_debug_elem_{int(time.time())}.png")
            set_captcha_lock()
            return True
    except:
        pass
    try:
        page_text = page.inner_text('body').lower()
        if any(phrase in page_text for phrase in [
            "verify your identity", "are you human", "unusual activity",
            "prove you're not a bot", "security challenge", "complete the challenge"
        ]):
            current_url = page.url.lower()
            if "challenge" in current_url or "captcha" in current_url or "suspended" in current_url:
                print("  ⚠️ Challenge text + suspicious URL detected!")
                page.screenshot(path=f"captcha_debug_text_{int(time.time())}.png")
                set_captcha_lock()
                return True
    except:
        pass
    current_url = page.url.lower()
    if "challenge" in current_url or "captcha" in current_url:
        print("  ⚠️ Challenge/Captcha URL detected!")
        page.screenshot(path=f"captcha_debug_url_{int(time.time())}.png")
        set_captcha_lock()
        return True
    return False

# ═══════════════════════════════════════════════════════════
# CACHE — 7-day posted memory (replaces old hash cache)
# ═══════════════════════════════════════════════════════════
def load_posted_cache() -> dict:
    if not os.path.exists(POSTED_CACHE_FILE):
        return {}
    try:
        with open(POSTED_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_posted_cache(cache: dict):
    with open(POSTED_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def is_recently_posted(post_id: str, cache: dict, days: int = 7) -> bool:
    if post_id not in cache:
        return False
    try:
        posted_at = datetime.fromisoformat(cache[post_id])
        return datetime.now(timezone.utc) - posted_at < timedelta(days=days)
    except:
        return False

def mark_as_posted(post_id: str, cache: dict) -> dict:
    cache[post_id] = datetime.now(timezone.utc).isoformat()
    cutoff = datetime.now(timezone.utc) - timedelta(days=8)
    cache = {k: v for k, v in cache.items() if datetime.fromisoformat(v) > cutoff}
    save_posted_cache(cache)
    return cache

# ═══════════════════════════════════════════════════════════
# DAILY POST LIMIT (unchanged)
# ═══════════════════════════════════════════════════════════
def get_daily_limit():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(DAILY_LIMIT_FILE):
        try:
            with open(DAILY_LIMIT_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return data["target"], data["count"]
        except:
            pass
    target = random.randint(35, 55)
    data = {"date": today_str, "target": target, "count": 0}
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump(data, f)
    print(f"📊 New daily post target: {target}")
    return target, 0

def increment_daily_counter():
    target, count = get_daily_limit()
    count += 1
    data = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "target": target, "count": count}
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump(data, f)
    print(f"📈 Daily count: {count}/{target}")
    return count >= target

# ═══════════════════════════════════════════════════════════
# LINK COUNTER (bio / direct / username)
# ═══════════════════════════════════════════════════════════
def load_link_counter() -> dict:
    if not os.path.exists(LINK_COUNTER_FILE):
        return {"date": "", "direct": 0, "bio": 0}
    try:
        with open(LINK_COUNTER_FILE, encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"date": "", "direct": 0, "bio": 0}

def save_link_counter(data: dict):
    with open(LINK_COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def _reset_if_new_day(data: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return {"date": today, "direct": 0, "bio": 0}
    return data

def decide_suffix_type() -> str:
    data        = _reset_if_new_day(load_link_counter())
    direct_done = data.get("direct", 0)
    bio_done    = data.get("bio", 0)
    EXPECTED    = 19
    total_done  = direct_done + bio_done
    direct_remaining = MAX_DIRECT_LINKS - direct_done
    bio_remaining    = MAX_BIO_LINKS    - bio_done
    posts_left       = max(EXPECTED - total_done, 1)
    p_direct = direct_remaining / posts_left if direct_remaining > 0 else 0
    p_bio    = bio_remaining    / posts_left if bio_remaining    > 0 else 0
    r = random.random()
    if r < p_direct:
        result = "direct"
    elif r < p_direct + p_bio:
        result = "bio"
    else:
        result = "username"
    print(f"  🎲 Suffix type → {result} (direct {direct_done}/{MAX_DIRECT_LINKS}, bio {bio_done}/{MAX_BIO_LINKS})")
    return result

def increment_link_counter(suffix_type: str):
    data = _reset_if_new_day(load_link_counter())
    if suffix_type == "direct":
        data["direct"] = data.get("direct", 0) + 1
    elif suffix_type == "bio":
        data["bio"] = data.get("bio", 0) + 1
    save_link_counter(data)
    print(f"  📊 Link counter — direct: {data['direct']}/{MAX_DIRECT_LINKS}, bio: {data['bio']}/{MAX_BIO_LINKS}")

# ═══════════════════════════════════════════════════════════
# COMMUNITY COUNTER
# ═══════════════════════════════════════════════════════════
def load_community_counter() -> dict:
    if not os.path.exists(COMMUNITY_COUNTER_FILE):
        return {"date": "", "communities": {}}
    try:
        with open(COMMUNITY_COUNTER_FILE, encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"date": "", "communities": {}}

def save_community_counter(data: dict):
    with open(COMMUNITY_COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_community_post_count(community_url: str) -> int:
    data  = load_community_counter()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return 0
    return data.get("communities", {}).get(community_url, 0)

def increment_community_counter(community_url: str):
    data  = load_community_counter()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today:
        data = {"date": today, "communities": {}}
    if "communities" not in data:
        data["communities"] = {}
    data["communities"][community_url] = data["communities"].get(community_url, 0) + 1
    save_community_counter(data)
    count = data["communities"][community_url]
    print(f"  📊 Community post count: {count}/{MAX_COMMUNITY_POSTS_PER_DAY} → {community_url[:50]}")

def get_eligible_communities() -> list:
    if not X_COMMUNITIES:
        return []
    eligible = [
        url for url in X_COMMUNITIES
        if get_community_post_count(url) < MAX_COMMUNITY_POSTS_PER_DAY
    ]
    print(f"  🏘️  Eligible communities: {len(eligible)}/{len(X_COMMUNITIES)}")
    return eligible

# ═══════════════════════════════════════════════════════════
# REDDIT COMMON PARSER
# ═══════════════════════════════════════════════════════════
REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

def _parse_reddit_children(children: list) -> list[dict]:
    posts = []
    for child in children:
        p = child.get("data", {})
        if p.get("stickied") or p.get("pinned"):
            continue
        post_id = p.get("id", "")
        title   = p.get("title", "").strip()
        if not title or not post_id:
            continue
        media_url = None
        is_video  = False
        if p.get("is_video") and p.get("media"):
            fallback = p["media"].get("reddit_video", {}).get("fallback_url", "")
            if fallback:
                media_url = fallback.split("?")[0]
                is_video  = True
        if not media_url:
            post_url = p.get("url", "")
            domain   = p.get("domain", "")
            if "redgifs.com" in domain or "redgifs.com" in post_url:
                media_url = post_url
                is_video  = True
        if not media_url:
            secure_media = p.get("secure_media") or p.get("media") or {}
            oembed = secure_media.get("oembed", {})
            if oembed and "video" in oembed.get("type", ""):
                embed_url = p.get("url", "")
                if embed_url:
                    media_url = embed_url
                    is_video  = True
        if not media_url and p.get("is_gallery"):
            continue
        if not media_url:
            post_url = p.get("url", "")
            if re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", post_url, re.IGNORECASE):
                media_url = post_url
        if not media_url:
            previews = p.get("preview", {}).get("images", [])
            if previews:
                src = previews[0].get("source", {}).get("url", "").replace("&amp;", "&")
                if src:
                    media_url = src
        if not media_url:
            continue
        posts.append({
            "id":          post_id,
            "title":       title,
            "permalink":   "https://www.reddit.com" + p.get("permalink", ""),
            "score":       p.get("score", 0),
            "created_utc": p.get("created_utc", 0),
            "media_url":   media_url,
            "is_video":    is_video,
            "subreddit":   p.get("subreddit", ""),
        })
    return posts

# ═══════════════════════════════════════════════════════════
# REDDIT FETCHING (Playwright)
# ═══════════════════════════════════════════════════════════
def get_creator_posts_playwright(username: str) -> list[dict]:
    if not os.path.exists(REDDIT_SESSION_FILE):
        print(f"  ❌ Reddit session missing! Run get_reddit_session.py first.")
        return []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                storage_state=REDDIT_SESSION_FILE,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            print(f"  🌐 Fetching u/{username}...")
            json_url = f"https://www.reddit.com/user/{username}/submitted.json?limit=25&sort=new"
            page.goto(json_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            content = page.inner_text("body")
            browser.close()
        data = json.loads(content)
        children = data.get("data", {}).get("children", [])
    except Exception as e:
        print(f"  ❌ Reddit fetch failed for u/{username}: {e}")
        return []
    return _parse_reddit_children(children)

def get_subreddit_posts_playwright(subreddit: str) -> list[dict]:
    if not os.path.exists(REDDIT_SESSION_FILE):
        print(f"  ❌ Reddit session missing!")
        return []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                storage_state=REDDIT_SESSION_FILE,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            print(f"  🌐 Fetching r/{subreddit}...")
            json_url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=25"
            page.goto(json_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            content = page.inner_text("body")
            browser.close()
        data = json.loads(content)
        children = data.get("data", {}).get("children", [])
    except Exception as e:
        print(f"  ❌ Subreddit fetch failed for r/{subreddit}: {e}")
        return []
    return _parse_reddit_children(children)

# ═══════════════════════════════════════════════════════════
# MEDIA DOWNLOAD
# ═══════════════════════════════════════════════════════════
def download_image(url: str, filename: str) -> str | None:
    try:
        r = requests.get(url, headers=REDDIT_HEADERS, timeout=20, stream=True)
        if r.status_code == 200:
            path = os.path.join(MEDIA_DIR, filename)
            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            print(f"  📥 Image: {os.path.getsize(path)//1024} KB")
            return path
    except Exception as e:
        print(f"  ❌ Image download failed: {e}")
    return None

def download_video_redgifs(redgifs_url: str) -> str | None:
    match = re.search(r'redgifs\.com/(?:watch|ifr)/([a-zA-Z0-9]+)', redgifs_url)
    if not match:
        return None
    gif_id = match.group(1).lower()
    print(f"  🎬 Redgifs ID: {gif_id}")
    try:
        token_r = requests.get(
            "https://api.redgifs.com/v2/auth/temporary",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20
        )
        token = token_r.json().get("token", "")
        headers = {"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {token}"}
        info_r = requests.get(
            f"https://api.redgifs.com/v2/gifs/{gif_id}",
            headers=headers, timeout=20
        )
        info = info_r.json()
        urls = info.get("gif", {}).get("urls", {})
        video_url = urls.get("hd") or urls.get("sd") or urls.get("silent")
        if not video_url:
            return None
        out = os.path.join(MEDIA_DIR, f"video_{int(time.time())}.mp4")
        r = requests.get(video_url, headers=headers, timeout=120, stream=True)
        if r.status_code == 200:
            with open(out, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            size = os.path.getsize(out)
            if size < 1000:
                os.remove(out)
                return None
            print(f"  ✅ Redgifs downloaded: {size//1024} KB")
            return out
    except Exception as e:
        print(f"  ❌ Redgifs error: {e}")
    return None

def download_video_ytdlp(url: str) -> str | None:
    out = os.path.join(MEDIA_DIR, f"video_{int(time.time())}.mp4")
    cmd = [
        "yt-dlp", "--no-playlist",
        "--format", "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", out,
        "--quiet", "--no-warnings",
        "--socket-timeout", "60",
        url,
    ]
    try:
        res = subprocess.run(cmd, timeout=180, capture_output=True, text=True)
        if res.returncode == 0 and os.path.exists(out):
            size = os.path.getsize(out)
            if size > 50 * 1024 * 1024:
                print("  ⚠️  Video >50MB — skip")
                os.remove(out)
                return None
            print(f"  📥 Video: {size//1024} KB")
            return out
    except Exception as e:
        print(f"  ❌ yt-dlp error: {e}")
    return None

def fetch_post_media(post: dict) -> tuple[str | None, bool]:
    url      = post.get("media_url")
    is_video = post.get("is_video", False)
    ts       = int(time.time())
    if not url:
        return None, False
    if is_video:
        if "redgifs.com" in url or "redgifs.com" in post.get("permalink", ""):
            path = download_video_redgifs(url)
            if not path:
                path = download_video_redgifs(post["permalink"])
        else:
            path = download_video_ytdlp(url)
            if not path:
                path = download_video_ytdlp(post["permalink"])
        return path, bool(path)
    ext  = re.search(r"\.(jpg|jpeg|png|gif|webp)", url, re.IGNORECASE)
    ext  = ext.group(0) if ext else ".jpg"
    path = download_image(url, f"img_{ts}{ext}")
    return path, False

# ═══════════════════════════════════════════════════════════
# LINK SHORTENER
# ═══════════════════════════════════════════════════════════
def shorten_url(long_url: str) -> str:
    if not USE_LINK_SHORTENER:
        return long_url
    try:
        r = requests.get(
            f"https://tinyurl.com/api-create.php?url={long_url}",
            timeout=10
        )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception as e:
        print(f"  ⚠️  Shortener failed: {e}")
    return long_url

# ═══════════════════════════════════════════════════════════
# LOCAL TITLE REWRITE (synonym replacement)
# ═══════════════════════════════════════════════════════════
def rewrite_title_locally(original: str) -> str:
    synonyms = {
        "fuck": ["bang", "nail", "drill", "rail", "pound", "smash", "wreck", "pipe", "destroy", "hammer", "ruin", "break"],
        "fucking": ["banging", "nailing", "drilling", "railing", "pounding", "smashing", "wrecking", "piping", "destroying", "hammering"],
        "suck": ["gobble", "slurp", "deep throat", "devour", "lick", "tongue"],
        "sucking": ["gobbling", "slurping", "deep throating", "devouring", "licking"],
        "dick": ["cock", "meat", "shaft", "dong", "pole", "rod", "tool", "member", "length"],
        "pussy": ["cunt", "snatch", "hole", "slit", "pink", "cooch", "flower"],
        "ass": ["butt", "backside", "rump", "behind", "booty", "cheeks", "hole"],
        "tits": ["boobs", "rack", "melons", "bust", "cans", "jugs", "knockers"],
        "cum": ["seed", "load", "jizz", "spunk", "nut", "cream", "splooge"],
        "horny": ["needy", "desperate", "eager", "thirsty", "heated", "achy"],
        "moan": ["groan", "whimper", "sigh", "gasp", "mewl"],
        "slut": ["whore", "tramp", "skank", "slag", "floozy"],
        "fuck me": ["take me", "ruin me", "break me", "wreck me", "destroy me", "nail me", "drill me", "rail me"],
        "suck me": ["gobble me", "slurp me", "deep throat me", "devour me", "lick me"],
        "eat me": ["devour me", "taste me", "lick me", "savor me"],
        "ride me": ["bounce on me", "grind on me", "mount me", "work me"],
        "hard": ["stiff", "rock", "solid", "rigid", "throbbing", "pulsing"],
        "wet": ["soaked", "drenched", "slick", "dripping", "creamy", "slippery"],
        "tight": ["snug", "gripping", "squeezing", "clenching", "vice-like"],
        "hot": ["steamy", "burning", "fiery", "red-hot", "sizzling", "scalding"],
        "sexy": ["alluring", "tempting", "irresistible", "provocative", "sultry", "steamy"],
    }
    words = original.split()
    new_words = []
    i = 0
    while i < len(words):
        w = words[i]
        if i + 1 < len(words):
            phrase = (w + " " + words[i+1]).lower().rstrip(",.!?")
            if phrase in synonyms:
                repl = random.choice(synonyms[phrase])
                if w[0].isupper():
                    repl = repl.capitalize()
                new_words.append(repl)
                i += 2
                continue
        low = w.lower().rstrip(",.!?")
        if low in synonyms:
            repl = random.choice(synonyms[low])
            if w[0].isupper():
                repl = repl.capitalize()
            new_words.append(repl)
        else:
            new_words.append(w)
        i += 1
    return " ".join(new_words)

# ═══════════════════════════════════════════════════════════
# GROK REWRITE (reuses existing X browser context)
# ═══════════════════════════════════════════════════════════
def grok_rewrite_using_context(context, original: str) -> str | None:
    """Open a new page in the logged-in X context, ask Grok to rewrite, return cleaned text."""
    page = context.new_page()
    try:
        print("  🌐 Grok page loading...")
        page.goto("https://x.com/i/grok", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # find textarea
        textarea = None
        for sel in ['textarea[placeholder="Ask anything"]', 'textarea']:
            try:
                el = page.wait_for_selector(sel, timeout=8000)
                if el and el.is_visible():
                    textarea = el
                    break
            except:
                continue
        if not textarea:
            print("  ❌ Grok textarea not found.")
            return None

        prompt = (
            f"Rewrite this sentence,\n"
            f"keep same meaning with aggressive words. Then add 2-3 relevant NSFW hashtags "
            f"that people actually search (do NOT use #NSFW). "
            f"return only result.\n"
            f"[rewritten sentence]\n"
            f"[hashtags]\n"
            f"Sentence: {original}"
        )
        textarea.click()
        page.wait_for_timeout(500)
        textarea.fill(prompt)
        page.wait_for_timeout(random.uniform(500, 800))

        # submit
        sent = False
        for btn_sel in [
            'button[aria-label="Send"]',
            'button[data-testid="grok-send-button"]',
            'button[type="submit"]',
        ]:
            try:
                btn = page.wait_for_selector(btn_sel, timeout=5000)
                if btn and btn.is_visible() and btn.is_enabled():
                    btn.click()
                    sent = True
                    break
            except:
                continue
        if not sent:
            page.keyboard.press("Enter")

        print("  ⏳ Waiting for Grok response...")
        page.wait_for_timeout(15000)

        response_text = ""
        for _ in range(20):
            page.wait_for_timeout(1500)
            try:
                els = page.query_selector_all("div.r-1wbh5a2.r-11niif6.r-bnwqim.r-13qz1uu")
                for el in reversed(els):
                    txt = el.inner_text().strip()
                    if txt and len(txt) > 5 and prompt[:20] not in txt:
                        if any(t in txt.lower() for t in [
                            "thinking about", "let me think", "i'm thinking",
                            "processing your", "considering your", "analyzing"
                        ]):
                            continue
                        response_text = txt
                        break
            except:
                pass
            if response_text:
                break

        if response_text:
            # parse title / tags
            title_part = ""
            tags_part  = ""
            for line in response_text.splitlines():
                line = line.strip()
                if line.upper().startswith("TITLE:"):
                    title_part = line[6:].strip()
                elif line.upper().startswith("TAGS:"):
                    tags_part = line[5:].strip()
            if not title_part:
                title_part = response_text.strip()
            # clean
            title_part = re.sub(r'^(Rewritten:|Original:|Title:|Output:)\s*', "", title_part, flags=re.IGNORECASE)
            title_part = re.sub(r'\*+|_+|#+|`+', "", title_part).strip().strip('"\' ')
            tags_part  = tags_part.strip()
            if title_part and not any(p in title_part.lower() for p in [
                "i'm sorry", "i cannot", "i can't", "i am unable", "not able to",
                "inappropriate", "against my", "my guidelines", "i apologize",
                "as an ai", "i must decline", "i won't", "cannot assist"
            ]):
                result = title_part
                if tags_part:
                    result = result + "\n" + tags_part
                print(f"  ✅ Grok title: {title_part[:70]}")
                return result
            print("  ⚠️  Grok refused/empty.")
        else:
            print("  ⚠️  Grok no response.")
    except Exception as e:
        print(f"  ⚠️  Grok error: {e}")
    finally:
        page.close()
    return None

def rewrite_with_grok_or_local(context, original: str) -> str:
    grok_result = grok_rewrite_using_context(context, original)
    if grok_result:
        return grok_result
    print("  ↪️  Grok failed, using local rewrite.")
    return rewrite_title_locally(original)

# ═══════════════════════════════════════════════════════════
# TWEET BUILDING (hook + suffix)
# ═══════════════════════════════════════════════════════════
def build_hook_tweet(post: dict, source_name: str, source_type: str,
                     has_video: bool, context, suffix_type: str = "username") -> str:
    original_title = post["title"].strip()
    ai_result = rewrite_with_grok_or_local(context, original_title)

    if ai_result:
        lines     = ai_result.strip().splitlines()
        tag_lines = [l for l in lines if l.strip().startswith("#")]
        txt_lines = [l for l in lines if not l.strip().startswith("#")]
        if not tag_lines and len(lines) >= 2:
            last = lines[-1].strip()
            words = last.split()
            if all(not w.startswith("#") and len(w) > 2 for w in words) and len(words) <= 5:
                tag_lines = ["#" + " #".join(words)]
                txt_lines = lines[:-1]
        title    = " ".join(txt_lines).strip()
        hashtags = " ".join(tag_lines).strip()
    else:
        title    = rewrite_title_locally(original_title)
        hashtags = "#NSFW #Reddit"

    if suffix_type == "direct":
        landing_url = shorten_url(LANDING_PAGE_URL) if USE_LINK_SHORTENER else LANDING_PAGE_URL
        suffix = f"\n\nWant to see something darker? 👀\n{landing_url}"
    elif suffix_type == "bio":
        suffix = "\n\nWant to see something darker? 👀\nLink in bio ↑"
    else:
        prefix = "u/" if source_type == "user" else "r/"
        suffix = f"\n\n{prefix}{source_name}"

    hashtag_block = f"\n{hashtags}" if hashtags else ""
    tweet = title + hashtag_block + suffix

    if len(tweet) > 280:
        max_title = 280 - len(hashtag_block) - len(suffix) - 3
        title = title[:max_title] + "..."
        tweet = title + hashtag_block + suffix

    return tweet

# ═══════════════════════════════════════════════════════════
# X POSTING HELPERS (kept from original, with community post addition)
# ═══════════════════════════════════════════════════════════
def human_mouse_move(page, target_x, target_y, steps=15):
    start_x, start_y = random.randint(100, 300), random.randint(100, 300)
    cp_x = (start_x + target_x) / 2 + random.randint(-80, 80)
    cp_y = (start_y + target_y) / 2 + random.randint(-80, 80)
    for i in range(steps + 1):
        t = i / steps
        x = (1-t)**2 * start_x + 2*(1-t)*t * cp_x + t**2 * target_x
        y = (1-t)**2 * start_y + 2*(1-t)*t * cp_y + t**2 * target_y
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.005, 0.015))

def human_type(page, element, text):
    element.click()
    time.sleep(random.uniform(0.3, 0.8))
    for char in text:
        element.type(char, delay=random.randint(40, 120))
        if random.random() < 0.05:
            time.sleep(random.uniform(0.3, 0.9))
    time.sleep(random.uniform(0.5, 1.2))

def type_and_submit(page, text, media_paths):
    viewport = page.viewport_size
    human_mouse_move(page, viewport['width']//2, viewport['height']//2)
    textarea = page.wait_for_selector(
        'div[data-testid="tweetTextarea_0"]', timeout=25000
    )
    box = textarea.bounding_box()
    human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
    human_type(page, textarea, text)
    page.wait_for_timeout(random.randint(800, 1500))

    if media_paths:
        has_video = False
        for mp in media_paths:
            try:
                attach_btn = page.query_selector('button[aria-label="Add photos or video"]')
                if attach_btn:
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        attach_btn.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(mp)
                    if mp.lower().endswith('.mp4'):
                        has_video = True
                        print(f"  🎞 Video file queued: {os.path.basename(mp)}")
                    else:
                        print(f"  📎 Image queued: {os.path.basename(mp)}")
                else:
                    print(f"  ⚠️ Attach button not found.")
            except Exception as e:
                print(f"  ⚠️ Media attach error: {e}")

        if has_video:
            page.wait_for_timeout(3000)
            try:
                page.wait_for_selector('div[data-testid="attachments"]', timeout=30000)
                print("  ✅ Attachment container found.")
            except:
                print("  ⚠️ Attachment container not found.")
                page.screenshot(path=f"attach_fail_{int(time.time())}.png")
            page.wait_for_timeout(45000)
            try:
                page.wait_for_selector('div[data-testid="attachments"] video', timeout=15000)
                print("  ✅ Video preview confirmed.")
            except:
                print("  ⚠️ Preview not confirmed, continuing anyway.")
        else:
            page.wait_for_timeout(random.randint(3000, 5000))

    try:
        btn = page.wait_for_selector('div[data-testid="tweetButtonInline"]', timeout=8000)
    except:
        btn = page.wait_for_selector('button[data-testid="tweetButton"]', timeout=8000)
    box = btn.bounding_box()
    human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
    page.wait_for_timeout(random.randint(500, 1200))
    btn.click()
    page.wait_for_timeout(5000)

def open_compose_and_post(page, text, media_paths):
    for method_num, method in enumerate(["keyboard", "sidenav", "direct"], 1):
        try:
            print(f"  🔄 Method {method_num} ({method})...")
            if method in ("keyboard", "sidenav"):
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(random.randint(4000, 7000))
                if check_captcha(page):
                    raise Exception("CAPTCHA_DETECTED")
                if method == "keyboard":
                    page.keyboard.press("n")
                else:
                    btn = page.wait_for_selector(
                        'a[data-testid="SideNav_NewTweet_Button"]', timeout=15000
                    )
                    box = btn.bounding_box()
                    human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
                    btn.click()
            else:
                page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(random.randint(4000, 7000))
                if check_captcha(page):
                    raise Exception("CAPTCHA_DETECTED")
            page.wait_for_timeout(random.randint(2000, 4000))
            type_and_submit(page, text, media_paths)
            print(f"  ✅ Method {method_num} success!")
            return True
        except Exception as e:
            if "CAPTCHA_DETECTED" in str(e):
                raise
            print(f"  ❌ Method {method_num} failed: {e}")
    print("  💥 All posting methods failed.")
    return False

def post_to_community(page, community_url, text, media_paths):
    print(f"  🏘️  Posting to community: {community_url[:60]}...")
    try:
        xcom_url = community_url.replace("twitter.com", "x.com")
        page.goto(xcom_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(random.randint(5000, 8000))
        if check_captcha(page):
            raise Exception("CAPTCHA_DETECTED")
        # Find sidebar new post button
        post_btn = None
        for sel in [
            'a[data-testid="SideNav_NewTweet_Button"]',
            'button[data-testid="SideNav_NewTweet_Button"]',
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=8000)
                if btn and btn.is_visible():
                    post_btn = btn
                    break
            except:
                continue
        if not post_btn:
            print("  ❌ Sidebar Post button not found.")
            return False
        post_btn.click()
        page.wait_for_timeout(random.randint(2000, 3500))
        # Now use common type_and_submit
        return type_and_submit(page, text, media_paths)
    except Exception as e:
        if "CAPTCHA_DETECTED" in str(e):
            raise
        print(f"  ❌ Community post failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# MAIN REDDIT POST FUNCTION (replaces perform_post_only)
# ═══════════════════════════════════════════════════════════
def perform_reddit_post(page, context, posted_cache):
    # Decide if community post
    eligible_communities = get_eligible_communities()
    post_destination = "profile"
    chosen_community = None
    if eligible_communities:
        total_remaining = sum(
            MAX_COMMUNITY_POSTS_PER_DAY - get_community_post_count(url)
            for url in eligible_communities
        )
        community_prob = total_remaining / (total_remaining + 9)
        if random.random() < community_prob:
            chosen_community = random.choice(eligible_communities)
            post_destination = "community"
    if post_destination == "community":
        print(f"🏘️  Posting to community: {chosen_community[:60]}")
    else:
        print("👤 Posting to profile.")

    # Build combined source list
    sources = []
    if TARGET_CREATORS:
        for u in TARGET_CREATORS:
            sources.append(("user", u))
    if TARGET_SUBREDDITS:
        for s in TARGET_SUBREDDITS:
            sources.append(("subreddit", s))
    if not sources:
        print("❌ No Reddit sources configured. Set TARGET_CREATORS or TARGET_SUBREDDITS.")
        return False

    random.shuffle(sources)
    chosen_source_type = None
    chosen_source_name = None
    chosen_post        = None

    print("📡 Scanning Reddit sources...")
    for source_type, source_id in sources:
        if source_type == "user":
            print(f"👤 u/{source_id} checking...")
            posts = get_creator_posts_playwright(source_id)
        else:
            print(f"🔖 r/{source_id} checking...")
            posts = get_subreddit_posts_playwright(source_id)

        time.sleep(random.uniform(1.5, 3.0))
        if not posts:
            continue
        unposted = [p for p in posts if not is_recently_posted(p["id"], posted_cache)]
        if not unposted:
            continue
        videos = [p for p in unposted if p.get("is_video")]
        images = [p for p in unposted if not p.get("is_video")]
        print(f"  📊 Unposted: {len(unposted)} | Video: {len(videos)} | Image: {len(images)}")
        chosen_post = None
        want_video = random.random() < 0.80
        if want_video and videos:
            chosen_post = random.choice(videos)
        elif not want_video and images:
            chosen_post = random.choice(images)
        elif videos:
            chosen_post = random.choice(videos)
        elif images:
            chosen_post = random.choice(images)
        if chosen_post:
            chosen_source_type = source_type
            chosen_source_name = source_id
            print(f"  ✅ Selected: {chosen_post['title'][:60]}")
            break

    if not chosen_post:
        print("\n⚠️  No new Reddit posts found.")
        return False

    print(f"\n🏆 Final: @{chosen_source_name} ({chosen_source_type}) - {chosen_post['title'][:80]}")

    # Download media
    print("📥 Downloading media...")
    media_path, is_video = fetch_post_media(chosen_post)
    print(f"   Media: {media_path or 'None'} | Video: {is_video}")

    # Determine suffix type
    suffix_type = decide_suffix_type()
    increment_link_counter(suffix_type)

    # Build tweet (Grok rewrite)
    print("🤖 Building tweet...")
    hook_tweet = build_hook_tweet(chosen_post, chosen_source_name, chosen_source_type,
                                  is_video, context, suffix_type)
    print(f"   Tweet: {hook_tweet}")

    # Post
    media_list = [media_path] if media_path else []
    posted = False
    if post_destination == "community":
        posted = post_to_community(page, chosen_community, hook_tweet, media_list)
        if posted:
            increment_community_counter(chosen_community)
    else:
        posted = open_compose_and_post(page, hook_tweet, media_list)

    if not posted:
        print("❌ Post failed.")
        return False

    # Update caches
    posted_cache = mark_as_posted(chosen_post["id"], posted_cache)
    limit_reached = increment_daily_counter()
    print("✅ Post successful!")
    if limit_reached:
        print("🎯 Daily post limit reached.")
    # Cleanup media
    if media_path and os.path.exists(media_path):
        try:
            os.remove(media_path)
        except:
            pass
    return True

# ═══════════════════════════════════════════════════════════
# MAIN LOOP (unchanged structure, now calls Reddit function)
# ═══════════════════════════════════════════════════════════
def human_delay(iteration, hour):
    if 6 <= hour < 10:
        base = random.randint(15, 25) * 60
    elif 10 <= hour < 16:
        base = random.randint(10, 18) * 60
    elif 16 <= hour < 22:
        base = random.randint(15, 25) * 60
    else:
        base = random.randint(40, 90) * 60
    if iteration > 40:
        base = int(base * 1.6)
    elif iteration > 25:
        base = int(base * 1.3)
    return base

def run_bot_loop():
    if not validate_session():
        return
    if is_captcha_locked():
        return

    target, current = get_daily_limit()
    print(f"📊 Daily limit: {current}/{target}")
    if current >= target:
        print("🎯 Today's post limit already reached. Exiting.")
        return

    MAX_DURATION = 6 * 3600
    start_time = time.time()

    with sync_playwright() as p:
        headless = os.environ.get("HEADLESS", "false").lower() == "true"
        browser = p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--use-gl=egl",
            ]
        )
        session_data = load_session()
        context = browser.new_context(
            storage_state=session_data,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()

        # Fingerprint spoofing (same as original)
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});

            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Google Inc. (Intel)';
                if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter.call(this, parameter);
            };

            const originalCreateOscillator = AudioContext.prototype.createOscillator;
            AudioContext.prototype.createOscillator = function() {
                const osc = originalCreateOscillator.apply(this, arguments);
                const originalStart = osc.start;
                osc.start = function() {
                    setTimeout(() => originalStart.apply(this, arguments), Math.random() * 2);
                };
                return osc;
            };

            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({state: Notification.permission}) :
                    originalQuery(parameters)
            );
        """)

        print(f"\n🤖 Reddit→X Bot started — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        iteration = 0
        SIESTA_EVERY = random.randint(15, 20)

        while True:
            target, current = get_daily_limit()
            if current >= target:
                print("🎯 Daily limit reached. Stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > MAX_DURATION - 300:
                print("⏰ Approaching 6-hour limit. Exiting loop.", flush=True)
                break

            if is_captcha_locked():
                print("🔒 Captcha lock active. Exiting loop.", flush=True)
                break

            if iteration > 0 and iteration % SIESTA_EVERY == 0:
                siesta = random.randint(45, 90) * 60
                print(f"\n☕ Siesta for {siesta//60} minutes...", flush=True)
                time.sleep(siesta)
                continue

            iteration += 1
            now = datetime.now()
            print(f"\n🔄 Iteration {iteration} — {now.strftime('%H:%M:%S')}", flush=True)

            posted_cache = load_posted_cache()
            success = perform_reddit_post(page, context, posted_cache)
            if not success:
                print("⚠️ Post failed, continuing after delay.", flush=True)

            delay = human_delay(iteration, now.hour)
            print(f"⏳ Next post in {delay//60} minutes...", flush=True)
            time.sleep(delay)

        browser.close()
        print("\n🔒 Browser closed. Loop ended.", flush=True)

if __name__ == "__main__":
    delay = random.randint(60, 180)
    print(f"⏱ {delay}s initial delay...")
    time.sleep(delay)
    run_bot_loop()
