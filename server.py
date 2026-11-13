from fastmcp import FastMCP
import instagrapi
import json
import os
import sys
import logging
from typing import Dict, Any
import time
import re

# Set up logging to stderr instead of stdout
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%m/%d/%y %H:%M:%S',
    stream=sys.stderr
)

# Version information
VERSION = "1.3.5"

# NOTE (local patch): do NOT redirect sys.stdout to sys.stderr here. The mcp SDK
# writes protocol frames through the sys.stdout object, so swapping it sends the
# MCP protocol itself to stderr and the client times out. fastmcp logging is
# already configured to stderr above; keep stdout untouched as the protocol
# channel. Upstream comment claimed this prevented stray prints breaking the
# protocol, but it has the opposite effect with current mcp SDK versions.
# sys.stdout = sys.stderr

# Track startup time
startup_time = time.time()

# Initialize the MCP server
mcp = FastMCP("InstagramDM", version=VERSION, dependencies=["instagrapi"])

# Load Instagram credentials from environment variables or cookie file
cookies = {}

# First try getting credentials directly from environment variables (preferred method)
sessionid = os.environ.get("INSTAGRAM_SESSION_ID")
csrftoken = os.environ.get("INSTAGRAM_CSRF_TOKEN")
ds_user_id = os.environ.get("INSTAGRAM_DS_USER_ID")

if sessionid and csrftoken and ds_user_id:
    cookies = {
        "sessionid": sessionid,
        "csrftoken": csrftoken,
        "ds_user_id": ds_user_id
    }
    logging.info("Using Instagram credentials from environment variables")
# Otherwise, fall back to legacy format from either env or file
elif os.environ.get("INSTAGRAM_COOKIES"):
    try:
        cookies = json.loads(os.environ.get("INSTAGRAM_COOKIES", "{}"))
        logging.info("Using Instagram credentials from INSTAGRAM_COOKIES environment variable")
    except json.JSONDecodeError:
        logging.error("INSTAGRAM_COOKIES environment variable is not valid JSON")
# Last resort: try loading from file
elif os.path.exists("instagram_cookies.json"):
    try:
        with open("instagram_cookies.json", "r") as f:
            cookies = json.load(f)
        logging.info("Using Instagram credentials from cookies file")
    except json.JSONDecodeError:
        logging.error("instagram_cookies.json is not valid JSON")

# Initialize Instagram client
client = instagrapi.Client()
try:
    # Handle the cookies dict correctly
    if cookies and 'sessionid' in cookies:
        sessionid = cookies.get('sessionid')
        client.login_by_sessionid(sessionid)
        logging.info("Successfully logged in with session ID")
    else:
        logging.warning("No valid Instagram session found. You need to set cookies.")
except Exception as e:
    logging.error(f"Failed to initialize Instagram client: {e}")
    # Continue without raising to allow server to start

# NOTE (local patch): media metadata extraction. Upstream read_dms returned
# msg.text only, so every shared reel, post, story or story mention arrived as
# text: None. Modern shares come as xma_* item types; instagrapi models those
# through msg.xma_share (canonical instagram.com/<reel|p>/<code> URL plus the
# caption in .title) and keeps the raw payload for unmodelled ones in msg.raw_xma.
IG_PATH_RE = re.compile(
    r"instagram\.com/(reels?/|p/|tv/|stories/[A-Za-z0-9_.]+?(?:/[0-9]+)?/)([A-Za-z0-9_-]+)"
)
MEDIA_TYPE_NAMES = {1: "image", 2: "video", 8: "carousel"}


def _canonical_share_url(url: str) -> str:
    """Strip tracking params from an Instagram share URL; None if not a web URL."""
    if not url:
        return None
    match = IG_PATH_RE.search(url)
    if match:
        return f"https://www.instagram.com/{match.group(1)}{match.group(2)}/"
    plain = url.split("?", 1)[0]
    return plain if plain.startswith("http") else None


def _media_from_xma(xma) -> dict:
    if not xma or not getattr(xma, "video_url", None):
        return None
    url = _canonical_share_url(xma.video_url)
    caption = (xma.title or "").strip() or None
    if not url and not caption:
        return None
    kind = "media"
    segment = IG_PATH_RE.search(xma.video_url)
    if segment:
        path = segment.group(1).rstrip("/")
        kind = "reel" if "reel" in path else "story" if "stories" in path else "post"
    return {"kind": kind, "url": url, "caption": caption}


def _media_from_raw_xma(xma_share, raw) -> dict:
    """Fallback for unmodelled xma item types (e.g. xma_story_share): the raw
    payload keeps target_url and the caption in title/caption body text."""
    if xma_share or not raw:
        return None
    for key, items in raw.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = _canonical_share_url(item.get("target_url"))
            caption = item.get("title_text") or item.get("subtitle_text") or item.get("caption_body_text")
            caption = caption.strip() if isinstance(caption, str) else None
            if url or caption:
                return {"kind": "story" if "story" in key else "media", "url": url, "caption": caption}
    return None


def _media_from_media_obj(media) -> dict:
    """Full Media objects (media_share posts, clip reels) carry code + caption."""
    if not media or not media.code:
        return None
    if media.product_type == "clips":
        kind, path = "reel", "reel"
    else:
        kind, path = "post", "p"
    return {
        "kind": kind,
        "url": f"https://www.instagram.com/{path}/{media.code}/",
        "caption": (media.caption_text or "").strip() or None,
    }


def _media_from_direct_media(media) -> dict:
    """Photos/videos/audio sent directly inside the DM (not feed shares)."""
    if not media:
        return None
    url = media.video_url or media.audio_url or media.thumbnail_url
    if not url:
        return None
    return {"kind": MEDIA_TYPE_NAMES.get(media.media_type, "media"), "url": str(url), "caption": None}


def _message_media_info(msg) -> dict:
    """First populated share wins; typed xma_share is the most specific."""
    for candidate in (
        _media_from_xma(msg.xma_share),
        _media_from_media_obj(msg.clip),
        _media_from_media_obj(msg.media_share),
        _media_from_direct_media(msg.media),
        _media_from_raw_xma(msg.xma_share, msg.raw_xma),
    ):
        if candidate:
            return candidate
    return None


# Tool: Read recent DMs
@mcp.tool()
def read_dms(limit: int = 10) -> Dict[str, Any]:
    """Read recent Instagram DMs from the inbox.
    
    Args:
        limit (int): Number of threads/messages to fetch (default: 10)
    
    Returns:
        dict: List of messages with sender, text, media metadata, and timestamp
    """
    try:
        inbox = client.direct_threads(amount=limit)
        messages = []
        for thread in inbox:
            thread_info = {
                "thread_id": thread.id,
                "thread_title": thread.thread_title,
                "users": [user.username for user in thread.users if hasattr(user, 'username')],
            }
            
            for msg in thread.messages:
                sender_username = None
                # Safely get username if possible
                try:
                    sender_username = next((user.username for user in thread.users if hasattr(user, 'username') and user.pk == msg.user_id), None)
                except:
                    pass

                # NOTE (local patch): surface non-text content. Keep the
                # original keys untouched; null fields mean absent.
                media_info = _message_media_info(msg)
                link_info = None
                if msg.link and msg.link.link_context:
                    link_info = {
                        "url": msg.link.link_context.link_url,
                        "title": msg.link.link_context.link_title,
                        "summary": msg.link.link_context.link_summary,
                    }
                reply_to = None
                if msg.reply:
                    reply_to = {"item_type": msg.reply.item_type, "text": msg.reply.text}
                reactions = []
                if msg.reactions and msg.reactions.emojis:
                    reactions = [r.emoji for r in msg.reactions.emojis]
                
                messages.append({
                    "thread": thread_info,
                    "sender_id": msg.user_id,
                    "sender": sender_username,
                    "text": msg.text,
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                    "item_type": msg.item_type,
                    "media": media_info,
                    "link": link_info,
                    "reply_to": reply_to,
                    "reactions": reactions,
                })
        return {"status": "success", "messages": messages}
    except Exception as e:
        logging.error(f"Error reading DMs: {e}")
        return {"status": "error", "message": str(e)}

# Tool: Send a DM
@mcp.tool()
def send_dm(username: str, message: str) -> Dict[str, Any]:
    """Send a direct message to an Instagram user.
    
    Args:
        username (str): Instagram username of the recipient
        message (str): Message content to send
    
    Returns:
        dict: Status of the operation
    """
    if not username or not message:
        return {"status": "error", "message": "Username and message are required"}
    try:
        recipient_id = client.user_id_from_username(username)
        result = client.direct_send(message, [recipient_id])
        logging.info(f"Successfully sent DM to {username}")
        return {"status": "success", "message": f"Sent DM to {username}"}
    except Exception as e:
        logging.error(f"Error sending DM to {username}: {e}")
        return {"status": "error", "message": str(e)}

# Resource: Dynamic greeting (keeping your example)
@mcp.resource("greeting://{name}")
def get_greeting(name: str) -> str:
    """Get a personalized greeting.
    
    Args:
        name (str): Name to include in the greeting
    
    Returns:
        str: Personalized greeting
    """
    return f"Hello, {name}!"

# Health check tool
@mcp.tool()
def health_check() -> Dict[str, str]:
    """Check the server's health status."""
    # Simplified health check to avoid potential issues
    # NOTE (local patch): upstream referenced an undefined variable `cl`
    # (the module-level client is `client`), so health_check always crashed.
    is_logged_in = hasattr(client, 'user_id') and client.user_id is not None
    
    result = {
        "status": "healthy",
        "service": "InstagramDM MCP Server",
        "version": VERSION,
        "logged_in": str(is_logged_in)
    }
    
    logging.info(f"Health check completed with status: {result['status']}")
    return result
if __name__ == "__main__":
    # Run the MCP server
    logging.info("Starting Instagram DM MCP Server...")
    mcp.run()
